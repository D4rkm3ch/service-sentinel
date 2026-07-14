import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from app import ai_provider, check_state, db, stacks
from app.check_state import set_finished, set_running
from app.config import settings
from app.docker_client import get_container_logs_since, list_running_containers_for_logs
from app.docker_client import open_client as open_docker_client
from app.log_filter import extract_suspicious_excerpt
from app.notifications import notify_findings_digest, notify_logs_check_errors
from app.summarizer import analyze_logs_batch

logger = logging.getLogger("service_sentinel.log_watcher")

ProgressFunc = Optional[Callable[[str, int, int], None]]

# A real-world report: a homelab with enough chatty containers (24 out of 59 matching a
# suspicious keyword in one check) sent one unbounded combined excerpt set to the AI in a
# single triage call -- ~150K characters of excerpts, needing a response covering ~20 real
# findings at once. That response needed several truncation-retry rounds (see ai_provider.py's
# _with_truncation_retry) just to squeak through, and on a run where the model's response
# happened to be a little more verbose, it silently hit the retry ceiling and came back
# unparseable -- losing every finding for every container in the whole check, not just
# whichever container pushed it over the edge. Chunking bounds each individual AI call's input
# (and therefore its expected output) well under where that ceiling becomes a real risk, and
# isolates failures to just the chunk that failed -- same "many independent calls, not one
# giant fragile one" principle persist.py's Updates pipeline already uses via
# _run_concurrent_phase, just batched a few containers at a time here instead of going fully
# per-container, to keep the token-cost savings of batching for the common (few matches) case.
_MAX_BATCH_EXCERPT_CHARS = 24000
_MAX_BATCH_CONTAINERS = 8


def _chunk_excerpts(excerpts_by_container: dict[str, str]) -> list[dict[str, str]]:
    """Splits a (possibly large) set of per-container excerpts into chunks bounded by both
    total characters and container count, whichever limit is hit first -- see this module's
    own _MAX_BATCH_EXCERPT_CHARS/_MAX_BATCH_CONTAINERS comment for why. A single container
    with an excerpt over the character limit still gets its own chunk (never dropped) rather
    than being split further -- MAX_EXCERPT_CHARS in log_filter.py already caps any one
    container's excerpt well under what a single triage call can handle alone."""
    chunks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_chars = 0
    for name, excerpt in excerpts_by_container.items():
        if current and (current_chars + len(excerpt) > _MAX_BATCH_EXCERPT_CHARS or len(current) >= _MAX_BATCH_CONTAINERS):
            chunks.append(current)
            current = {}
            current_chars = 0
        current[name] = excerpt
        current_chars += len(excerpt)
    if current:
        chunks.append(current)
    return chunks


def run_log_check() -> dict:
    """Runs one full log-health pass over every non-ignored running container -- see
    run_log_check_for below for the actual fetch/filter/triage logic this wraps. Deliberately
    does NOT check db.get_feature_enabled("logs") here -- this function backs both the
    scheduled job and the manual Check now button (see scheduler.py), and the feature toggle is
    only meant to control the automatic schedule, not the manual button.
    scheduler.apply_schedules() is what actually skips scheduling this when the feature is
    disabled."""
    set_running("logs")

    try:
        containers = list_running_containers_for_logs()
    except Exception:
        logger.exception("Could not reach the Docker socket — skipping this log check")
        result = {"checked": 0, "findings_found": 0, "errors": 1, "rate_limited": 0, "cancelled": False}
        set_finished("logs", result)
        return result

    checked_names = [c.name for c in containers]
    result = run_log_check_for(
        checked_names,
        on_progress=lambda stage, done, total: check_state.set_progress("logs", stage, done, total),
    )
    logger.info(
        "Log check complete: %d containers checked, %d findings", result["checked"], result["findings_found"]
    )
    _run_log_stack_analysis_pass_safely(checked_names)
    set_finished("logs", result)
    return result


def run_log_check_for(container_names: list[str], on_progress: ProgressFunc = None) -> dict:
    """The actual fetch/filter/triage pass, scoped to whichever container names are given --
    pull logs since each one's last checkpoint (or the configured lookback window, for a
    container with none), keep only lines that matched a suspicious keyword locally, and —
    only for containers that actually had something worth showing — send those excerpts to
    Claude for triage in bounded-size chunks (see _chunk_excerpts), dispatched concurrently
    (capped by ai_provider.concurrency_limit()) rather than one after another -- several
    chunks run in parallel instead of each one waiting for the last to finish. Containers with
    clean logs never reach the API at all. Shared by the full check (run_log_check, every
    currently running container) and every scoped Check now / Reset & re-check action (stack-
    and service-level), which call this directly with just their own subset.

    on_progress (stage, done, total), when given, is called once upfront with (0, total) before
    each phase starts and once per completion after -- both stage="checking_logs" (per
    container) and stage="triage_logs" (once per chunk, if any triage calls happen) -- same
    shape as persist.py's Updates pipeline (_run_concurrent_phase), so the status
    badge's live "Checking container logs (N/M)…" text works the same way at every scope (main
    page, stack, and service) instead of Logs' checks all silently sitting at a bare, totalless
    "Checking…" for their entire duration.

    Checkpoints are read and written in two batched calls (db.get_log_watch_checkpoints /
    set_log_watch_checkpoints) rather than one small connection per container -- same
    "two-phase, fixed connection count regardless of item count" discipline persist.py's
    Updates pipeline uses. Does NOT call set_running/set_finished (that's the full-check-only
    feature-level status badge) and does NOT run the Cross-Service Analysis pass itself --
    callers that want it call stacks.run_log_stack_analysis_pass afterward, same as
    run_log_check does above."""
    checked = 0
    findings_found = 0
    excerpts_by_container: dict[str, str] = {}
    checkpoints = db.get_log_watch_checkpoints(container_names)
    checked_ok_names: list[str] = []
    failed: dict[str, str] = {}
    total = len(container_names)

    # Reported once upfront (0, total) before the loop starts, not just after each completion
    # -- without this, a scoped check covering just one or two containers would sit at a bare,
    # totalless "Checking…" for its entire duration (see main.py's _progress_text: no total
    # means no "(N/M)" text at all), only ever ticking over a moment before it was already done.
    if on_progress and total:
        on_progress("checking_logs", 0, total)

    # One shared Docker client for the whole fetch loop -- without it every container's fetch
    # opens (and version-negotiates) its own client, paid once per container per check. If the
    # shared open itself fails (daemon unreachable), fall back to client=None so each fetch
    # attempts (and records) its own failure per-container, exactly like before -- the shared
    # client is purely an optimization, never a new way for the whole check to fail.
    docker_client = None
    try:
        docker_client = open_docker_client()
    except Exception as exc:
        # Expected whenever the daemon is unreachable -- each per-container fetch below will
        # surface (and record) its own failure, so a short note beats a full traceback here.
        logger.warning("Could not open a shared Docker client (%s); falling back to per-container connects", exc)
    cancelled = False
    try:
        for name in container_names:
            if check_state.is_cancel_requested("logs"):
                cancelled = True
                break
            checked += 1
            checkpoint = checkpoints.get(name)
            try:
                log_text = get_container_logs_since(
                    name, checkpoint, settings.log_max_lines_per_container, client=docker_client,
                )
            except Exception as exc:
                logger.exception("Could not fetch logs for %s", name)
                failed[name] = str(exc) or exc.__class__.__name__
                if on_progress:
                    on_progress("checking_logs", checked, total)
                continue

            checked_ok_names.append(name)
            excerpt = extract_suspicious_excerpt(log_text) if log_text else None
            if excerpt:
                excerpts_by_container[name] = excerpt
            if on_progress:
                on_progress("checking_logs", checked, total)
    finally:
        if docker_client is not None:
            docker_client.close()

    db.set_log_watch_checkpoints(checked_ok_names)
    db.clear_log_check_errors(checked_ok_names)
    db.record_log_check_errors(failed)
    if failed:
        notify_logs_check_errors([{"container_name": name, "error": err} for name, err in failed.items()])

    if not excerpts_by_container:
        return {"checked": checked, "findings_found": 0, "errors": len(failed), "rate_limited": 0, "cancelled": cancelled}

    ai_provider.reset_rate_limited_count()
    chunks = _chunk_excerpts(excerpts_by_container)
    include_fix = db.get_deep_analysis_enabled("logs")
    findings: list[dict] = []
    triage_errors = 0
    total_chunks = len(chunks)
    if on_progress:
        on_progress("triage_logs", 0, total_chunks)

    # Chunks are dispatched concurrently (same thread-pool-capped-by-ai_provider.concurrency_
    # limit() shape as persist.py's _run_concurrent_phase), not one after another -- a real-
    # world report: with several chunks, running them sequentially meant total wait time was
    # every chunk's AI latency added together (a check with a handful of chunks taking 50-60s
    # for what's really only a few seconds of AI work per chunk). progress_lock guards
    # findings/triage_errors/done_count the same way reconcile.run_check()'s own concurrent
    # phase does -- safe to mutate from multiple worker threads at once.
    progress_lock = threading.Lock()
    done_count = 0

    def _triage_chunk(chunk: dict[str, str]) -> None:
        nonlocal triage_errors, done_count, cancelled
        # Same "queued work stops, in-flight work finishes" semantics as persist.py's
        # _run_concurrent_phase -- a chunk a worker thread has already picked up still gets its
        # real AI call, only ones still waiting behind the concurrency cap skip it. A skipped
        # chunk isn't counted as an error -- it's simply not attempted, not a failure.
        if check_state.is_cancel_requested("logs"):
            with progress_lock:
                cancelled = True
                done_count += 1
                current = done_count
            if on_progress:
                on_progress("triage_logs", current, total_chunks)
            return
        try:
            result = analyze_logs_batch(chunk, include_fix=include_fix)
        except Exception:
            logger.exception("Log triage AI call failed for a chunk of %d container(s)", len(chunk))
            result = None
        with progress_lock:
            if result is None:
                triage_errors += 1
            else:
                findings.extend(result)
            done_count += 1
            current = done_count
        if on_progress:
            on_progress("triage_logs", current, total_chunks)

    max_workers = min(ai_provider.concurrency_limit(), total_chunks)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_triage_chunk, chunks))

    new_findings = []
    for finding in findings:
        container_name = finding.get("container")
        title = finding.get("title")
        if not container_name or not title:
            continue
        severity = finding.get("severity", "warning")
        _finding_id, is_new = db.upsert_finding(
            source="logs",
            subject=container_name,
            title=title,
            category=finding.get("category", "error"),
            severity=severity,
            description_markdown=finding.get("description", ""),
            suggested_fix=finding.get("fix"),
        )
        findings_found += 1
        if is_new:
            new_findings.append({"subject": container_name, "severity": severity, "title": title})

    notify_findings_digest("logs", new_findings)

    return {
        "checked": checked, "findings_found": findings_found, "errors": len(failed) + triage_errors,
        "rate_limited": ai_provider.rate_limited_count(), "cancelled": cancelled,
    }


# ---------------------------------------------------------------------------
# Scoped Check now / Reset & re-check (service- and stack-level) -- same claimed-mutex shape as
# persist.py's Updates equivalents, keyed by check_state's "logs" channel rather than "updates"
# since these are Logs-scoped actions. A container/stack's own identity never changes underneath
# a running action the way an Updates row's id can, so unlike persist.py's per-item functions
# there's no "did this get superseded mid-check" redirect logic needed here -- the caller (see
# main.py's _launch_scoped_log_item_check / _launch_scoped_log_stack_check) always lands back on
# the exact same container/stack page it started from.
# ---------------------------------------------------------------------------

def _item_progress(item_key: str) -> ProgressFunc:
    return lambda stage, done, total: check_state.set_item_progress(item_key, stage, done, total)


def run_claimed_log_item_check_now(item_key: str, container_name: str) -> None:
    """Service-scoped Check now: non-destructive, only fetches logs since this container's
    existing checkpoint -- exactly like every other Check now in the app."""
    try:
        run_log_check_for([container_name], on_progress=_item_progress(item_key))
    except Exception:
        logger.exception("Scoped log check failed unexpectedly for %s", container_name)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("logs")


def run_claimed_log_item_reset_and_recheck(item_key: str, container_name: str) -> None:
    """Service-scoped Reset & re-check: wipes this container's findings/checkpoint/cached
    overview first (db.reset_logs_data), then re-checks it -- with no checkpoint left, that
    re-scan naturally covers the full configured lookback window fresh, as if seeing this
    container for the first time."""
    try:
        db.reset_logs_data(subjects=[container_name])
        run_log_check_for([container_name], on_progress=_item_progress(item_key))
    except Exception:
        logger.exception("Scoped log reset & re-check failed unexpectedly for %s", container_name)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("logs")


def run_claimed_log_stack_check_now(item_key: str, stack_id: str) -> None:
    """Stack-scoped Check now: re-checks every current member of this stack (non-destructive),
    then runs the Cross-Service Analysis pass so the stack's blurb reflects anything that just
    changed. Member names are re-resolved fresh right before the check (not whatever the page
    had loaded), same reasoning as Updates' run_claimed_stack_check_now."""
    try:
        member_names = stacks.stack_member_names_for_logs(stack_id)
        run_log_check_for(member_names, on_progress=_item_progress(item_key))
        stacks.run_log_stack_analysis_pass(member_names)
    except Exception:
        logger.exception("Scoped log stack check failed unexpectedly for %s", stack_id)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("logs")


def run_claimed_log_stack_reset_and_recheck(item_key: str, stack_id: str) -> None:
    """Stack-scoped Reset & re-check: wipes findings/checkpoint/cached overview for every
    current member of this stack, then re-checks all of them (a fresh full-lookback scan, same
    as the service-level version), and force-regenerates the stack's Cross-Service Analysis
    blurb afterward -- same "an explicit 'start over' click must never leave the exact same
    blurb on screen" reasoning as Updates' run_claimed_stack_reset_and_recheck."""
    try:
        member_names = stacks.stack_member_names_for_logs(stack_id)
        db.reset_logs_data(subjects=member_names)
        run_log_check_for(member_names, on_progress=_item_progress(item_key))
        stacks.run_log_stack_analysis_pass(member_names, force=True)
    except Exception:
        logger.exception("Scoped log stack reset & re-check failed unexpectedly for %s", stack_id)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("logs")


def _run_log_stack_analysis_pass_safely(checked_names: list[str]) -> None:
    """Cross-Service Analysis for Logs (see stacks.run_log_stack_analysis_pass) -- a no-op
    unless the toggle is on and 2+ members of the same stack were both checked this round
    (always true for a full check). Content-hash cached internally, so calling this on every
    check (not just ones that found something new) is cheap -- it naturally no-ops whenever
    nothing about any stack's active findings has actually changed. Never fatal to the check
    itself, same "log and move on" treatment persist.py gives Updates' equivalent pass."""
    try:
        stacks.run_log_stack_analysis_pass(checked_names)
    except Exception:
        logger.exception("Log stack analysis pass failed for this check")

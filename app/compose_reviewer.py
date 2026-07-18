import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from app import ai_provider, check_state, db
from app.check_state import set_finished, set_running
from app.compose_lookup import list_compose_files, redact_compose_file_text, subject_display_name
from app.notifications import notify_compose_check_errors, notify_findings_digest
from app.summarizer import review_compose_file

logger = logging.getLogger("service_sentinel.compose_reviewer")

ProgressFunc = Optional[Callable[[str, int, int], None]]


def run_compose_check() -> dict:
    """Runs one full pass over every compose file Service Sentinel can see -- see
    run_compose_check_for below for the actual hash/review/triage logic this wraps.
    Deliberately does NOT check db.get_feature_enabled("compose") here -- see run_log_check's
    equivalent docstring note in log_watcher.py; the toggle only controls the automatic
    schedule, never the manual Check now button, and scheduler.apply_schedules() is what
    actually skips scheduling this when the feature is disabled."""
    set_running("compose")

    try:
        files = list_compose_files()
    except Exception:
        logger.exception("Could not list compose files -- skipping this compose check")
        result = {"checked": 0, "reviewed": 0, "findings_found": 0, "errors": 1, "rate_limited": 0, "cancelled": False}
        set_finished("compose", result)
        return result

    result = run_compose_check_for(
        files,
        on_progress=lambda stage, done, total: check_state.set_progress("compose", stage, done, total),
    )
    logger.info(
        "Compose check complete: %d files checked, %d reviewed, %d findings, %d errors",
        result["checked"], result["reviewed"], result["findings_found"], result["errors"],
    )
    set_finished("compose", result)
    return result


def run_compose_check_for(paths: list[Path], on_progress: ProgressFunc = None) -> dict:
    """The actual hash/review/triage pass, scoped to whichever compose files are given. A file
    that's new (never hashed before) or changed (hash differs from what's stored) gets reviewed
    by Claude; anything unchanged is skipped entirely -- this is what keeps the feature cheap
    over time, since editing a stack is infrequent. Shared by the full check (run_compose_check,
    every file Service Sentinel can see) and every scoped Check now / Reset & re-check action
    (service-level -- Compose has no stack concept, see db.reset_compose_data's docstring),
    which call this directly with just their own subset.

    Two phases: a fast sequential pass over every path (read, hash, compare -- pure local I/O,
    no network) determines which files actually changed and need a real AI review; those are
    then dispatched concurrently (capped by ai_provider.concurrency_limit(), same shape as
    persist.py's Updates pipeline and log_watcher's Logs triage) rather than one after another.
    A real-world report: a 43-file homelab took ~4 minutes for a check that's entirely local
    file I/O plus AI calls -- no external release-note fetching at all -- because every file's
    (independent) AI review waited for the previous one to finish first.

    on_progress (stage, done, total), when given, is called once upfront with (0, total) before
    anything starts, once for every file skipped in the fast pass (near-instantly, since that
    phase is pure local I/O), and once per file as its concurrent review completes -- same
    "checking_compose_files" stage throughout (no separate post-loop "triage" phase, unlike
    Logs' batched triage call) so the status badge's live "Checking compose files (N/M)…" text
    keeps working the same way at every scope.

    Does NOT call set_running/set_finished (that's the full-check-only feature-level status
    badge) -- callers that want that wrap this themselves, same as run_log_check does."""
    checked = 0
    failed: dict[str, str] = {}
    checked_ok_paths: list[str] = []
    to_review: list[tuple[str, Path, str]] = []  # (path_str, path, content_hash)
    total = len(paths)

    if on_progress and total:
        on_progress("checking_compose_files", 0, total)

    done_count = 0

    cancelled = False

    # Phase 1: fast sequential pass -- read, hash, compare against what's stored. Pure local
    # I/O, so doing this one at a time costs nothing worth parallelizing; what it produces is
    # the (usually much smaller) subset of files whose content actually changed and need the
    # real, slow, AI review below. previous_hash lookups come from one batched read upfront
    # (db.get_compose_file_hashes) rather than one connection per file in this loop -- a
    # real-world 43-file homelab check meant 43 separate connect/commit cycles here for pure
    # local I/O with no AI call anywhere nearby to justify it, same "one connection per item"
    # pattern already fixed for Updates (persist.py) and Logs (db.get/set_log_watch_checkpoints).
    hashes_by_path = db.get_compose_file_hashes([str(p) for p in paths])
    hashes_to_stamp: dict[str, str] = {}

    for path in paths:
        if check_state.is_cancel_requested("compose"):
            cancelled = True
            break
        checked += 1
        path_str = str(path)
        try:
            content = path.read_text()
        except OSError as exc:
            failed[path_str] = str(exc) or exc.__class__.__name__
            done_count += 1
            if on_progress:
                on_progress("checking_compose_files", done_count, total)
            continue

        checked_ok_paths.append(path_str)
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        previous_hash = hashes_by_path.get(path_str)
        if previous_hash == content_hash:
            done_count += 1
            if on_progress:
                on_progress("checking_compose_files", done_count, total)
            continue

        redacted = redact_compose_file_text(path)
        if redacted is None:
            hashes_to_stamp[path_str] = content_hash
            done_count += 1
            if on_progress:
                on_progress("checking_compose_files", done_count, total)
            continue

        # Deferred to the concurrent phase below -- done_count/on_progress fire there instead,
        # once this file's actual review completes, not here.
        to_review.append((path_str, redacted, content_hash))

    db.set_compose_file_hashes(hashes_to_stamp)

    # Unlike Logs (which only ever sees an incremental window of new log lines and so needs the
    # AI to explicitly judge "is this now resolved?", see analyze_logs_batch's resolved_title
    # field), a compose review sees the file's ENTIRE current content every single pass -- no AI
    # judgment call is needed here, a plain diff against what this pass's fresh review actually
    # produced is enough. A real-world report: Check Now correctly re-reviewed a file after an
    # edit (its hash genuinely changed), but a finding for whatever got fixed just stayed active
    # forever, since nothing ever cleared it -- only Reset & re-check's full wipe made it look
    # like the fix "worked." Batched (one connection for every file about to be reviewed this
    # pass), same discipline as Logs' own active_findings_by_container fetch.
    active_findings_by_path = db.get_active_findings_by_subject(
        "compose", [path_str for path_str, _, _ in to_review], include_silenced=True
    )

    reviewed = 0
    findings_found = 0
    new_findings = []
    progress_lock = threading.Lock()

    def _review_one(item: tuple[str, str, str]) -> None:
        nonlocal reviewed, findings_found, done_count, cancelled
        path_str, redacted, content_hash = item
        # Same "queued files stop, a file already being reviewed finishes naturally" semantics
        # as persist.py's _run_concurrent_phase / log_watcher's _triage_chunk. A skipped file
        # isn't recorded as an error and doesn't get its hash stamped, so it's picked back up
        # (reviewed fresh) on the very next check rather than silently treated as "seen."
        if check_state.is_cancel_requested("compose"):
            with progress_lock:
                cancelled = True
                done_count += 1
                current = done_count
            if on_progress:
                on_progress("checking_compose_files", current, total)
            return
        try:
            include_fix = db.get_deep_analysis_enabled("compose")
            findings = review_compose_file(
                path_str, redacted, include_fix=include_fix,
                active_findings=active_findings_by_path.get(path_str, []),
            )
        except Exception as exc:
            logger.exception("Compose review AI call failed for %s", path_str)
            with progress_lock:
                failed[path_str] = str(exc) or exc.__class__.__name__
                done_count += 1
                current = done_count
            if on_progress:
                on_progress("checking_compose_files", current, total)
            return

        file_findings_count = 0
        file_new_findings = []
        fresh_titles = set()
        for finding in findings:
            title = finding.get("title")
            if not title:
                continue
            fresh_titles.add(title)
            severity = finding.get("severity", "warning")
            _finding_id, is_new = db.upsert_finding(
                source="compose",
                subject=path_str,
                title=title,
                category=finding.get("category", "reliability"),
                severity=severity,
                description_markdown=finding.get("description", ""),
                suggested_fix=finding.get("fix"),
            )
            file_findings_count += 1
            if is_new:
                # Discord shows the service name(s) defined in the file, not the raw path --
                # e.g. "tautulli", not "/compose/tautulli/compose.yaml".
                file_new_findings.append({
                    "subject": subject_display_name("compose", path_str), "severity": severity, "title": title,
                })

        # Anything that was active for this file before this review but isn't in what the
        # review just produced no longer applies -- clear it now rather than leaving it to look
        # active forever (see this function's own docstring above for the real-world report).
        # active_findings_by_path now includes silenced findings too (fetched with
        # include_silenced=True so review_compose_file can match/reuse their titles -- see its
        # own docstring), but db.resolve_finding only ever deletes a status='active' row, so a
        # silenced entry that isn't in fresh_titles is safely left alone here, not deleted.
        for active in active_findings_by_path.get(path_str, []):
            if active["title"] not in fresh_titles:
                db.resolve_finding("compose", path_str, active["title"])

        db.set_compose_file_hash(path_str, content_hash)

        with progress_lock:
            reviewed += 1
            findings_found += file_findings_count
            new_findings.extend(file_new_findings)
            done_count += 1
            current = done_count
        if on_progress:
            on_progress("checking_compose_files", current, total)

    # Phase 2: the actual AI reviews, dispatched concurrently -- with 43 files needing review
    # (a real check that took ~4 minutes sequentially), this is what gets that down to roughly
    # one file's worth of AI latency times (total / concurrency_limit) instead of times total.
    rate_limited = 0
    if to_review:
        ai_provider.reset_rate_limited_count()
        max_workers = min(ai_provider.concurrency_limit(), len(to_review))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_review_one, to_review))
        rate_limited = ai_provider.rate_limited_count()

    db.clear_compose_check_errors(checked_ok_paths)
    db.record_compose_check_errors(failed)
    if failed:
        notify_compose_check_errors([{"container_name": path, "error": err} for path, err in failed.items()])

    notify_findings_digest("compose", new_findings)

    return {
        "checked": checked, "reviewed": reviewed, "findings_found": findings_found, "errors": len(failed),
        "rate_limited": rate_limited, "cancelled": cancelled,
    }


# ---------------------------------------------------------------------------
# Scoped Check now / Reset & re-check (service-level) -- same claimed-mutex shape as
# log_watcher.py's own equivalents, keyed by check_state's "compose" channel. A file's own
# identity never changes underneath a running action, so unlike persist.py's per-item Updates
# functions there's no "did this get superseded mid-check" redirect logic needed here -- the
# caller (see main.py's _launch_scoped_compose_item_check) always lands back on the exact same
# file page it started from. Compose has no stack-level scope (see db.reset_compose_data).
# ---------------------------------------------------------------------------

def _item_progress(item_key: str) -> ProgressFunc:
    return lambda stage, done, total: check_state.set_item_progress(item_key, stage, done, total)


def run_claimed_compose_item_check_now(item_key: str, path: str) -> None:
    """Service-scoped Check now: non-destructive, only reviews the file if its content hash has
    actually changed since the last successful check -- exactly like every other Check now in
    the app."""
    try:
        run_compose_check_for([Path(path)], on_progress=_item_progress(item_key))
    except Exception:
        logger.exception("Scoped compose check failed unexpectedly for %s", path)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("compose")


def run_claimed_compose_item_reset_and_recheck(item_key: str, path: str) -> None:
    """Service-scoped Reset & re-check: wipes this file's findings/checkpoint/cached overview
    first (db.reset_compose_data), then re-checks it -- with no stored content hash left, that
    re-review happens regardless of whether the file's content has actually changed, as if
    seeing this file for the first time."""
    try:
        db.reset_compose_data(subjects=[path])
        run_compose_check_for([Path(path)], on_progress=_item_progress(item_key))
    except Exception:
        logger.exception("Scoped compose reset & re-check failed unexpectedly for %s", path)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("compose")

"""Stage 3 of the ground-up rebuild: persistence. Stage 5 adds the guarded entry point that
lets automatic scheduled checks and manual UI checks share one "only one at a time" invariant.
Stage 7 adds AI summarization: a per-service summary_markdown + severity classification,
generated from Stage 6's fetched release notes plus the operator's own compose config for
that service, for the exact same set of genuinely-new/changed updates that get fresh release
notes. Deliberately no toggle for this one — it's the "single path" base tier, unlike the
optional (and separate, not yet wired up here) stack-level cross-service analysis.

Wraps reconcile.run_check() and writes its outcome into SQLite, so the Tracked Containers
table and per-update/per-stack detail pages survive restarts and get real database ids to
link to, instead of Stage 1/2's request-lifetime-only in-memory cache. Kept as its own module
rather than folded into reconcile.py, so reconcile.py stays exactly what its own docstring
promises: a pure, database-free check function.

An update record exists in the database if and only if that container currently needs
attention (a pending update or a check error) — there's no separate "resolved" flag. Once a
container catches up to the digest a record was tracking, or that record gets superseded by a
newer transition, the old row is deleted rather than marked done. This mirrors exactly what
Stage 1/2's in-memory outcome already showed on every check (current state, not a history
log) — just made durable across restarts, with at most one update row per container at a
time.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app import check_state, compose_lookup, db, reconcile, release_notes
from app.config import settings
from app.summarizer import summarize_update

logger = logging.getLogger("release_radar.persist")

_REGISTRY_ERROR_TEXT = "Could not reach the registry to check for an update."


ProgressFunc = Callable[[str, int, int], None]


def run_and_persist_check(on_progress: ProgressFunc | None = None) -> dict:
    """Runs one check and persists its outcome, unconditionally — no running-flag guard, no
    exception handling. This is the low-level building block; almost every real caller should
    use run_updates_check_if_not_running() below instead, which adds both.

    on_progress(stage, done, total), if given, is called for every phase of the pipeline — the
    registry check itself (stage="checking"), release notes fetching (stage="release_notes"),
    and AI summarization (stage="summarizing") — each skipped entirely (never announced) if
    there's nothing for that phase to do this round. See persist_check_outcome()'s docstring
    for why a phase that doesn't report progress here looks exactly like a hang."""
    reconcile_progress = (lambda done, total: on_progress("checking", done, total)) if on_progress else None
    outcome = reconcile.run_check(on_progress=reconcile_progress)
    persist_check_outcome(outcome, on_progress=on_progress)
    return outcome


def try_start_updates_check() -> bool:
    """Atomically claims the "a check is running" slot if it's free. Returns True if this
    caller now owns it and must go on to call run_claimed_updates_check(); False if a check
    was already in progress, meaning the caller should do nothing further.

    Split out from run_updates_check_if_not_running() specifically so the UI's Check now
    button can claim the slot synchronously in the request-handling thread — before the HTTP
    response is even built — so that response deterministically shows "running" instead of
    racing a background thread that might not have claimed it yet by the time the response
    renders (see main.py's _launch_check_if_not_running). The actual check work still happens
    on a background thread; only the claim itself needs to be synchronous."""
    if check_state.get_state("updates").get("running"):
        return False
    check_state.set_running("updates")
    return True


def run_claimed_updates_check() -> None:
    """Does the actual check + persist work, assuming try_start_updates_check() already
    claimed the running slot. Includes the Stage 4 safety net: even if the check fails
    partway through, check_state ends up "not running" regardless, so a single bad run (a DB
    error, a bug in a later stage's code, anything) can never wedge the app the way the
    pre-rebuild version did ("ran all night and was still checking"). A failed check just
    reports itself as failed and lets the next trigger — scheduled or manual — try again."""
    try:
        outcome = run_and_persist_check(
            on_progress=lambda stage, done, total: check_state.set_progress("updates", stage, done, total)
        )
        result = {
            "checked": len(outcome["containers"]),
            "updates_found": sum(1 for c in outcome["containers"] if c["status"] == "update_available"),
            "errors": outcome["errors"],
        }
    except Exception:
        logger.exception("Update check failed unexpectedly")
        result = {"checked": 0, "updates_found": 0, "errors": 1}

    check_state.set_finished("updates", result)


def run_updates_check_if_not_running() -> bool:
    """Combines try_start_updates_check() + run_claimed_updates_check() into one synchronous
    call — the right shape for the automatic schedule (Stage 5), which already runs on
    APScheduler's own worker thread and has no HTTP response that needs to return immediately,
    unlike the UI's Check now button. Returns True if a check actually ran, False if one was
    already in progress and this call was skipped."""
    if not try_start_updates_check():
        return False
    run_claimed_updates_check()
    return True


def persist_check_outcome(outcome: dict, on_progress: ProgressFunc | None = None, prune: bool = True) -> None:
    """Writes one check's results into container_state/updates. An empty container list is
    always treated as "the check itself didn't complete" (Docker socket unreachable, etc.)
    rather than "there are genuinely zero containers" — existing persisted state is left
    completely untouched rather than risking a wipe from a transient failure.

    Stage 6/7 add a read-fetch-summarize-write shape: first a read-only pass figures out which
    containers are genuinely new (or changed) update_available transitions, then release notes
    are fetched for just those (real GitHub API calls), then an AI summary + severity is
    generated for whichever of those actually got real notes text back (real Anthropic API
    calls) — all done with no database transaction open, each phase fanned out across its own
    thread pool via _run_concurrent_phase() (settings.ai_summarize_concurrency caps both; kept
    lower than the registry check concurrency since these are meaningfully more expensive,
    rate-limited calls) — and only then does the actual write batch run. This keeps the "one
    transaction for the whole write batch" property described below while never holding a
    SQLite transaction open across however long a batch of GitHub/Anthropic API calls takes.
    on_progress(stage, done, total) is called once per completion during each of the two fetch
    phases (stage="release_notes", stage="summarizing"; done-count updated under a lock, safe
    to call from multiple worker threads at once — same approach as reconcile.run_check()'s
    progress callback), exactly like reconcile.run_check() already does for stage="checking" —
    each skipped entirely (never called with that stage) if there's nothing to do this round,
    so the caller never has to render a meaningless "0/0". A phase that fetches or generates
    things but never reports progress here is exactly the "hangs at N/N" bug Stage 6 initially
    reintroduced.

    Holds a single connection/transaction for the whole write batch rather than letting each
    db.py call open and commit its own — with 59 containers and up to four db.py calls each,
    that was ~200 separate connect+commit+close cycles (each a real fsync in WAL mode)
    happening silently after the progress bar already showed the check as finished, which is
    what a "hangs for several seconds after reaching N/N" report traced back to. One
    transaction also means a check that fails partway through leaves the database exactly as
    it was — readers never see a half-applied check, and there's nothing to reconcile after
    the retry the Stage 4 safety net triggers.

    prune=False skips prune_removed_containers() — required for a scoped single-container
    outcome (see run_and_persist_single_check below), since that outcome's container list
    legitimately contains just the one container being re-checked, not every tracked
    container; pruning against it would wrongly delete every other container's state."""
    containers = outcome["containers"]
    if not containers:
        return

    read_conn = db.open_conn()
    try:
        existing_by_name = {
            c["container_name"]: db.get_latest_update_for_container(c["container_name"], conn=read_conn)
            for c in containers
        }
    finally:
        read_conn.close()

    to_fetch = [c for c in containers if _is_new_or_changed_update(c, existing_by_name[c["container_name"]])]
    release_notes_by_name = _run_concurrent_phase("release_notes", to_fetch, _fetch_release_notes, on_progress)

    # Only worth summarizing a container that actually got real notes text back -- nothing to
    # summarize otherwise, and asking the model to work from an empty release-notes block
    # would just be a wasted call. Skipped entirely (not just per-container) when the API key
    # isn't configured, matching every other AI call site's own early-out, so this never logs
    # a stream of "not configured" exceptions once per new update.
    to_summarize = []
    if settings.anthropic_api_key:
        to_summarize = [
            c for c in to_fetch
            if (release_notes_by_name.get(c["container_name"]) or (None, None))[0]
        ]
    summaries_by_name = _run_concurrent_phase(
        "summarizing", to_summarize,
        lambda c: _summarize_container(c, release_notes_by_name[c["container_name"]][0]),
        on_progress,
    )

    conn = db.open_conn()
    try:
        if prune:
            db.prune_removed_containers([c["container_name"] for c in containers], conn=conn)
        for container in containers:
            name = container["container_name"]
            _persist_one(
                container, existing_by_name[name], release_notes_by_name.get(name),
                summaries_by_name.get(name), conn,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run_concurrent_phase(stage: str, containers: list[dict], worker: Callable[[dict], object],
                           on_progress: ProgressFunc | None) -> dict[str, object]:
    """Shared shape for every fan-out phase in the pipeline (release notes fetching, AI
    summarization) — a thread pool capped by settings.ai_summarize_concurrency, progress
    reported once per completion under a lock (safe from multiple worker threads at once,
    same approach reconcile.run_check() uses), and the stage never announced at all
    (on_progress is never called with this stage name) if there's nothing to do this round —
    see persist_check_outcome()'s docstring for why a phase that goes quiet without reporting
    anything looks exactly like a hang."""
    results: dict[str, object] = {}
    if not containers:
        return results

    total = len(containers)
    if on_progress:
        on_progress(stage, 0, total)

    progress_lock = threading.Lock()
    done_count = 0

    def _call_and_report(container: dict):
        nonlocal done_count
        result = worker(container)
        if on_progress:
            with progress_lock:
                done_count += 1
                current = done_count
            on_progress(stage, current, total)
        return container["container_name"], result

    max_workers = min(settings.ai_summarize_concurrency, total)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for name, result in pool.map(_call_and_report, containers):
            results[name] = result
    return results


def _is_new_or_changed_update(container: dict, existing) -> bool:
    """True only for a container that actually needs release notes fetched: it currently has
    an update available, and either nothing was recorded for it before or what's recorded no
    longer matches (a newer update superseded it). Unchanged from the last check, or not an
    update at all, both return False so notes are never re-fetched for the same transition."""
    if container["status"] != "update_available":
        return False
    existing_unchanged = (
        existing is not None
        and existing["old_digest"] == container.get("current_digest")
        and existing["new_digest"] == container.get("latest_digest")
        and existing["error"] is None
    )
    return not existing_unchanged


def _fetch_release_notes(container: dict) -> tuple[str | None, str | None]:
    try:
        return release_notes.get_release_notes(
            container["image_repo"], container["tag"],
            source_override=container.get("source_override"),
            changelog_url_override=container.get("changelog_url_override"),
        )
    except Exception:
        logger.exception("Release notes fetch failed for %s", container["container_name"])
        return None, None


def _summarize_container(container: dict, release_notes_raw: str) -> tuple[str, str] | None:
    """Returns (summary_markdown, severity), or None on any failure -- a failed summarization
    is never fatal to the check itself; _persist_one() below just falls back to storing no
    summary, and the detail page already falls back to showing the raw release notes whenever
    summary_markdown is empty (Stage 6), so the operator still sees real content either way."""
    try:
        compose_config = compose_lookup.find_service_config(container["container_name"])
        return summarize_update(
            container_name=container["container_name"],
            image_repo=container["image_repo"],
            old_tag_or_digest=container.get("current_digest"),
            new_tag_or_digest=container.get("latest_digest"),
            release_notes=release_notes_raw,
            compose_config=compose_config,
        )
    except Exception:
        logger.exception("AI summarization failed for %s", container["container_name"])
        return None


def _persist_one(container: dict, existing, release_notes_result, summary_result, conn) -> None:
    name = container["container_name"]
    image_repo = container["image_repo"]
    tag = container["tag"]
    status = container["status"]
    current_digest = container.get("current_digest")
    latest_digest = container.get("latest_digest")

    db.upsert_container_state(name, image_repo, tag, current_digest, conn=conn)

    if status == "up_to_date":
        if existing is not None:
            db.delete_update(existing["id"], conn=conn)
        return

    error_text = _REGISTRY_ERROR_TEXT if status == "error" else None
    unchanged = (
        existing is not None
        and existing["old_digest"] == current_digest
        and existing["new_digest"] == latest_digest
        and (existing["error"] is not None) == (error_text is not None)
    )
    if unchanged:
        return

    release_notes_raw, source_url = release_notes_result if release_notes_result else (None, None)
    summary_markdown, severity = summary_result if summary_result else (None, "")

    if existing is not None:
        db.delete_update(existing["id"], conn=conn)
    db.record_update(
        container_name=name, image_repo=image_repo, tag=tag,
        old_digest=current_digest, new_digest=latest_digest,
        summary_markdown=summary_markdown, source_url=source_url,
        error=error_text, severity=severity,
        release_notes_raw=release_notes_raw,
        conn=conn,
    )


# ---------------------------------------------------------------------------
# Scoped single-container re-check — backs the per-update "Check now" and "Reset & re-check"
# buttons (Stage 6). Both share the exact same "only one check at a time" mutex as the full
# check above (claimed via try_start_updates_check()) so neither can ever run concurrently
# with a full check and race on the same rows, but must NOT overwrite the full check's
# "Last checked: N checked, M found" summary with its own single-container result — see
# check_state.release_running() vs set_finished() for how that's kept separate.
# ---------------------------------------------------------------------------

def run_and_persist_single_check(container_name: str, on_progress: ProgressFunc | None = None) -> dict:
    """Single-container equivalent of run_and_persist_check() — re-checks just container_name
    against the registry and, if it's a genuinely new/changed update, fetches its release
    notes and generates an AI summary fresh, exactly like a full check would for that one
    container. Non-destructive: the row is only touched if the digest actually moved, exactly
    like every other "Check now" in the app (see run_and_persist_single_reset_and_check below
    for the force-fresh variant). prune=False on the persist call below is load-bearing: this
    outcome's container list is deliberately just the one container, and pruning against a
    1-item list would delete every other tracked container's state."""
    reconcile_progress = (lambda done, total: on_progress("checking", done, total)) if on_progress else None
    outcome = reconcile.run_check_one(container_name, on_progress=reconcile_progress)
    persist_check_outcome(outcome, on_progress=on_progress, prune=False)
    return outcome


def run_and_persist_single_reset_and_check(container_name: str, on_progress: ProgressFunc | None = None) -> dict:
    """The per-item "Reset & re-check" button's real behavior: deletes this container's
    existing update row first (if any), then runs the same scoped check as
    run_and_persist_single_check() above. Deleting first is what makes this force a fresh
    release notes fetch and AI summary even when the digest hasn't actually changed since the
    last check (_is_new_or_changed_update() in persist_check_outcome() only fetches for a
    container with no existing row, or one whose recorded digests don't match) — useful for
    retrying a notes fetch or summary that failed without waiting for the image to genuinely
    update again. Mirrors the global Reset & re-check's "wipe history, then check" shape,
    scoped to one container.

    Deliberately does NOT also wipe this container's container_state row the way the global
    button's db.reset_updates_data() wipes that whole table: container_state is just a
    persisted display cache, re-upserted fresh on every check regardless of its prior
    contents, so clearing it first would only risk the container briefly vanishing from the
    Tracked Containers list mid-recheck for no functional benefit."""
    existing = db.get_latest_update_for_container(container_name)
    if existing is not None:
        db.delete_update(existing["id"])
    return run_and_persist_single_check(container_name, on_progress=on_progress)


def run_claimed_single_check(item_key: str, container_name: str) -> None:
    """Does the actual scoped check + persist work, assuming the caller already claimed the
    shared running slot via try_start_updates_check(). Mirrors run_claimed_updates_check()'s
    Stage 4 safety net (a failed scoped check still releases the mutex, never wedges future
    checks) but reports progress on the item's own channel and releases the shared mutex
    without touching the feature-level last_result — see check_state.py."""
    try:
        run_and_persist_single_check(
            container_name,
            on_progress=lambda stage, done, total: check_state.set_item_progress(item_key, stage, done, total),
        )
    except Exception:
        logger.exception("Scoped re-check failed unexpectedly for %s", container_name)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("updates")


def run_claimed_single_reset_and_check(item_key: str, container_name: str) -> None:
    """Reset & re-check's counterpart to run_claimed_single_check() above — identical shape
    and safety net, wired to the force-fresh variant instead."""
    try:
        run_and_persist_single_reset_and_check(
            container_name,
            on_progress=lambda stage, done, total: check_state.set_item_progress(item_key, stage, done, total),
        )
    except Exception:
        logger.exception("Scoped reset & re-check failed unexpectedly for %s", container_name)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("updates")


# ---------------------------------------------------------------------------
# Regenerate AI Response (Stage 7) — re-runs summarization for an update's already-stored
# release_notes_raw in place: no registry check, no fresh notes fetch, just a new attempt at
# turning the same notes into a summary + severity. Shares run_claimed_single_check's exact
# (item_key, container_name) -> None shape, so main.py can launch it through the very same
# _launch_scoped_check helper, and the poll route's "look the container back up by name, land
# on whatever id it currently has" redirect logic works unmodified here too — the id never
# actually changes for this action, it just resolves back to itself.
# ---------------------------------------------------------------------------

def run_and_persist_regenerate_summary(container_name: str) -> bool:
    """Returns True if a summary was actually regenerated, False if there was nothing to
    regenerate from (no release_notes_raw stored, e.g. notes were never found -- the button
    itself is disabled in the UI for that case, this is the server-side backstop) or nothing
    is currently tracked as pending for this container at all (resolved between click and
    background execution). A failed AI call is treated the same as _summarize_container()
    treats one everywhere else: logged, update left exactly as it was."""
    existing = db.get_latest_update_for_container(container_name)
    if existing is None or not existing["release_notes_raw"]:
        return False

    container = {
        "container_name": container_name, "image_repo": existing["image_repo"],
        "current_digest": existing["old_digest"], "latest_digest": existing["new_digest"],
    }
    result = _summarize_container(container, existing["release_notes_raw"])
    if result is None:
        return False

    summary_markdown, severity = result
    db.update_existing_update(existing["id"], summary_markdown, severity, existing["error"], existing["source_url"])
    return True


def run_claimed_regenerate_summary(item_key: str, container_name: str) -> None:
    """Same claimed-mutex shape as run_claimed_single_check -- just one AI call rather than a
    multi-container fan-out, so progress is reported as a single (0, 1) -> (1, 1) step under
    stage="regenerating" (see main.py's _STAGE_LABELS) purely so the button's spinner text
    says something meaningful rather than falling back to the generic "Checking…"."""
    check_state.set_item_progress(item_key, "regenerating", 0, 1)
    try:
        run_and_persist_regenerate_summary(container_name)
    except Exception:
        logger.exception("Regenerate AI Response failed unexpectedly for %s", container_name)
    finally:
        check_state.set_item_progress(item_key, "regenerating", 1, 1)
        check_state.finish_item(item_key)
        check_state.release_running("updates")

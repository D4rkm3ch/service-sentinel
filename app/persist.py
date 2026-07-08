"""Stage 3 of the ground-up rebuild: persistence. Stage 5 adds the guarded entry point that
lets automatic scheduled checks and manual UI checks share one "only one at a time" invariant.

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

from app import check_state, db, reconcile, release_notes
from app.config import settings

logger = logging.getLogger("release_radar.persist")

_REGISTRY_ERROR_TEXT = "Could not reach the registry to check for an update."


ProgressFunc = Callable[[str, int, int], None]


def run_and_persist_check(on_progress: ProgressFunc | None = None) -> dict:
    """Runs one check and persists its outcome, unconditionally — no running-flag guard, no
    exception handling. This is the low-level building block; almost every real caller should
    use run_updates_check_if_not_running() below instead, which adds both.

    on_progress(stage, done, total), if given, is called for both phases of the pipeline — the
    registry check itself (stage="checking") and release notes fetching (stage="release_notes",
    skipped entirely if nothing new needs notes) — see persist_check_outcome()'s docstring for
    why a phase that doesn't report progress here looks exactly like a hang."""
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

    Stage 6 adds a read-fetch-write shape: first a read-only pass figures out which
    containers are genuinely new (or changed) update_available transitions, then release
    notes are fetched for just those — real network calls, done with no database transaction
    open, fanned out across a thread pool exactly like reconcile.run_check() already does for
    registry checks (settings.ai_summarize_concurrency caps it; kept lower than the registry
    check concurrency since GitHub API calls are meaningfully more expensive and rate-limited)
    — and only then does the actual write batch run. This keeps the "one transaction for the
    whole write batch" property described below while never holding a SQLite transaction open
    across however long a batch of GitHub API calls takes.
    on_progress(stage, done, total) is called with stage="release_notes" once per fetch during
    that phase (done-count updated under a lock, safe to call from multiple worker threads at
    once — same approach as reconcile.run_check()'s progress callback), exactly like
    reconcile.run_check() already does for stage="checking" — skipped entirely (never called
    with this stage) if nothing needs notes this round, so the caller never has to render a
    meaningless "0/0". A phase that fetches things but never reports progress here is exactly
    the "hangs at N/N" bug this stage previously reintroduced.

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
    release_notes_by_name = {}
    if to_fetch:
        total = len(to_fetch)
        if on_progress:
            on_progress("release_notes", 0, total)

        progress_lock = threading.Lock()
        done_count = 0

        def _fetch_and_report(container: dict) -> tuple[str, tuple[str | None, str | None]]:
            nonlocal done_count
            result = _fetch_release_notes(container)
            if on_progress:
                with progress_lock:
                    done_count += 1
                    current = done_count
                on_progress("release_notes", current, total)
            return container["container_name"], result

        max_workers = min(settings.ai_summarize_concurrency, total)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for name, result in pool.map(_fetch_and_report, to_fetch):
                release_notes_by_name[name] = result

    conn = db.open_conn()
    try:
        if prune:
            db.prune_removed_containers([c["container_name"] for c in containers], conn=conn)
        for container in containers:
            name = container["container_name"]
            _persist_one(
                container, existing_by_name[name], release_notes_by_name.get(name), conn,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def _persist_one(container: dict, existing, release_notes_result, conn) -> None:
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

    if existing is not None:
        db.delete_update(existing["id"], conn=conn)
    db.record_update(
        container_name=name, image_repo=image_repo, tag=tag,
        old_digest=current_digest, new_digest=latest_digest,
        summary_markdown=None, source_url=source_url,
        error=error_text, severity="",
        release_notes_raw=release_notes_raw,
        conn=conn,
    )


# ---------------------------------------------------------------------------
# Scoped single-container re-check — backs the per-update "Reset & re-check" button (Stage 6).
# Shares the exact same "only one check at a time" mutex as the full check above (claimed via
# try_start_updates_check()) so it can never run concurrently with a full check and race on
# the same rows, but must NOT overwrite the full check's "Last checked: N checked, M found"
# summary with its own single-container result — see check_state.release_running() vs
# set_finished() for how that's kept separate.
# ---------------------------------------------------------------------------

def run_and_persist_single_check(container_name: str, on_progress: ProgressFunc | None = None) -> dict:
    """Single-container equivalent of run_and_persist_check() — re-checks just container_name
    against the registry and, if it's a genuinely new/changed update, fetches its release
    notes fresh, exactly like a full check would for that one container. prune=False on the
    persist call below is load-bearing: this outcome's container list is deliberately just the
    one container, and pruning against a 1-item list would delete every other tracked
    container's state."""
    reconcile_progress = (lambda done, total: on_progress("checking", done, total)) if on_progress else None
    outcome = reconcile.run_check_one(container_name, on_progress=reconcile_progress)
    persist_check_outcome(outcome, on_progress=on_progress, prune=False)
    return outcome


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

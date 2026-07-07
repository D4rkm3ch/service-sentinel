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
from typing import Callable

from app import check_state, db, reconcile

logger = logging.getLogger("release_radar.persist")

_REGISTRY_ERROR_TEXT = "Could not reach the registry to check for an update."


def run_and_persist_check(on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Runs one check and persists its outcome, unconditionally — no running-flag guard, no
    exception handling. This is the low-level building block; almost every real caller should
    use run_updates_check_if_not_running() below instead, which adds both."""
    outcome = reconcile.run_check(on_progress=on_progress)
    persist_check_outcome(outcome)
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
            on_progress=lambda done, total: check_state.set_progress("updates", done, total)
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


def persist_check_outcome(outcome: dict) -> None:
    """Writes one check's results into container_state/updates. An empty container list is
    always treated as "the check itself didn't complete" (Docker socket unreachable, etc.)
    rather than "there are genuinely zero containers" — existing persisted state is left
    completely untouched rather than risking a wipe from a transient failure.

    Holds a single connection/transaction for the whole batch rather than letting each db.py
    call open and commit its own — with 59 containers and up to four db.py calls each, that
    was ~200 separate connect+commit+close cycles (each a real fsync in WAL mode) happening
    silently after the progress bar already showed the check as finished, which is what a
    "hangs for several seconds after reaching N/N" report traced back to. One transaction also
    means a check that fails partway through leaves the database exactly as it was — readers
    never see a half-applied check, and there's nothing to reconcile after the retry the Stage
    4 safety net triggers."""
    containers = outcome["containers"]
    if not containers:
        return

    conn = db.open_conn()
    try:
        db.prune_removed_containers([c["container_name"] for c in containers], conn=conn)
        for container in containers:
            _persist_one(container, conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _persist_one(container: dict, conn) -> None:
    name = container["container_name"]
    image_repo = container["image_repo"]
    tag = container["tag"]
    status = container["status"]
    current_digest = container.get("current_digest")
    latest_digest = container.get("latest_digest")

    db.upsert_container_state(name, image_repo, tag, current_digest, conn=conn)

    existing = db.get_latest_update_for_container(name, conn=conn)

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

    if existing is not None:
        db.delete_update(existing["id"], conn=conn)
    db.record_update(
        container_name=name, image_repo=image_repo, tag=tag,
        old_digest=current_digest, new_digest=latest_digest,
        summary_markdown=None, source_url=None,
        error=error_text, severity="",
        conn=conn,
    )

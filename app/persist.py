"""Stage 3 of the ground-up rebuild: persistence.

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

from typing import Callable

from app import db, reconcile

_REGISTRY_ERROR_TEXT = "Could not reach the registry to check for an update."


def run_and_persist_check(on_progress: Callable[[int, int], None] | None = None) -> dict:
    """The one shared entry point for actually running a check — used by both the UI's Check
    now / Reset & re-check and the Dockhand webhook, so every real check (whichever triggered
    it) updates persisted state the same way."""
    outcome = reconcile.run_check(on_progress=on_progress)
    persist_check_outcome(outcome)
    return outcome


def persist_check_outcome(outcome: dict) -> None:
    """Writes one check's results into container_state/updates. An empty container list is
    always treated as "the check itself didn't complete" (Docker socket unreachable, etc.)
    rather than "there are genuinely zero containers" — existing persisted state is left
    completely untouched rather than risking a wipe from a transient failure."""
    containers = outcome["containers"]
    if not containers:
        return

    db.prune_removed_containers([c["container_name"] for c in containers])
    for container in containers:
        _persist_one(container)


def _persist_one(container: dict) -> None:
    name = container["container_name"]
    image_repo = container["image_repo"]
    tag = container["tag"]
    status = container["status"]
    current_digest = container.get("current_digest")
    latest_digest = container.get("latest_digest")

    db.upsert_container_state(name, image_repo, tag, current_digest)

    existing = db.get_latest_update_for_container(name)

    if status == "up_to_date":
        if existing is not None:
            db.delete_update(existing["id"])
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
        db.delete_update(existing["id"])
    db.record_update(
        container_name=name, image_repo=image_repo, tag=tag,
        old_digest=current_digest, new_digest=latest_digest,
        summary_markdown=None, source_url=None,
        error=error_text, severity="",
    )

"""Stage 2 of the ground-up rebuild: concurrent registry checks.

Lists tracked Docker containers, then checks each one's registry digest in parallel via a
thread pool (registry checks are almost pure network wait, so this is the one part of the
pipeline worth parallelizing). No AI anywhere in this file, and still no persistence *here* —
every call re-checks everything from scratch against the real Docker socket and real
registries. This module stays a pure, database-free function on purpose (see app/persist.py,
which wraps it for Stage 3) so it stays simple to test by mocking Docker/registry calls and
asserting on the returned dict, with no DB side effects to also mock or reason about. Listing
containers itself stays a single sequential call to the Docker socket; only the per-container
registry lookups are fanned out.

This file will grow one capability at a time in later stages (release notes, AI summarization,
notifications, deduplication, stacks) — each introduced and tested in isolation, so if
something is ever slow or breaks, we know exactly which piece did it.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from app.config import settings
from app.docker_client import TrackedContainer, list_tracked_containers
from app.registry import get_latest_digest

logger = logging.getLogger("release_radar.reconcile")


def _check_one(container: TrackedContainer) -> dict:
    try:
        latest_digest = get_latest_digest(container.image_repo, container.tag)
    except Exception:
        logger.exception("Registry check failed for %s", container.name)
        latest_digest = None

    if latest_digest is None:
        status = "error"
    elif container.current_digest is not None and latest_digest != container.current_digest:
        status = "update_available"
    else:
        status = "up_to_date"

    return {
        "container_name": container.name,
        "image_repo": container.image_repo,
        "tag": container.tag,
        "status": status,
        "current_digest": container.current_digest,
        "latest_digest": latest_digest,
        "source_override": container.source_override,
        "changelog_url_override": container.changelog_url_override,
    }


def run_check(on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Returns {"containers": [...], "errors": int, "checked_at": iso timestamp}.

    Each entry in "containers" is a plain dict: container_name, image_repo, tag, status (one
    of "update_available", "up_to_date", "error"), current_digest (what's actually running,
    per Docker inspect), latest_digest (what the registry currently serves for that tag, or
    None if the check failed), and the two releaseradar.* label overrides (source,
    changelog_url) as plain strings or None. All of these exist here purely for app/persist.py
    (Stage 3) and app/release_notes.py (Stage 6) to use — this module itself does nothing with
    them beyond reading them off the container; no severity, no AI, no history of what was
    seen before. This module still only ever answers "what does a fresh check show right now."

    If given, on_progress(done, total) is called once with (0, total) right after the
    container list is known, then again after each container finishes — safe to call from
    multiple worker threads at once, since the done-count itself is updated under a lock
    before firing the callback. Purely a UI hook (the "Checking (N/59)" progress text) — the
    check's own result doesn't depend on it, and callers that don't need live progress can
    just leave it as None."""
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        containers = list_tracked_containers()
    except Exception:
        logger.exception("Could not reach the Docker socket")
        return {"containers": [], "errors": 1, "checked_at": checked_at}

    if not containers:
        logger.info("Check complete: 0 containers checked, 0 errors")
        if on_progress:
            on_progress(0, 0)
        return {"containers": [], "errors": 0, "checked_at": checked_at}

    total = len(containers)
    if on_progress:
        on_progress(0, total)

    progress_lock = threading.Lock()
    done_count = 0

    def _check_and_report(container: TrackedContainer) -> dict:
        nonlocal done_count
        result = _check_one(container)
        if on_progress:
            with progress_lock:
                done_count += 1
                current = done_count
            on_progress(current, total)
        return result

    max_workers = min(settings.registry_check_concurrency, total)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_check_and_report, containers))

    errors = sum(1 for r in results if r["status"] == "error")

    logger.info(
        "Check complete: %d containers checked, %d errors (concurrency=%d)",
        len(results), errors, max_workers,
    )
    return {"containers": results, "errors": errors, "checked_at": checked_at}


def run_check_one(container_name: str, on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Same shape and semantics as run_check() above, scoped to a single already-tracked
    container by name — backs the per-update "Reset & re-check" button (Stage 6), which needs
    to re-check just the one container a user clicked into rather than the whole fleet.
    Deliberately its own function rather than run_check(container_names=[...]) so run_check
    itself stays untouched and every existing test/caller of it is unaffected.

    Returns {"containers": [], "errors": 1, ...} if the named container isn't found (removed
    since the page was loaded) — same "couldn't check anything" shape run_check() returns for
    an unreachable Docker socket, so callers can treat both the same way."""
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        containers = list_tracked_containers()
    except Exception:
        logger.exception("Could not reach the Docker socket")
        return {"containers": [], "errors": 1, "checked_at": checked_at}

    container = next((c for c in containers if c.name == container_name), None)
    if container is None:
        logger.warning("Scoped re-check requested for %s but it's no longer tracked", container_name)
        return {"containers": [], "errors": 1, "checked_at": checked_at}

    if on_progress:
        on_progress(0, 1)
    result = _check_one(container)
    if on_progress:
        on_progress(1, 1)

    errors = 1 if result["status"] == "error" else 0
    return {"containers": [result], "errors": errors, "checked_at": checked_at}


def run_check_many(container_names: list[str], on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Same shape and semantics as run_check() above, scoped to a set of already-tracked
    containers by name -- backs the stack-level "Reset & re-check" button, which needs to
    re-check every service in one compose stack without touching the other tracked containers.
    Container names not currently tracked (removed since the page was loaded) are silently
    skipped rather than counted as errors, same as run_check_one's "not found" handling would
    suggest, just without failing the whole batch over one missing member."""
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        all_containers = list_tracked_containers()
    except Exception:
        logger.exception("Could not reach the Docker socket")
        return {"containers": [], "errors": 1, "checked_at": checked_at}

    name_set = set(container_names)
    containers = [c for c in all_containers if c.name in name_set]
    if not containers:
        return {"containers": [], "errors": 0, "checked_at": checked_at}

    total = len(containers)
    if on_progress:
        on_progress(0, total)

    progress_lock = threading.Lock()
    done_count = 0

    def _check_and_report(container: TrackedContainer) -> dict:
        nonlocal done_count
        result = _check_one(container)
        if on_progress:
            with progress_lock:
                done_count += 1
                current = done_count
            on_progress(current, total)
        return result

    max_workers = min(settings.registry_check_concurrency, total)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_check_and_report, containers))

    errors = sum(1 for r in results if r["status"] == "error")
    return {"containers": results, "errors": errors, "checked_at": checked_at}

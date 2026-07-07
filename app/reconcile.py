"""Stage 2 of the ground-up rebuild: concurrent registry checks.

Lists tracked Docker containers, then checks each one's registry digest in parallel via a
thread pool (registry checks are almost pure network wait, so this is the one part of the
pipeline worth parallelizing). Still no persistence and no AI anywhere in this file — every
call re-checks everything from scratch against the real Docker socket and real registries;
nothing persists between calls. Listing containers itself stays a single sequential call to
the Docker socket; only the per-container registry lookups are fanned out.

This file will grow one capability at a time in later stages (persistence, release notes, AI
summarization, notifications, deduplication, stacks) — each introduced and tested in
isolation, so if something is ever slow or breaks, we know exactly which piece did it.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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
    }


def run_check() -> dict:
    """Returns {"containers": [...], "errors": int, "checked_at": iso timestamp}.

    Each entry in "containers" is a plain dict: container_name, image_repo, tag, and status
    (one of "update_available", "up_to_date", "error"). That's genuinely everything we know
    at this stage — no severity, no release notes, no history of what was seen before."""
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        containers = list_tracked_containers()
    except Exception:
        logger.exception("Could not reach the Docker socket")
        return {"containers": [], "errors": 1, "checked_at": checked_at}

    if not containers:
        logger.info("Check complete: 0 containers checked, 0 errors")
        return {"containers": [], "errors": 0, "checked_at": checked_at}

    max_workers = min(settings.registry_check_concurrency, len(containers))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_check_one, containers))

    errors = sum(1 for r in results if r["status"] == "error")

    logger.info(
        "Check complete: %d containers checked, %d errors (concurrency=%d)",
        len(results), errors, max_workers,
    )
    return {"containers": results, "errors": errors, "checked_at": checked_at}

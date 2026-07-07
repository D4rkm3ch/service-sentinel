"""Stage 1 of the ground-up rebuild: bare-minimum update checking.

Lists tracked Docker containers and checks each one's registry digest, synchronously and one
container at a time — no concurrency yet (that's Stage 2). Nothing is written to a database
and no AI is involved anywhere in this file. Every call re-checks everything from scratch
against the real Docker socket and real registries; nothing persists between calls.

This file will grow one capability at a time in later stages (concurrency, persistence,
release notes, AI summarization, notifications, deduplication, stacks) — each introduced and
tested in isolation, so if something is ever slow or breaks, we know exactly which piece did it.
"""

import logging
from datetime import datetime, timezone

from app.docker_client import list_tracked_containers
from app.registry import get_latest_digest

logger = logging.getLogger("release_radar.reconcile")


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

    results = []
    errors = 0
    for container in containers:
        try:
            latest_digest = get_latest_digest(container.image_repo, container.tag)
        except Exception:
            logger.exception("Registry check failed for %s", container.name)
            latest_digest = None

        if latest_digest is None:
            errors += 1
            status = "error"
        elif container.current_digest is not None and latest_digest != container.current_digest:
            status = "update_available"
        else:
            status = "up_to_date"

        results.append({
            "container_name": container.name,
            "image_repo": container.image_repo,
            "tag": container.tag,
            "status": status,
        })

    logger.info("Check complete: %d containers checked, %d errors", len(results), errors)
    return {"containers": results, "errors": errors, "checked_at": checked_at}

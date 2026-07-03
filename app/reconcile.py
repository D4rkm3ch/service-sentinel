import logging

from app import db
from app.compose_lookup import find_service_config
from app.docker_client import list_tracked_containers
from app.registry import get_latest_digest
from app.release_notes import get_release_notes
from app.summarizer import summarize_update

logger = logging.getLogger("release_radar.reconcile")


def run_check() -> dict:
    """Runs one full pass: list containers, check each against its registry, and for
    anything new, fetch notes + summarize + record. Returns a small summary dict for
    logging / the manual-trigger endpoint."""
    checked = 0
    updates_found = 0
    errors = 0

    try:
        containers = list_tracked_containers()
    except Exception:
        logger.exception("Could not reach the Docker socket — skipping this check")
        return {"checked": 0, "updates_found": 0, "errors": 1}

    for container in containers:
        checked += 1
        try:
            latest_digest = get_latest_digest(container.image_repo, container.tag)
        except Exception:
            logger.exception("Registry check failed for %s", container.name)
            errors += 1
            continue

        if latest_digest is None:
            # Couldn't resolve a digest from the registry (auth issue, unsupported
            # registry, network blip). Leave existing state alone and try again next cycle.
            continue

        previous_state = db.get_container_state(container.name)
        already_notified_digest = previous_state["last_seen_digest"] if previous_state else None

        # The real comparison is against what's actually running, not just our last check —
        # this catches updates that were already pending the very first time we ever see a
        # container, not just ones that land after that point.
        update_available = (
            container.current_digest is not None and latest_digest != container.current_digest
        )

        if not update_available:
            # Running digest matches the registry: fully up to date. Reset our tracking so a
            # future new digest gets treated as fresh, not compared against a stale record.
            db.upsert_container_state(container.name, container.image_repo, container.tag, latest_digest)
            continue

        if latest_digest == already_notified_digest:
            # Same pending update we already told them about — don't re-notify every cycle,
            # just move on.
            continue

        # A new update, either just-detected or the registry moved again since we last notified.
        updates_found += 1
        _handle_update(container, container.current_digest, latest_digest)
        db.upsert_container_state(container.name, container.image_repo, container.tag, latest_digest)

    logger.info("Check complete: %d containers checked, %d updates found, %d errors", checked, updates_found, errors)
    return {"checked": checked, "updates_found": updates_found, "errors": errors}


def _handle_update(container, old_digest: str | None, new_digest: str | None) -> None:
    notes, source_url = get_release_notes(
        image_repo=container.image_repo,
        tag=container.tag,
        source_override=container.source_override,
        changelog_url_override=container.changelog_url_override,
    )
    compose_config = find_service_config(container.name)

    if not notes:
        db.record_update(
            container_name=container.name,
            image_repo=container.image_repo,
            tag=container.tag,
            old_digest=old_digest,
            new_digest=new_digest,
            summary_markdown=None,
            source_url=source_url,
            error="Couldn't find release notes automatically. Check manually, or set the "
            "'releaseradar.source' or 'releaseradar.changelog_url' label on this container.",
        )
        return

    try:
        summary = summarize_update(
            container_name=container.name,
            image_repo=container.image_repo,
            old_tag_or_digest=old_digest,
            new_tag_or_digest=new_digest,
            release_notes=notes,
            compose_config=compose_config,
        )
        db.record_update(
            container_name=container.name,
            image_repo=container.image_repo,
            tag=container.tag,
            old_digest=old_digest,
            new_digest=new_digest,
            summary_markdown=summary,
            source_url=source_url,
        )
    except Exception as exc:
        logger.exception("Summarization failed for %s", container.name)
        db.record_update(
            container_name=container.name,
            image_repo=container.image_repo,
            tag=container.tag,
            old_digest=old_digest,
            new_digest=new_digest,
            summary_markdown=None,
            source_url=source_url,
            error=f"Summarization failed: {exc}",
        )

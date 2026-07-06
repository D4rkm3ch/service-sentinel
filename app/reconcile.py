import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.check_state import set_finished, set_running
from app.compose_lookup import find_service_config
from app.config import settings
from app.docker_client import list_tracked_containers
from app.notifications import notify_update
from app.registry import get_latest_digest
from app.release_notes import get_release_notes
from app.summarizer import summarize_update

logger = logging.getLogger("release_radar.reconcile")


def run_check() -> dict:
    """Runs one full pass: list containers, check each against its registry, and for
    anything new, fetch notes + summarize + record. Returns a small summary dict for
    logging / the manual-trigger endpoint."""
    if not db.get_feature_enabled("updates"):
        return {"skipped": True}

    set_running("updates")
    checked = 0
    updates_found = 0
    errors = 0

    try:
        containers = list_tracked_containers()
    except Exception:
        logger.exception("Could not reach the Docker socket — skipping this check")
        result = {"checked": 0, "updates_found": 0, "errors": 1}
        set_finished("updates", result)
        return result

    # Registry checks are almost entirely network wait (DNS + TLS + auth handshake + the
    # actual request), so running them one container at a time is what makes a large stack
    # slow — the CPU work here is negligible either way. Fetch all of them concurrently, then
    # go back to handling results one at a time: that part touches SQLite, which isn't safe
    # to hit from multiple threads at once, but it's fast enough that it was never the issue.
    digest_by_container: dict[str, str | None] = {}
    error_containers: set[str] = set()
    with ThreadPoolExecutor(max_workers=settings.registry_check_concurrency) as pool:
        future_to_container = {
            pool.submit(get_latest_digest, c.image_repo, c.tag): c for c in containers
        }
        for future in as_completed(future_to_container):
            container = future_to_container[future]
            try:
                digest_by_container[container.name] = future.result()
            except Exception:
                logger.exception("Registry check failed for %s", container.name)
                error_containers.add(container.name)
                digest_by_container[container.name] = None

    for container in containers:
        checked += 1
        if container.name in error_containers:
            errors += 1
            continue

        latest_digest = digest_by_container.get(container.name)
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
    result = {"checked": checked, "updates_found": updates_found, "errors": errors}
    set_finished("updates", result)
    return result


def _handle_update(container, old_digest: str | None, new_digest: str | None) -> None:
    notes, source_url = get_release_notes(
        image_repo=container.image_repo,
        tag=container.tag,
        source_override=container.source_override,
        changelog_url_override=container.changelog_url_override,
    )
    compose_config = find_service_config(container.name)

    if not notes:
        update_id = db.record_update(
            container_name=container.name,
            image_repo=container.image_repo,
            tag=container.tag,
            old_digest=old_digest,
            new_digest=new_digest,
            summary_markdown=None,
            source_url=source_url,
            error="Couldn't find release notes automatically. Check manually, or set the "
            "'releaseradar.source' or 'releaseradar.changelog_url' label on this container.",
            severity="action_needed",
        )
        notify_update(container.name, container.image_repo, container.tag, update_id, "action_needed",
                       error="Couldn't find release notes automatically — check manually.")
        return

    try:
        summary, severity = summarize_update(
            container_name=container.name,
            image_repo=container.image_repo,
            old_tag_or_digest=old_digest,
            new_tag_or_digest=new_digest,
            release_notes=notes,
            compose_config=compose_config,
        )
        update_id = db.record_update(
            container_name=container.name,
            image_repo=container.image_repo,
            tag=container.tag,
            old_digest=old_digest,
            new_digest=new_digest,
            summary_markdown=summary,
            source_url=source_url,
            severity=severity,
        )
        notify_update(container.name, container.image_repo, container.tag, update_id, severity)
    except Exception as exc:
        logger.exception("Summarization failed for %s", container.name)
        update_id = db.record_update(
            container_name=container.name,
            image_repo=container.image_repo,
            tag=container.tag,
            old_digest=old_digest,
            new_digest=new_digest,
            summary_markdown=None,
            source_url=source_url,
            error=f"Summarization failed: {exc}",
            severity="action_needed",
        )
        notify_update(container.name, container.image_repo, container.tag, update_id, "action_needed",
                       error=f"Summarization failed: {exc}")

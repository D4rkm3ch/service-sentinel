"""Stage 2 of the ground-up rebuild: concurrent registry checks. Stage 11 adds deduplication:
containers sharing the exact same image:tag (a real, not hypothetical, case -- e.g. two
instances of the same *arr app, a spare/backup container mirroring a primary one) get exactly
one registry digest lookup between them instead of one each, since the registry has no idea
which of your containers is asking and would just answer the identical question twice.

Lists tracked Docker containers, then checks each *unique* image:tag's registry digest in
parallel via a thread pool (registry checks are almost pure network wait, so this is the one
part of the pipeline worth parallelizing). No AI anywhere in this file, and still no persistence
*here* — every call re-checks everything from scratch against the real Docker socket and real
registries. This module stays a pure, database-free function on purpose (see app/persist.py,
which wraps it for Stage 3) so it stays simple to test by mocking Docker/registry calls and
asserting on the returned dict, with no DB side effects to also mock or reason about. Listing
containers itself stays a single sequential call to the Docker socket; only the per-unique-image
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


def _check_one(container: TrackedContainer, latest_digest: str | None) -> dict:
    """Turns one container plus its (already-fetched, possibly shared) latest_digest into a
    result dict. Takes latest_digest as a parameter rather than fetching it itself -- see
    _fetch_latest_digests below, which fetches it once per unique image:tag and hands the same
    value to every container sharing it. status is still computed per-container: two
    containers on the same image:tag can legitimately have different current_digest values
    (e.g. one hasn't been restarted since the last update and the other has), so "is an update
    available" is never something that can itself be deduplicated, only the registry lookup
    that feeds it."""
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


def _fetch_latest_digests(containers: list[TrackedContainer],
                           on_progress: Callable[[int, int], None] | None) -> dict[str, dict]:
    """Fetches the registry's latest digest once per unique (image_repo, tag) among the given
    containers -- not once per container -- and returns every container's own result dict,
    keyed by container name, in the same shape _check_one always has. Progress still reports
    against len(containers) (the number everyone actually sees tracked), not the smaller number
    of real registry calls underneath -- each completed lookup jumps the counter by however many
    containers share that image:tag, so two containers sharing an image finishing together is
    exactly as visible as either finishing alone, never a silent gap that looks like a hang."""
    total = len(containers)
    if on_progress:
        on_progress(0, total)

    groups: dict[tuple[str, str], list[TrackedContainer]] = {}
    for c in containers:
        groups.setdefault((c.image_repo, c.tag), []).append(c)

    results: dict[str, dict] = {}
    progress_lock = threading.Lock()
    done_count = 0

    def _fetch_group(key: tuple[str, str]) -> None:
        nonlocal done_count
        image_repo, tag = key
        try:
            latest_digest = get_latest_digest(image_repo, tag)
        except Exception:
            logger.exception("Registry check failed for %s:%s", image_repo, tag)
            latest_digest = None

        members = groups[key]
        for member in members:
            results[member.name] = _check_one(member, latest_digest)

        if on_progress:
            with progress_lock:
                done_count += len(members)
                current = done_count
            on_progress(current, total)

    max_workers = min(settings.registry_check_concurrency, len(groups))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_fetch_group, groups.keys()))

    return results


def _run_checks(containers: list[TrackedContainer], checked_at: str,
                 on_progress: Callable[[int, int], None] | None) -> dict:
    """Shared body for run_check()/run_check_many() below -- both just gather a different list
    of TrackedContainer and hand it here. Preserves the input list's own order in the returned
    "containers" list regardless of which unique-image group finishes its (deduplicated,
    concurrent) registry lookup first."""
    if not containers:
        logger.info("Check complete: 0 containers checked, 0 errors")
        if on_progress:
            on_progress(0, 0)
        return {"containers": [], "errors": 0, "checked_at": checked_at}

    result_by_name = _fetch_latest_digests(containers, on_progress)
    results = [result_by_name[c.name] for c in containers]
    errors = sum(1 for r in results if r["status"] == "error")

    logger.info(
        "Check complete: %d containers checked, %d errors (%d unique image:tag)",
        len(results), errors, len({(c.image_repo, c.tag) for c in containers}),
    )
    return {"containers": results, "errors": errors, "checked_at": checked_at}


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

    return _run_checks(containers, checked_at, on_progress)


def run_check_one(container_name: str, on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Same shape and semantics as run_check() above, scoped to a single already-tracked
    container by name — backs the per-update "Reset & re-check" button (Stage 6), which needs
    to re-check just the one container a user clicked into rather than the whole fleet. Nothing
    to deduplicate against with only one container, so this stays its own simple path rather
    than routing through _run_checks()'s grouping machinery.

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
    try:
        latest_digest = get_latest_digest(container.image_repo, container.tag)
    except Exception:
        logger.exception("Registry check failed for %s", container.name)
        latest_digest = None
    result = _check_one(container, latest_digest)
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
    suggest, just without failing the whole batch over one missing member. Goes through
    _run_checks() same as run_check() -- a stack can itself contain two services on the same
    image (rarer than the whole-fleet case, but not impossible), so the same dedup applies."""
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        all_containers = list_tracked_containers()
    except Exception:
        logger.exception("Could not reach the Docker socket")
        return {"containers": [], "errors": 1, "checked_at": checked_at}

    name_set = set(container_names)
    containers = [c for c in all_containers if c.name in name_set]
    return _run_checks(containers, checked_at, on_progress)

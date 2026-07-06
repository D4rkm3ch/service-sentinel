import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import compose_lookup, db, stacks
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
    # slow — the CPU work here is negligible either way. Fetch all of them concurrently.
    #
    # Deduplicated by (image_repo, tag): containers that share an image (spare/backup
    # instances, multiple *arr copies, etc.) were each independently re-checking the exact
    # same manifest — wasted requests, and part of what was causing registry rate limiting
    # (429s) on busier registries. Check each unique image once, then apply the result to
    # every container that shares it.
    image_tag_pairs = {(c.image_repo, c.tag) for c in containers}
    digest_by_pair: dict[tuple[str, str], str | None] = {}
    error_pairs: set[tuple[str, str]] = set()
    with ThreadPoolExecutor(max_workers=settings.registry_check_concurrency) as pool:
        future_to_pair = {
            pool.submit(get_latest_digest, repo, tag): (repo, tag) for repo, tag in image_tag_pairs
        }
        for future in as_completed(future_to_pair):
            pair = future_to_pair[future]
            try:
                digest_by_pair[pair] = future.result()
            except Exception:
                logger.exception("Registry check failed for %s:%s", *pair)
                error_pairs.add(pair)
                digest_by_pair[pair] = None

    digest_by_container = {c.name: digest_by_pair.get((c.image_repo, c.tag)) for c in containers}
    error_containers = {c.name for c in containers if (c.image_repo, c.tag) in error_pairs}

    # Kick off the stack-wide cross-service analysis now, in the background — it only needs
    # container names/images/tags (never the per-container AI summary text), so it has no
    # real dependency on the summarization phase below and can run at the same time instead
    # of strictly after it. Joined at the end so the check isn't reported "done" until this
    # has genuinely finished too.
    stack_analysis_thread = None
    if db.get_deep_analysis_enabled("updates"):
        index = compose_lookup.build_stack_index()
        members_by_stack: dict[str, list] = {}
        for container in containers:
            if container.name in error_containers:
                continue
            info = compose_lookup.match_container_to_stack(container.name, index)
            if info and len(info["service_names"]) >= 2:
                members_by_stack.setdefault(info["stack_id"], []).append(container)

        def _run_stack_pass():
            try:
                stacks.run_stack_analysis_pass(members_by_stack, digest_by_container)
            except Exception:
                logger.exception("Stack analysis pass failed")

        stack_analysis_thread = threading.Thread(target=_run_stack_pass, daemon=True)
        stack_analysis_thread.start()

    # Figure out which containers actually need a fresh AI-generated summary. This is pure
    # comparison against already-fetched digests — no network or AI calls — so it stays
    # sequential; it's fast regardless of how many containers there are.
    to_process: list[tuple] = []
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
        to_process.append((container, container.current_digest, latest_digest))
        db.upsert_container_state(container.name, container.image_repo, container.tag, latest_digest)

    # Fetching release notes is the genuinely slow, expensive part (network round-trips,
    # occasionally a web search) — and it depends only on the image/tag/digest transition,
    # never on any individual container's own compose config. Containers sharing the exact
    # same image (spare/backup instances, duplicate *arr setups) were each independently
    # paying for this, including a second web search for identical content. Group by the
    # notes-determining key and fetch once per group, concurrently.
    notes_groups: dict[tuple, list] = {}
    for container, old_d, new_d in to_process:
        key = (container.image_repo, old_d, new_d, container.source_override, container.changelog_url_override)
        notes_groups.setdefault(key, []).append((container, old_d, new_d))

    notes_by_key: dict[tuple, tuple] = {}
    if notes_groups:
        def _fetch_for_key(key):
            image_repo, old_d, new_d, source_override, changelog_url_override = key
            tag = notes_groups[key][0][0].tag
            return get_release_notes(
                image_repo=image_repo, tag=tag,
                source_override=source_override, changelog_url_override=changelog_url_override,
            )

        with ThreadPoolExecutor(max_workers=settings.ai_summarize_concurrency) as pool:
            future_to_key = {pool.submit(_fetch_for_key, key): key for key in notes_groups}
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    notes_by_key[key] = future.result()
                except Exception:
                    logger.exception("Release notes fetch failed for %s", key[0])
                    notes_by_key[key] = (None, None)

    # Summarization still has to run per-container — each one's own compose config (env
    # vars, volumes, ports, labels) genuinely can differ even when the underlying release is
    # identical, and "Relevant to your Setup" is supposed to reflect that. But this step is
    # now just a single fast completion call reusing already-fetched notes, not a second
    # round of notes-fetching too.
    if to_process:
        with ThreadPoolExecutor(max_workers=settings.ai_summarize_concurrency) as pool:
            futures = []
            for key, group in notes_groups.items():
                prefetched = notes_by_key.get(key, (None, None))
                for container, old_d, new_d in group:
                    futures.append(pool.submit(_handle_update, container, old_d, new_d, prefetched))
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    logger.exception("Unexpected error while handling an update")
                    errors += 1

    if stack_analysis_thread is not None:
        stack_analysis_thread.join()

    logger.info("Check complete: %d containers checked, %d updates found, %d errors", checked, updates_found, errors)
    result = {"checked": checked, "updates_found": updates_found, "errors": errors}
    set_finished("updates", result)
    return result


def _generate_update_content(container_name: str, image_repo: str, tag: str,
                              old_digest: str | None, new_digest: str | None,
                              source_override: str | None, changelog_url_override: str | None,
                              prefetched_notes: tuple | None = None) -> dict:
    """Returns {summary_markdown, severity, error, source_url} — the shared generation path
    used both by a normal check (which inserts a new record) and a manual retry (which
    updates an existing one in place), so they can never drift out of sync with each other.

    prefetched_notes lets a caller supply an already-resolved (notes, source_url) pair —
    used when multiple containers share the exact same image/tag/digest transition, so the
    (potentially slow) notes lookup only happens once per unique combination rather than
    once per container."""
    if prefetched_notes is not None:
        notes, source_url = prefetched_notes
    else:
        notes, source_url = get_release_notes(
            image_repo=image_repo, tag=tag,
            source_override=source_override, changelog_url_override=changelog_url_override,
        )
    compose_config = find_service_config(container_name)

    if not notes:
        return {
            "summary_markdown": None, "severity": "action_needed", "source_url": source_url,
            "error": "Couldn't find release notes automatically. Check manually, or set the "
            "'releaseradar.source' or 'releaseradar.changelog_url' label on this container.",
        }

    try:
        summary, severity = summarize_update(
            container_name=container_name, image_repo=image_repo,
            old_tag_or_digest=old_digest, new_tag_or_digest=new_digest,
            release_notes=notes, compose_config=compose_config,
        )
        return {"summary_markdown": summary, "severity": severity, "source_url": source_url, "error": None}
    except Exception as exc:
        logger.exception("Summarization failed for %s", container_name)
        return {
            "summary_markdown": None, "severity": "action_needed", "source_url": source_url,
            "error": f"Summarization failed: {exc}",
        }


def _check_one_container(container) -> bool:
    """Runs the same comparison a normal check would for one container. Returns True if an
    update was found and recorded."""
    try:
        latest_digest = get_latest_digest(container.image_repo, container.tag)
    except Exception:
        logger.exception("Registry check failed for %s", container.name)
        return False

    if latest_digest is None:
        return False

    update_available = container.current_digest is not None and latest_digest != container.current_digest
    db.upsert_container_state(container.name, container.image_repo, container.tag, latest_digest)

    if not update_available:
        return False

    _handle_update(container, container.current_digest, latest_digest)
    return True


def reset_and_recheck_container(container_name: str) -> None:
    """Wipes this one container's update history and tracking baseline, then re-checks just
    it fresh — the scoped version of the main Reset & re-check button, for a single service."""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))

    for c in _safe_list_tracked_containers():
        if c.name == container_name:
            _check_one_container(c)
            return


def reset_and_recheck_stack(stack_id: str) -> None:
    """Wipes update history and tracking baselines for every member of this stack, re-checks
    each fresh, then force-regenerates the stack's cross-service analysis."""
    member_names = _stack_container_names(stack_id)
    if not member_names:
        return

    with db.get_conn() as conn:
        placeholders = ",".join("?" * len(member_names))
        conn.execute(f"DELETE FROM updates WHERE container_name IN ({placeholders})", member_names)
        conn.execute(f"DELETE FROM container_state WHERE container_name IN ({placeholders})", member_names)

    for c in _safe_list_tracked_containers():
        if c.name in member_names:
            _check_one_container(c)

    _run_stack_analysis_for_one(stack_id)


def _safe_list_tracked_containers() -> list:
    """Same graceful degradation run_check already does for a normal check — if Docker
    isn't reachable right now, log it and return nothing rather than raising, so a manual
    retry/reset action fails quietly instead of crashing the page."""
    try:
        return list_tracked_containers()
    except Exception:
        logger.exception("Could not reach the Docker socket")
        return []


def _handle_update(container, old_digest: str | None, new_digest: str | None,
                    prefetched_notes: tuple | None = None) -> None:
    content = _generate_update_content(
        container.name, container.image_repo, container.tag, old_digest, new_digest,
        container.source_override, container.changelog_url_override,
        prefetched_notes=prefetched_notes,
    )
    update_id = db.record_update(
        container_name=container.name, image_repo=container.image_repo, tag=container.tag,
        old_digest=old_digest, new_digest=new_digest,
        summary_markdown=content["summary_markdown"], source_url=content["source_url"],
        error=content["error"], severity=content["severity"],
    )
    notify_update(container.name, container.image_repo, container.tag, update_id,
                  content["severity"], error=content["error"])


def retry_update(update_id: int) -> None:
    """Regenerates an existing update record in place — used by the manual Retry button on
    an update's detail page. Reuses the exact same content-generation path as a normal check,
    just updating the existing row instead of inserting a new one."""
    update_row = db.get_update(update_id)
    if update_row is None:
        return

    source_override = None
    changelog_url_override = None
    for c in _safe_list_tracked_containers():
        if c.name == update_row["container_name"]:
            source_override = c.source_override
            changelog_url_override = c.changelog_url_override
            break

    content = _generate_update_content(
        update_row["container_name"], update_row["image_repo"], update_row["tag"],
        update_row["old_digest"], update_row["new_digest"],
        source_override, changelog_url_override,
    )
    db.update_existing_update(
        update_id,
        summary_markdown=content["summary_markdown"], severity=content["severity"],
        error=content["error"], source_url=content["source_url"],
    )


def retry_stack(stack_id: str) -> None:
    """Regenerates every update record for containers in this stack, then refreshes the
    stack's cross-service analysis — used by the manual Retry button on a stack's page."""
    for update_row in db.list_updates_for_stack_containers(_stack_container_names(stack_id)):
        retry_update(update_row["id"])
    _run_stack_analysis_for_one(stack_id)


def _stack_container_names(stack_id: str) -> list[str]:
    index = compose_lookup.build_stack_index()
    for entry in index:
        if entry["stack_id"] == stack_id:
            return entry["service_names"]
    return []


def _run_stack_analysis_for_one(stack_id: str) -> None:
    tracked = {c.name: c for c in _safe_list_tracked_containers()}
    members = [tracked[name] for name in _stack_container_names(stack_id) if name in tracked]
    if len(members) < 2:
        return
    digest_by_container = {}
    for m in members:
        try:
            digest_by_container[m.name] = get_latest_digest(m.image_repo, m.tag)
        except Exception:
            digest_by_container[m.name] = m.current_digest
    stacks.regenerate_stack_analysis(stack_id, members, digest_by_container, force=True)

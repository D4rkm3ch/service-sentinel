"""Ties together stack naming and cross-service analysis with their caching rules, so the
AI only gets called when something about a stack has actually changed — never on every
page view or every check cycle for an unchanged stack.
"""

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import compose_lookup, db
from app.config import settings
from app.summarizer import analyze_stack_impact, generate_stack_name

logger = logging.getLogger("release_radar.stacks")


def get_or_generate_stack_name(stack_id: str, service_names: list[str]) -> str:
    """Returns the stack's display name — a manual override if one's been set (never
    auto-regenerated), the cached AI name if the service list hasn't changed since it was
    generated, or a freshly generated one otherwise."""
    services_hash = hashlib.sha256(",".join(sorted(service_names)).encode()).hexdigest()[:16]
    existing = db.get_stack(stack_id)

    if existing:
        if existing["name_source"] == "manual":
            return existing["display_name"]
        if existing["services_hash"] == services_hash:
            return existing["display_name"]

    name = generate_stack_name(service_names)
    db.set_stack_name(stack_id, name, "ai", services_hash)
    return name


def rename_stack(stack_id: str, new_name: str) -> None:
    db.set_stack_name(stack_id, new_name.strip(), "manual", None)


def reset_stack_name(stack_id: str) -> None:
    db.reset_stack_name(stack_id)


def stack_member_names(stack_id: str) -> list[str]:
    """Every currently-tracked container name belonging to this compose stack, alphabetical --
    shared by the stack detail page, the rename routes, and the Retry/Reset & re-check actions
    below, all of which need the same "who's actually in this stack right now" answer."""
    index = compose_lookup.build_stack_index()
    return sorted(
        c["container_name"] for c in db.all_container_states()
        if (match := compose_lookup.match_container_to_stack(c["container_name"], index)) and match["stack_id"] == stack_id
    )


def stack_member_names_for_logs(stack_id: str) -> list[str]:
    """Logs' equivalent of stack_member_names -- every container the log watcher has ever
    checked (db.all_log_watch_states_with_status, keyed "name" rather than Updates'
    "container_name") that belongs to this compose stack, alphabetical."""
    index = compose_lookup.build_stack_index()
    return sorted(
        row["name"] for row in db.all_log_watch_states_with_status()
        if (match := compose_lookup.match_container_to_stack(row["name"], index)) and match["stack_id"] == stack_id
    )


def members_for_analysis(stack_id: str) -> list[dict]:
    """Builds the same check-outcome-shaped member dicts regenerate_stack_analysis() expects
    (container_name, image_repo, tag, current_digest, latest_digest), from whatever's currently
    persisted -- used by the stack page's Retry button and the forced post-recheck regeneration
    below, neither of which has a fresh reconcile.py outcome of its own to draw from."""
    members = []
    for name in stack_member_names(stack_id):
        container_row = db.get_container_state(name)
        if container_row is None:
            continue
        latest_update = db.get_latest_update_for_container(name)
        latest_digest = latest_update["new_digest"] if latest_update else container_row["last_seen_digest"]
        members.append({
            "container_name": name,
            "image_repo": container_row["image_repo"],
            "tag": container_row["tag"],
            "current_digest": container_row["last_seen_digest"],
            "latest_digest": latest_digest,
        })
    return members


def _group_containers_by_stack(containers: list[dict]) -> dict[str, list[dict]]:
    """Groups check-outcome container dicts (container_name, image_repo, tag, current_digest,
    latest_digest, ...) by which compose stack they belong to, keeping only stacks where 2+ of
    their services are actually present in this particular list -- a "stack" of one service has
    nothing to cross-analyze, and (for scoped checks covering just one or a few containers) a
    stack most of whose members aren't part of this check at all shouldn't get a partial,
    misleading analysis run against only the subset that happened to be checked."""
    index = compose_lookup.build_stack_index()
    groups: dict[str, list[dict]] = {}
    for c in containers:
        info = compose_lookup.match_container_to_stack(c["container_name"], index)
        if info and len(info["service_names"]) >= 2:
            groups.setdefault(info["stack_id"], []).append(c)
    return {stack_id: members for stack_id, members in groups.items() if len(members) >= 2}


def run_stack_analysis_pass(containers: list[dict], force: bool = False) -> None:
    """Called once per persisted check outcome (see persist.persist_check_outcome) — a full
    check naturally covers every stack, a stack-scoped Reset & re-check covers exactly one, and
    a single-container scoped check never has 2+ members of the same stack present so this is
    always a no-op there, with no special-casing needed to make that true (see
    _group_containers_by_stack above). Only runs at all if the Deep Analysis toggle is on —
    this is the automatic, scheduled path.

    force=True (the stack page's own Reset & re-check button, via persist.run_claimed_stack_
    reset_and_recheck) always regenerates regardless of whether the digest fingerprint actually
    moved, same "an explicit click always gets a fresh take" semantics as the Retry button
    (regenerate_stack_analysis) uses directly. Still gated behind the Deep Analysis toggle like
    everything else here -- force only means "skip the content-hash cache," not "ignore the
    opt-in setting entirely" (Retry itself bypasses the toggle by calling
    regenerate_stack_analysis directly rather than going through this function).

    Stacks are processed concurrently with each other too — with many multi-service stacks,
    processing them one at a time would just recreate the same sequential bottleneck that
    per-container summarization had."""
    if not db.get_deep_analysis_enabled("updates"):
        return

    groups = _group_containers_by_stack(containers)
    if not groups:
        return

    with ThreadPoolExecutor(max_workers=settings.ai_summarize_concurrency) as pool:
        futures = [
            pool.submit(regenerate_stack_analysis, stack_id, members, force)
            for stack_id, members in groups.items()
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                logger.exception("Stack analysis pass failed for one stack")


_MAX_NOTES_CHARS = 1000


def _build_changed_summary(members: list[dict]) -> str:
    """Builds the actual substance the AI reasons about — real release notes/summary text for
    every member with a pending update, not just their bare image:tag. Passing only image:tag
    (the original implementation) gave the model nothing concrete to reason about beyond service
    names, so it reliably fell back to generic, useless observations true of every compose stack
    ("yes, there is a network") rather than anything grounded in what actually changed. Members
    without a pending update are listed by name only — the model already has their names via
    all_service_names, and there's nothing to summarize for something that hasn't changed."""
    lines = []
    for m in members:
        name = m["container_name"]
        update = db.get_latest_update_for_container(name)
        if update is None:
            lines.append(f"- {name}: no pending update.")
            continue
        notes = (update["summary_markdown"] or update["release_notes_raw"] or "").strip()
        if not notes:
            lines.append(f"- {name} ({m['image_repo']}:{m['tag']}): update pending, no release notes available.")
            continue
        if len(notes) > _MAX_NOTES_CHARS:
            notes = notes[:_MAX_NOTES_CHARS] + "…"
        lines.append(f"- {name} ({m['image_repo']}:{m['tag']}) -- update pending. Release notes:\n{notes}")
    return "\n\n".join(lines)


def regenerate_stack_analysis(stack_id: str, members: list[dict], force: bool = False) -> None:
    """The actual cache-aware regeneration logic — for each stack with 2+ members, only calls
    the AI if the set of (member, latest known digest) pairs has actually changed since last
    time, unless force=True (manual retry always regenerates, cache or not). members are plain
    check-outcome-shaped dicts (container_name, image_repo, tag, current_digest, latest_digest)
    -- the same shape used everywhere else downstream of reconcile.py, not the TrackedContainer
    objects reconcile.py itself works with. latest_digest is preferred over current_digest for
    the fingerprint (it reflects what the registry says right now, not just what's currently
    running), falling back to current_digest only when latest_digest is unknown (e.g. a
    registry error this round) so a transient outage doesn't spuriously invalidate the cache."""
    if len(members) < 2:
        return

    service_names = [m["container_name"] for m in members]
    fingerprint_input = "|".join(
        sorted(f"{m['container_name']}:{m.get('latest_digest') or m.get('current_digest')}" for m in members)
    )
    content_hash = hashlib.sha256(fingerprint_input.encode()).hexdigest()[:16]

    if not force:
        cached = db.get_stack_analysis(stack_id)
        if cached and cached["content_hash"] == content_hash:
            return

    display_name = get_or_generate_stack_name(stack_id, service_names)
    changed_summary = _build_changed_summary(members)

    try:
        analysis = analyze_stack_impact(display_name, service_names, changed_summary)
    except Exception:
        logger.exception("Stack analysis failed for %s", stack_id)
        return

    if analysis:
        db.set_stack_analysis(stack_id, content_hash, analysis)

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


def run_stack_analysis_pass(containers: list[dict]) -> None:
    """Called once per persisted check outcome (see persist.persist_check_outcome) — a full
    check naturally covers every stack, a stack-scoped Reset & re-check covers exactly one, and
    a single-container scoped check never has 2+ members of the same stack present so this is
    always a no-op there, with no special-casing needed to make that true (see
    _group_containers_by_stack above). Only runs at all if the Deep Analysis toggle is on —
    this is the automatic, scheduled path. Manual retries (see regenerate_stack_analysis)
    bypass this toggle entirely, since clicking Retry is an explicit request regardless of the
    automatic setting.

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
            pool.submit(regenerate_stack_analysis, stack_id, members)
            for stack_id, members in groups.items()
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                logger.exception("Stack analysis pass failed for one stack")


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
    changed_summary = "\n".join(f"- {m['container_name']} ({m['image_repo']}:{m['tag']})" for m in members)

    try:
        analysis = analyze_stack_impact(display_name, service_names, changed_summary)
    except Exception:
        logger.exception("Stack analysis failed for %s", stack_id)
        return

    if analysis:
        db.set_stack_analysis(stack_id, content_hash, analysis)

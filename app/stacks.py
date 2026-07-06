"""Ties together stack naming and cross-service analysis with their caching rules, so the
AI only gets called when something about a stack has actually changed — never on every
page view or every check cycle for an unchanged stack.
"""

import hashlib
import logging

from app import compose_lookup, db
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


def run_stack_analysis_pass(members_by_stack: dict[str, list], digest_by_container: dict[str, str | None]) -> None:
    """Called once at the end of a full Updates check, after every container's own digest
    has already been resolved. For each stack with 2+ members, only regenerates the
    cross-service analysis if the set of (member, current digest) pairs has actually
    changed since last time — covers "anything in the stack changed" per how this was
    asked for, not just "multiple members happened to update at once"."""
    if not db.get_deep_analysis_enabled("updates"):
        return

    for stack_id, members in members_by_stack.items():
        if len(members) < 2:
            continue

        service_names = [m.name for m in members]
        fingerprint_input = "|".join(
            sorted(f"{m.name}:{digest_by_container.get(m.name) or m.current_digest}" for m in members)
        )
        content_hash = hashlib.sha256(fingerprint_input.encode()).hexdigest()[:16]

        cached = db.get_stack_analysis(stack_id)
        if cached and cached["content_hash"] == content_hash:
            continue

        display_name = get_or_generate_stack_name(stack_id, service_names)
        changed_summary = "\n".join(
            f"- {m.name} ({m.image_repo}:{m.tag})" for m in members
        )

        try:
            analysis = analyze_stack_impact(display_name, service_names, changed_summary)
        except Exception:
            logger.exception("Stack analysis failed for %s", stack_id)
            continue

        if analysis:
            db.set_stack_analysis(stack_id, content_hash, analysis)

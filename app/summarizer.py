import json

import anthropic

from app.ai_json import extract_json
from app.config import settings

SYSTEM_PROMPT = """You write short, practical release-note summaries for a homelab operator \
deciding whether to update a self-hosted Docker container.

Structure your response in markdown with exactly these sections:

## New features
Bullet points, plain language, most significant first. Skip internal refactors or anything \
with no user-facing effect. If nothing qualifies, write "Nothing notable."

## Breaking changes
Bullet points. Only include things that could actually break on update: removed env vars, \
changed default ports, config file format changes, deprecated volumes, required migration \
steps. If nothing qualifies, write "None found."

## Relevant to your setup
This is the most important section. Cross-reference the features and breaking changes above \
against the operator's actual compose configuration (provided below). Call out specifically \
which env vars, volumes, ports, or labels they have set are affected, and how. If nothing in \
the release touches their actual configuration, say so plainly and keep this section short — \
don't pad it.

Be concise. This is read on a dashboard, not a blog post. No preamble, no closing summary, no \
restating the version numbers."""


def summarize_update(
    container_name: str,
    image_repo: str,
    old_tag_or_digest: str | None,
    new_tag_or_digest: str | None,
    release_notes: str,
    compose_config: dict | None,
) -> str:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    compose_block = (
        json.dumps(compose_config, indent=2, default=str)
        if compose_config
        else "(no matching compose service found — general summary only, "
        "can't assess relevance to a specific config)"
    )

    user_message = f"""Container: {container_name}
Image: {image_repo}
Previous version: {old_tag_or_digest or "unknown"}
New version: {new_tag_or_digest or "unknown"}

Release notes:
---
{release_notes}
---

Operator's compose configuration for this service:
---
{compose_block}
---"""

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    return "".join(block.text for block in response.content if block.type == "text")


LOG_TRIAGE_SYSTEM_PROMPT = """You are triaging pre-filtered log excerpts from a homelab \
operator's self-hosted Docker containers. Each excerpt already only contains lines that matched \
suspicious keywords (error, exception, failed, etc.) plus a little surrounding context — most \
routine noise has already been stripped out before it reached you.

Your job: separate genuine problems from false positives. A lot of software logs the word \
"error" or "warning" for routine, expected situations (a health check retry during startup, an \
SSL renegotiation, a client disconnect) — do not report those. Only report things that indicate \
an actual problem worth a human's attention, or a clear, concrete optimization opportunity you \
can see directly in the excerpt (e.g. a container repeatedly restarting, an obvious \
misconfiguration visible in the error text).

Respond with ONLY a JSON array and nothing else — no markdown fences, no preamble. Each element:
{"container": "the container name from the excerpt's header", "title": "a short, specific title \
(under 8 words) that would let someone recognize this same issue if it recurred", "category": \
one of "error", "reliability", "optimization", "severity": one of "critical", "warning", \
"suggestion", "description": "1-3 sentences explaining what's happening and, if obvious, what to \
check or try"}

If nothing in the provided excerpts represents a real issue, respond with an empty JSON array: []"""


def analyze_logs_batch(excerpts_by_container: dict[str, str]) -> list[dict]:
    """Sends pre-filtered log excerpts (already keyword-matched locally) to Claude for triage.
    Returns a list of finding dicts, or an empty list if nothing real was found — callers
    should treat an empty list as a clean, quiet result, not an error."""
    if not settings.anthropic_api_key or not excerpts_by_container:
        return []

    sections = []
    for container_name, excerpt in excerpts_by_container.items():
        sections.append(f"=== Container: {container_name} ===\n{excerpt}")
    user_message = "\n\n".join(sections)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=2000,
        system=LOG_TRIAGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    data = extract_json(text)
    return data if isinstance(data, list) else []


COMPOSE_REVIEW_SYSTEM_PROMPT = """You are reviewing a docker-compose file from a homelab \
operator's self-hosted setup. Secret-looking values have already been redacted before you see \
this — you're reviewing structure and configuration, not credentials.

Look for:
- Security issues: unnecessarily exposed ports, containers running as root when they don't need \
to, overly permissive volume mounts (e.g. mounting the whole filesystem or the Docker socket \
read-write when read-only would do), missing resource limits that could let one container take \
down the host.
- Reliability issues: missing restart policy, missing healthchecks where they'd matter, service \
dependencies that aren't declared via depends_on.
- Optimization opportunities: redundant or unused environment variables, obviously outdated \
image-pinning practice (e.g. floating :latest on a service where that's risky), network \
misconfiguration.

Only report things with real substance — skip purely stylistic nitpicks. If the file looks fine, \
say so by returning an empty array.

Respond with ONLY a JSON array and nothing else — no markdown fences, no preamble. Each element:
{"title": "a short, specific title (under 8 words)", "category": one of "security", \
"reliability", "optimization", "severity": one of "critical", "warning", "suggestion", \
"description": "1-3 sentences explaining the issue and a concrete suggestion"}"""


def review_compose_file(file_path: str, redacted_yaml: str) -> list[dict]:
    """Sends a secret-redacted compose file to Claude for a structural review. Returns a list
    of finding dicts, or an empty list if the file looks fine."""
    if not settings.anthropic_api_key:
        return []

    user_message = f"File: {file_path}\n\n{redacted_yaml}"

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=1500,
        system=COMPOSE_REVIEW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    data = extract_json(text)
    return data if isinstance(data, list) else []

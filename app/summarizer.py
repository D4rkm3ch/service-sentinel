import json

import anthropic

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

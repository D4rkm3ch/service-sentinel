import json
import re

import anthropic

from app.ai_json import extract_json
from app.config import settings

SYSTEM_PROMPT = """You write short, practical release-note summaries for a homelab operator \
deciding whether to update a self-hosted Docker container.

Structure your response in markdown with exactly these sections:

## New Features
Plain language, most significant first. Skip internal refactors or anything with no \
user-facing effect. If nothing qualifies, write "Nothing notable."

## Breaking Changes
Only include things that could actually break on update: removed env vars, changed default \
ports, config file format changes, deprecated volumes, required migration steps. If nothing \
qualifies, write "None found."

## Relevant to your Setup
This is the most important section. Cross-reference the features and breaking changes above \
against the operator's actual compose configuration (provided below). Call out specifically \
which env vars, volumes, ports, or labels they have set are affected, and how. If nothing in \
the release touches their actual configuration, say so plainly and keep this section short — \
don't pad it.

For all three sections: use a bullet list only when there are two or more distinct points to \
make. If there's exactly one point, or none, write a plain sentence instead — a bullet list \
with a single item, or a single item padded out to look like a list, reads worse than just \
saying it.

Be concise. This is read on a dashboard, not a blog post. No preamble, no closing summary, no \
restating the version numbers.

After the three sections above, add one final line with nothing else on it, in exactly this \
format: `SEVERITY: X` where X is one of: bugfix, feature, action_needed, breaking.

Determine X using this exact order — stop at the first line that applies, don't judge it \
separately from what you already wrote above:
1. breaking — the Breaking Changes section above says anything other than "None found."
2. action_needed — the Relevant to your Setup section above concludes the operator must \
actually change something in their own configuration (an env var, a volume, a port, a label) \
for this update to work correctly, or to keep working the same way. This is not for optional \
new configuration they could choose to use — only for something they must do.
3. feature — New Features above has real content (not "Nothing notable"), and neither of the \
above applies.
4. bugfix — everything else: routine fixes, internal-only changes, dependency bumps with no \
user-facing effect, and nothing the operator needs to act on."""

SEVERITY_LINE_PATTERN = re.compile(
    r"^\s*SEVERITY:\s*(bugfix|feature|action_needed|breaking)\s*$", re.IGNORECASE | re.MULTILINE
)


def summarize_update(
    container_name: str,
    image_repo: str,
    old_tag_or_digest: str | None,
    new_tag_or_digest: str | None,
    release_notes: str,
    compose_config: dict | None,
) -> tuple[str, str]:
    """Returns (summary_markdown, severity). Severity is parsed out of the model's response
    and stripped from the markdown before it's returned, since it's for our own use (dashboard
    badge, notification threshold), not something that reads naturally inline in the note."""
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

    text = "".join(block.text for block in response.content if block.type == "text")

    match = SEVERITY_LINE_PATTERN.search(text)
    severity = match.group(1).lower() if match else "feature"
    summary_markdown = SEVERITY_LINE_PATTERN.sub("", text).strip()

    if not summary_markdown:
        # The model returned essentially nothing beyond the severity line — treat this as a
        # failure rather than silently storing a blank "successful" record with no content
        # for the operator to read. Raising here routes it into reconcile.py's existing
        # error-handling path (visible notice, action_needed severity), same as any other
        # summarization failure.
        raise RuntimeError("Model returned no summary content beyond the severity line")

    return summary_markdown, severity


LOG_TRIAGE_SYSTEM_PROMPT_BASE = """You are triaging pre-filtered log excerpts from a homelab \
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
{{"container": "the container name from the excerpt's header", "title": "a short, specific title \
(under 8 words) that would let someone recognize this same issue if it recurred", "category": \
one of "error", "reliability", "optimization", "severity": one of "critical", "warning", \
"suggestion", "description": "1-3 sentences explaining what's happening"{fix_field}}}

If nothing in the provided excerpts represents a real issue, respond with an empty JSON array: []"""

FIX_FIELD_LOG = ', "fix": "a concrete, specific suggestion for how to resolve this — commands, ' \
    'config changes, or what to check, not generic advice"'


def analyze_logs_batch(excerpts_by_container: dict[str, str], include_fix: bool = False) -> list[dict]:
    """Sends pre-filtered log excerpts (already keyword-matched locally) to Claude for triage.
    Returns a list of finding dicts, or an empty list if nothing real was found — callers
    should treat an empty list as a clean, quiet result, not an error.

    include_fix requests an additional "fix" field (Deep Analysis) — left off by default since
    asking the model to actually work out a remediation costs meaningfully more output tokens
    than just naming the problem.
    """
    if not settings.anthropic_api_key or not excerpts_by_container:
        return []

    sections = []
    for container_name, excerpt in excerpts_by_container.items():
        sections.append(f"=== Container: {container_name} ===\n{excerpt}")
    user_message = "\n\n".join(sections)

    system_prompt = LOG_TRIAGE_SYSTEM_PROMPT_BASE.format(fix_field=FIX_FIELD_LOG if include_fix else "")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=2500 if include_fix else 2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    data = extract_json(text)
    return data if isinstance(data, list) else []


COMPOSE_REVIEW_SYSTEM_PROMPT_BASE = """You are reviewing a docker-compose file from a homelab \
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
{{"title": "a short, specific title (under 8 words)", "category": one of "security", \
"reliability", "optimization", "severity": one of "critical", "warning", "suggestion", \
"description": "1-3 sentences explaining the issue"{fix_field}}}"""

FIX_FIELD_COMPOSE = ', "fix": "a concrete suggested compose file change — the specific key(s) ' \
    'to add or edit, not generic advice"'


def review_compose_file(file_path: str, redacted_yaml: str, include_fix: bool = False) -> list[dict]:
    """Sends a secret-redacted compose file to Claude for a structural review. Returns a list
    of finding dicts, or an empty list if the file looks fine.

    include_fix requests an additional "fix" field (Deep Analysis) — off by default for the
    same token-cost reason as the log triage function.
    """
    if not settings.anthropic_api_key:
        return []

    user_message = f"File: {file_path}\n\n{redacted_yaml}"
    system_prompt = COMPOSE_REVIEW_SYSTEM_PROMPT_BASE.format(fix_field=FIX_FIELD_COMPOSE if include_fix else "")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=2000 if include_fix else 1500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    data = extract_json(text)
    return data if isinstance(data, list) else []


FINDINGS_OVERVIEW_SYSTEM_PROMPT = """You are summarizing a set of findings for a homelab \
operator, all belonging to the same container or compose file. The individual findings are \
already listed separately below where this appears — your job is a short combined overview, \
not a restatement of each one.

Write 2-4 sentences of plain prose: lead with the most important issue, note anything that's \
related or should probably be addressed together, and give an overall sense of how concerning \
the current state is. No markdown headers, no bullet list, no restating every title."""


def summarize_findings_overview(subject_display: str, findings: list[dict]) -> str:
    """Short combined AI overview shown above a subject's findings list. Only meaningful for
    2+ findings — callers should skip calling this for 0 or 1."""
    if not settings.anthropic_api_key or not findings:
        return ""

    listing = "\n".join(
        f"- [{f.get('severity', 'warning')}] {f.get('title', '')} ({f.get('category', '')}): "
        f"{f.get('description_markdown') or ''}"
        for f in findings
    )
    user_message = f"Subject: {subject_display}\n\nFindings:\n{listing}"

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=400,
        system=FINDINGS_OVERVIEW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()

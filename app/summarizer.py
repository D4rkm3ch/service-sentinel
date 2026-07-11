import json
import logging
import re

from app import ai_provider
from app.ai_json import extract_json

logger = logging.getLogger("service_sentinel.summarizer")

SYSTEM_PROMPT = """You write short, practical release-note summaries for a homelab operator \
deciding whether to update a self-hosted Docker container.

The release notes below may cover a SINGLE release, or MULTIPLE releases the operator missed \
since their last check (look for multiple "## <version> (<date>)" headers in the text). If \
there are multiple, write ONE combined summary of everything that changed across all of them \
-- don't summarize each release separately or restate the version headers. Treat it as one \
batch of changes to catch the operator up on, oldest-relevant-context to newest.

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
the release touches their actual configuration, write exactly: "Nothing in this release affects \
your configuration."

For all three sections: use a bullet list only when there are two or more distinct points to \
make. If there's exactly one point, or none, write a plain sentence instead — a bullet list \
with a single item, or a single item padded out to look like a list, reads worse than just \
saying it.

Be concise. This is read on a dashboard, not a blog post. No preamble, no closing summary, no \
restating the version numbers.

After the three sections above, add one final line with nothing else on it, in exactly this \
format: `SEVERITY: X` where X is one of: bugfix, feature, action_needed, breaking.

If multiple releases are covered, this reflects the HIGHEST severity found across any of them \
individually, not just the most recent one — a breaking change three releases back still makes \
the whole batch "breaking." Determine X using this exact order — stop at the first line that \
applies, don't judge it separately from what you already wrote above:
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

    if not ai_provider.is_configured():
        raise RuntimeError("No AI provider is configured (see Settings)")

    for attempt in range(2):
        text = ai_provider.complete_text(system=SYSTEM_PROMPT, user_message=user_message, max_tokens=1000)

        match = SEVERITY_LINE_PATTERN.search(text)
        severity = match.group(1).lower() if match else "feature"
        summary_markdown = SEVERITY_LINE_PATTERN.sub("", text).strip()

        if summary_markdown:
            return summary_markdown, severity

        logger.warning(
            "Model returned no summary content for %s beyond the severity line (attempt %d/2)",
            container_name, attempt + 1,
        )

    # The model returned essentially nothing beyond the severity line, twice in a row —
    # treat this as a real failure rather than silently storing a blank "successful" record
    # with no content for the operator to read. Raising here routes it into reconcile.py's
    # existing error-handling path (visible notice, action_needed severity), same as any
    # other summarization failure.
    raise RuntimeError("Model returned no summary content beyond the severity line, even after a retry")


UPGRADE_GUIDANCE_SYSTEM_PROMPT = """You give a homelab operator concrete, actionable guidance \
for upgrading a self-hosted Docker container, given its release notes, the summary already \
written for this update, and their actual compose configuration.

Write a short, practical checklist of concrete steps to take BEFORE or DURING this upgrade -- \
config changes to make first, backups worth taking, migration commands to run, env vars to add \
or change, ports or volumes to adjust. Only include real, specific guidance grounded in what \
the release notes actually say -- never invent generic advice like "always back up before \
upgrading" unless the release notes specifically call out something that makes that unusually \
important this time.

If there's genuinely nothing the operator needs to do beyond a normal update (pull the new \
image, restart), write exactly: "No action needed beyond a normal update."

Format as a short markdown bullet list, or exactly that sentence if there's nothing to do. No \
headers, no preamble, no restating the summary you were given."""


def generate_upgrade_guidance(container_name: str, image_repo: str, release_notes: str,
                               compose_config: dict | None, summary_markdown: str) -> str:
    """Deep Analysis for Updates (opt-in, off by default) -- concrete upgrade/migration steps
    alongside the regular summary, mirroring Logs/Compose's per-finding suggested fix. A
    separate AI call from summarize_update() above (same pattern analyze_stack_impact below
    uses) rather than a third field bolted onto that response -- summarize_update's severity-
    line-stripping contract is already relied on by several callers/tests, and this is only
    ever wanted for the subset of updates where the toggle is on."""
    compose_block = (
        json.dumps(compose_config, indent=2, default=str)
        if compose_config
        else "(no matching compose service found)"
    )
    user_message = f"""Container: {container_name}
Image: {image_repo}

Release notes:
---
{release_notes}
---

Summary already written for this update:
---
{summary_markdown}
---

Operator's compose configuration for this service:
---
{compose_block}
---"""

    if not ai_provider.is_configured():
        return ""

    return ai_provider.complete_text(
        system=UPGRADE_GUIDANCE_SYSTEM_PROMPT, user_message=user_message, max_tokens=500,
    ).strip()


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

FIX_INSTRUCTION_LOG = "a concrete, specific suggestion for how to resolve this — commands, " \
    "config changes, or what to check, not generic advice"
FIX_FIELD_LOG = f', "fix": "{FIX_INSTRUCTION_LOG}"'


def analyze_logs_batch(excerpts_by_container: dict[str, str], include_fix: bool = False) -> list[dict]:
    """Sends pre-filtered log excerpts (already keyword-matched locally) to Claude for triage.
    Returns a list of finding dicts, or an empty list if nothing real was found — callers
    should treat an empty list as a clean, quiet result, not an error.

    include_fix requests an additional "fix" field (Deep Analysis) — left off by default since
    asking the model to actually work out a remediation costs meaningfully more output tokens
    than just naming the problem.
    """
    if not excerpts_by_container:
        return []

    sections = []
    for container_name, excerpt in excerpts_by_container.items():
        sections.append(f"=== Container: {container_name} ===\n{excerpt}")
    user_message = "\n\n".join(sections)

    system_prompt = LOG_TRIAGE_SYSTEM_PROMPT_BASE.format(fix_field=FIX_FIELD_LOG if include_fix else "")

    if not ai_provider.is_configured():
        return []

    text = ai_provider.complete_text(
        system=system_prompt, user_message=user_message, max_tokens=2500 if include_fix else 2000,
    )
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
- Reliability issues: missing restart policy, service dependencies that aren't declared via \
depends_on.
- Optimization opportunities: redundant or unused environment variables, obviously outdated \
image-pinning practice (e.g. floating :latest on a service where that's risky), network \
misconfiguration.

Only report things with real substance — skip purely stylistic nitpicks. If the file looks fine, \
say so by returning an empty array.

Respond with ONLY a JSON array and nothing else — no markdown fences, no preamble. Each element:
{{"title": "a short, specific title (under 8 words)", "category": one of "security", \
"reliability", "optimization", "severity": one of "critical", "warning", "suggestion", \
"description": "1-3 sentences explaining the issue"{fix_field}}}"""

FIX_INSTRUCTION_COMPOSE = "a concrete suggested compose file change — the specific key(s) to " \
    "add or edit, not generic advice"
FIX_FIELD_COMPOSE = f', "fix": "{FIX_INSTRUCTION_COMPOSE}"'


def review_compose_file(file_path: str, redacted_yaml: str, include_fix: bool = False) -> list[dict]:
    """Sends a secret-redacted compose file to Claude for a structural review. Returns a list
    of finding dicts, or an empty list if the file looks fine.

    include_fix requests an additional "fix" field (Deep Analysis) — off by default for the
    same token-cost reason as the log triage function.
    """
    user_message = f"File: {file_path}\n\n{redacted_yaml}"
    system_prompt = COMPOSE_REVIEW_SYSTEM_PROMPT_BASE.format(fix_field=FIX_FIELD_COMPOSE if include_fix else "")

    if not ai_provider.is_configured():
        return []

    text = ai_provider.complete_text(
        system=system_prompt, user_message=user_message, max_tokens=2000 if include_fix else 1500,
    )
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
    if not findings:
        return ""

    listing = "\n".join(
        f"- [{f.get('severity', 'warning')}] {f.get('title', '')} ({f.get('category', '')}): "
        f"{f.get('description_markdown') or ''}"
        for f in findings
    )
    user_message = f"Subject: {subject_display}\n\nFindings:\n{listing}"

    if not ai_provider.is_configured():
        return ""

    return ai_provider.complete_text(
        system=FINDINGS_OVERVIEW_SYSTEM_PROMPT, user_message=user_message, max_tokens=400,
    ).strip()


def generate_stack_name(service_names: list[str]) -> str:
    """Picks the most central/important service in a compose stack to use as its short
    display label. Falls back to the first service name (alphabetically, for stability)
    if the API isn't configured or the model's answer doesn't match anything we gave it —
    never invents a name outside the actual service list."""
    if not service_names:
        return "Unnamed stack"
    if len(service_names) == 1 or not ai_provider.is_configured():
        return sorted(service_names)[0]

    prompt = (
        f"These services run together in one docker-compose stack: {', '.join(service_names)}.\n\n"
        "Reply with ONLY the name of the single most important or central service, exactly as "
        "written above — no extra text, no punctuation, nothing else."
    )
    try:
        answer = ai_provider.complete_text(system=None, user_message=prompt, max_tokens=30).strip()
        if answer in service_names:
            return answer
    except Exception:
        pass
    return sorted(service_names)[0]


STACK_ANALYSIS_SYSTEM_PROMPT = """You are looking at one docker-compose stack for a homelab \
operator — several services that run together and can affect each other, defined in the same \
file. You'll be given the full list of services in the stack, and for each one that has a \
pending update, its actual release notes or summary.

Read the release notes for anything that names a concrete requirement or effect on another \
service in the same stack — a minimum version of another service/database it now needs, a \
required migration or config/env var change, a changed port or API contract another service \
in this stack calls, breaking changes to a shared volume or data format. Only real, specific \
findings from the text you were given — never guess or speculate about services just because \
they happen to be in the same stack, and never comment on networking, "they share a network," \
or anything else that's true of every compose stack by definition.

In AT MOST 2 short sentences: if you found something concrete, name the specific requirement \
and which service(s) it affects. If nothing in the notes points to a real cross-service effect, \
respond with exactly: "No cross-service issues found." Do not restate the update itself, do not \
describe how each service works, do not explain your reasoning — only state the conclusion.

No markdown, no headers, no bullet list."""


def analyze_stack_impact(stack_display_name: str, all_service_names: list[str], changed_summary_text: str) -> str:
    """Cross-service analysis for a compose stack — only meaningful for stacks with 2+
    services. Deliberately separate from the per-service summary: this is about whether a
    change in one service could ripple into its stack-mates, not a restatement of the change
    itself. changed_summary_text should carry the actual release notes/summary text for each
    member with a pending update (see stacks._build_changed_summary) -- without real notes text
    to read, the model has nothing to reason about beyond service names and reliably falls back
    to generic, useless observations like "they share a network"."""
    if len(all_service_names) < 2:
        return ""

    user_message = (
        f"Stack: {stack_display_name}\n"
        f"All services in this stack: {', '.join(all_service_names)}\n\n"
        f"Recent update activity in this stack:\n{changed_summary_text}"
    )

    if not ai_provider.is_configured():
        return ""

    return ai_provider.complete_text(
        system=STACK_ANALYSIS_SYSTEM_PROMPT, user_message=user_message, max_tokens=150,
    ).strip()


LOG_STACK_ANALYSIS_SYSTEM_PROMPT = """You are looking at one docker-compose stack for a \
homelab operator — several services that run together and can affect each other, defined in \
the same file. You'll be given the full list of services in the stack, and for each one with \
active log findings, those findings (crashes, errors, reliability issues, etc.).

Read the findings for anything that suggests one service's problem is causing or being caused \
by another service in the same stack — a database connection failure in one service matching a \
crash in another that depends on it, a shared resource (disk, memory, network) being exhausted \
by one service and starving others, a cascading restart loop. Only real, specific connections \
grounded in the actual findings you were given — never guess or speculate about services just \
because they happen to be in the same stack, and never comment on networking, "they share a \
network," or anything else that's true of every compose stack by definition.

In AT MOST 2 short sentences: if you found a real connection, name the specific services and \
what links their issues. If nothing in the findings points to a real cross-service effect, \
respond with exactly: "No cross-service issues found." Do not restate each finding, do not \
describe how each service works, do not explain your reasoning — only state the conclusion.

No markdown, no headers, no bullet list."""


def analyze_log_stack_impact(stack_display_name: str, all_service_names: list[str], findings_summary_text: str) -> str:
    """Logs' equivalent of analyze_stack_impact above -- cross-service analysis for a compose
    stack's active log findings instead of Updates' release notes. Same "only meaningful for
    2+ services, needs real findings text to reason about" shape; see stacks.
    _build_log_findings_summary for what findings_summary_text actually contains."""
    if len(all_service_names) < 2:
        return ""

    user_message = (
        f"Stack: {stack_display_name}\n"
        f"All services in this stack: {', '.join(all_service_names)}\n\n"
        f"Current log findings in this stack:\n{findings_summary_text}"
    )

    if not ai_provider.is_configured():
        return ""

    return ai_provider.complete_text(
        system=LOG_STACK_ANALYSIS_SYSTEM_PROMPT, user_message=user_message, max_tokens=150,
    ).strip()

import json
import logging
import re

import yaml

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
user-facing effect. This is also where a removed/deprecated/changed thing belongs as a plain \
factual line (e.g. "The Podnapisi subtitle provider has been removed") whenever you can't \
confirm from the compose configuration below that it actually applies to this operator's setup \
-- an app-level setting, a chosen integration, or which of several optional providers they've \
picked inside the application's own UI/database isn't something the compose file can confirm, \
so it's just changelog information here, not a warning. If nothing qualifies, write "Nothing \
notable."

If the release notes themselves have no real changelog content -- just a version/build number \
bump with nothing describing what actually changed -- that counts as nothing qualifying too. \
Write "Nothing notable," never a restated version number dressed up as a sentence (e.g. not \
"The app has been updated to version 1.2.3," which repeats the version header you were already \
told not to restate and gives the operator no more information than they already had). Two \
releases that are equally uninformative must land on the same "Nothing notable" -- and therefore \
the same bugfix severity below -- regardless of whether the raw notes happened to include a \
bare version string or said nothing at all.

## Breaking Changes
Only include something here if BOTH are true: (1) the operator's actual compose configuration \
(provided below) confirms it applies to them -- a specific env var, volume, port, or label they \
have set is the one being removed, changed, or made incompatible, not a guess -- and (2) if \
they don't act, their setup will actually stop working or become inaccessible, not just "a \
specific detail behaves slightly differently" or "something new is now available." If you can't \
confirm it applies from the compose config, or acting on it is recommended rather than required \
to keep working, it doesn't belong here -- mention it under New Features instead (see above), \
or under Relevant to your Setup if it's a real but non-breaking config-specific note. If nothing \
qualifies, write "None found."

## Relevant to your Setup
This is the most important section. Cross-reference the features and breaking changes above \
against the operator's actual compose configuration (provided below). Call out specifically \
which env vars, volumes, ports, or labels they have set are affected, and how. A bullet only \
belongs here if it names something ACTUAL from their compose config -- a specific env var, \
volume path, port number, or label they have set. "This feature is now available on your \
instance" is not relevant to their setup just because they run the container -- every feature \
in a release is technically "available" to every operator running it; that's not what this \
section is for. If nothing in the release touches something specific in their actual \
configuration, write exactly: "Nothing in this release affects your configuration." -- and \
nothing else. Never write real bullets and then also add that exact sentence; if you wrote a \
genuine bullet, that sentence doesn't belong, and if that sentence applies, no bullets do.

Whenever the compose configuration provided can directly answer whether something applies -- a \
specific env var is or isn't set, a service is or isn't present, a volume or port is or isn't \
mapped -- check it and state the answer definitively ("You have APP_URL set, so..."), never \
hedge with "if you have X configured" or "if you are running Y" when you can already see \
whether they do. Save conditional "if" phrasing for things that genuinely can't be determined \
from the compose file, like an in-app setting, database content, or which of several optional \
providers they've picked inside the application's own UI.

If a confirmed, real bullet here means the operator needs to actually change something to keep \
working correctly (even if it's not urgent enough to belong in Breaking Changes above), say so \
explicitly using clear "you need to..."/"you must..."/"you should plan to..." language -- don't \
just neutrally restate the fact (e.g. not "You are using the deprecated X image", but "You are \
using the deprecated X image and should plan to migrate, since it won't receive further \
updates"). Two operators in an identical situation should never end up with different severities \
just because one bullet happened to state the requirement plainly and the other only implied it.

For all three sections: use a bullet list only when there are two or more distinct points to \
make. If there's exactly one point, or none, write a plain sentence instead -- a bullet list \
with a single item, or a single item padded out to look like a list, reads worse than just \
saying it.

Be concise. This is read on a dashboard, not a blog post. No preamble, no closing summary, no \
restating the version numbers.

After the three sections above, add one final line with nothing else on it, in exactly this \
format: `SEVERITY: X` where X is one of: bugfix, feature, action_needed, breaking.

If multiple releases are covered, this reflects the HIGHEST severity found across any of them \
individually, not just the most recent one -- a breaking change three releases back still makes \
the whole batch "breaking." Determine X using this exact order -- stop at the first line that \
applies, don't judge it separately from what you already wrote above:
1. breaking -- the Breaking Changes section above says anything other than "None found" -- since \
that section is itself now restricted to confirmed-applicable, setup-actually-stops-working \
items (see its own instructions above), anything listed there earns this automatically.
2. action_needed -- nothing rose to Breaking Changes above (their setup keeps running), but the \
Relevant to your Setup section above concludes the operator must still actually change \
something in their own configuration (an env var, a volume, a port, a label) for this update to \
work correctly, or to keep working the same way. This is not for optional new configuration \
they could choose to use, or for anything you couldn't confirm applies to their actual setup -- \
only for something confirmed and required.
3. feature -- New Features above describes at least one genuinely new, added capability or \
enhancement (something the operator couldn't do before), and neither of the above applies. A \
line that only describes existing behavior being corrected, adjusted, or removed -- "X now \
correctly does Y," "fixed an issue where...," "Z has been removed" -- does not by itself count \
as new content for this rule, even though it belongs in the New Features section above (see \
that section's own instructions) and even though it's not "Nothing notable" either.
4. bugfix -- everything else: New Features above is either "Nothing notable," or entirely made \
up of fixes/corrections/removals/internal changes/dependency bumps with no newly added \
capability, and nothing the operator needs to act on. Two releases that both amount to "a \
handful of behavior corrections, nothing new added" must land on the same bugfix severity, \
regardless of how many such corrections there were or how each one happened to be phrased."""

SEVERITY_LINE_PATTERN = re.compile(
    r"^\s*SEVERITY:\s*(bugfix|feature|action_needed|breaking)\s*$", re.IGNORECASE | re.MULTILINE
)

_LIST_ITEM_LINE = re.compile(r"^[-*]\s+(.*)$")


def _debulletize_single_item_sections(markdown_text: str) -> str:
    """The system prompt above (see "use a bullet list only when there are two or more distinct
    points") asks the model to write a single point as a plain sentence rather than a one-item
    bullet list -- a real-world report showed a "## New Features" section with a lone bullet
    anyway, since a prompt instruction is a request, not a guarantee. Enforced here in code
    instead: any "## " section whose entire body is exactly one bullet line has that line's
    marker stripped, turning it into a plain sentence. Genuine 2+-item lists, and sections that
    were already plain sentences, are left untouched."""
    lines = markdown_text.split("\n")
    sections: list[tuple[str | None, list[str]]] = []
    header = None
    body: list[str] = []
    for line in lines:
        if line.startswith("## "):
            sections.append((header, body))
            header, body = line, []
        else:
            body.append(line)
    sections.append((header, body))

    out: list[str] = []
    for header, body in sections:
        if header is not None:
            out.append(header)
        non_blank = [line for line in body if line.strip()]
        list_items = [line for line in non_blank if _LIST_ITEM_LINE.match(line.strip())]
        if len(non_blank) == 1 and len(list_items) == 1:
            out.append("")
            out.append(_LIST_ITEM_LINE.match(non_blank[0].strip()).group(1))
        else:
            out.extend(body)
    return "\n".join(out)


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
        else "(no matching compose service found -- general summary only, "
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
        text = ai_provider.complete_text(system=SYSTEM_PROMPT, user_message=user_message, max_tokens=2000)

        match = SEVERITY_LINE_PATTERN.search(text)
        severity = match.group(1).lower() if match else "feature"
        summary_markdown = SEVERITY_LINE_PATTERN.sub("", text).strip()

        if summary_markdown:
            return _debulletize_single_item_sections(summary_markdown), severity

        logger.warning(
            "Model returned no summary content for %s beyond the severity line (attempt %d/2)",
            container_name, attempt + 1,
        )

    # The model returned essentially nothing beyond the severity line, twice in a row --
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

The operator is already running this container successfully today -- skip pre-flight checks \
that are almost certainly already satisfied by any working setup (e.g. a baseline CPU/OS \
requirement bump that virtually all modern hardware already meets). Only include a step if \
there's a real, meaningful chance they need to actually act on it, not a hedge to "double check" \
something that's already extremely likely fine.

Whenever the compose configuration provided can directly answer whether a step applies -- a \
specific service, env var, volume, or port is or isn't present -- check it and say so \
definitively, never write conditional "if you are running X" or "if you have Y configured" \
guidance when you can already see whether they do. Save "if" phrasing for things that genuinely \
can't be determined from the compose file, like an in-app setting or which of several optional \
providers they've picked inside the application's own UI.

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
        system=UPGRADE_GUIDANCE_SYSTEM_PROMPT, user_message=user_message, max_tokens=1200,
    ).strip()


LOG_TRIAGE_SYSTEM_PROMPT_BASE = """You are triaging pre-filtered log excerpts from a homelab \
operator's self-hosted Docker containers. Each excerpt already only contains lines that matched \
suspicious keywords (error, exception, failed, etc.) plus a little surrounding context -- most \
routine noise has already been stripped out before it reached you.

Your job: separate genuine problems from false positives. A lot of software logs the word \
"error" or "warning" for routine, expected situations (a health check retry during startup, an \
SSL renegotiation, a client disconnect) -- do not report those. Only report things that indicate \
an actual problem worth a human's attention, or a clear, concrete optimization opportunity you \
can see directly in the excerpt (e.g. a container repeatedly restarting, an obvious \
misconfiguration visible in the error text).

If the excerpt itself explains that something is expected default behavior for how a container \
commonly runs (e.g. a GUI app logging that it found no X server, which is normal for a headless \
container; a container failing to set an advanced sysctl like `src_valid_mark` because it isn't \
running with elevated privileges, which is the default and expected state for almost every \
container) don't report it at all -- that's the same "routine, expected situation" case above, \
just one where the explanation happens to be visible in the log line rather than something you \
already knew. Never report something as an issue and then hedge in its own description that it \
might actually be expected or fine -- if you're explaining away your own finding, it isn't one.

Pick "category" by what's actually happening, not by which container it's happening in: a \
request/query/connection that completes and gets a normal response, just not the content you \
wanted (e.g. a search that returns zero results because of a category or filter mismatch), is \
an "optimization" -- nothing failed. Reserve "error" for something that didn't complete \
successfully (a connection refused, a request rejected, a crash, a title/file that couldn't be \
parsed at all), and "reliability" for something intermittent or flaky (works sometimes, times \
out or drops other times). The same kind of situation must get the same category regardless of \
which container or application it's happening in -- these exact shapes recur constantly across \
a homelab's *arr-style applications (Sonarr, Radarr, Lidarr, Readarr, Prowlarr, etc.) and must \
be judged the same way every time, not per-container:
- "An indexer search succeeded but matched nothing in the configured categories" is always an \
"optimization" (suggestion severity) -- nothing failed, the config just doesn't line up.
- "A release/file's title couldn't be parsed at all, with nothing in the excerpt suggesting the \
content itself is the wrong media type for this application" is an "error" -- something \
genuinely didn't complete -- regardless of which specific *arr application logged it.
- "A release/file's title couldn't be parsed, and the title itself is visibly the wrong media \
type for this application" (e.g. adult content, a different show, a game, pushed to an \
application that only handles one media type) is an "optimization" (suggestion severity) -- \
the application is working correctly, the indexer/category configuration feeding it is what's \
wrong, and that's a one-time config fix, not an ongoing problem.

Pick "severity" by how urgently a human needs to act, independent of which "category" it's \
filed under: "critical" is for something actively broken right now (a service down, a \
connection that never succeeds, data corruption); "warning" is for something degraded or \
intermittently failing that still deserves attention; "suggestion" is for something that isn't \
broken at all, just an opportunity to improve. An "optimization" category finding describes \
something that isn't broken by definition, so it should almost always be "suggestion" severity \
-- reserve "warning" for it only if the inefficiency is actively causing real, ongoing harm (not \
just "could be better"). Two containers hitting the same underlying kind of issue (e.g. both \
misconfigured indexer categories yielding no results) should land on the same category AND the \
same severity, not just one or the other.

Respond with ONLY a JSON array and nothing else -- no markdown fences, no preamble. Each element:
{{"container": "the container name from the excerpt's header", "title": "a short, specific title \
(under 8 words) that would let someone recognize this same issue if it recurred", "category": \
one of "error", "reliability", "optimization", "severity": one of "critical", "warning", \
"suggestion", "description": "1-3 sentences explaining what's happening"{fix_field}}}

If nothing in the provided excerpts represents a real issue, respond with an empty JSON array: []

Some containers below also list their own currently tracked open issues, under "Already tracked \
-- check if still happening". For each one, decide from the log evidence you're given whether \
it's still occurring or appears to have been resolved (the log now shows normal operation, or \
the specific failure mode described is no longer present). Only report one as resolved if the \
evidence actually supports it -- if what you're given is too short, unrelated, or simply doesn't \
touch on that issue either way, leave it alone and say nothing about it, don't guess. Report a \
resolved issue as its own element in the same JSON array as your findings above, shaped exactly \
like this instead: {{"container": "the container name", "resolved_title": "its exact title as \
given below"}}."""

FIX_INSTRUCTION_LOG = "a concrete, specific suggestion for how to resolve this -- commands, " \
    "config changes, or what to check, not generic advice"
FIX_FIELD_LOG = f', "fix": "{FIX_INSTRUCTION_LOG}"'


def analyze_logs_batch(excerpts_by_container: dict[str, str], include_fix: bool = False,
                        active_findings_by_container: dict[str, list[dict]] | None = None) -> list[dict]:
    """Sends pre-filtered log excerpts (already keyword-matched locally) to Claude for triage.
    Returns a list of dicts, or an empty list if nothing real was found -- callers should treat
    an empty list as a clean, quiet result, not an error. Each element is either a new finding
    (has "title"/"category"/"severity"/"description") or, when active_findings_by_container
    named that container, possibly a resolved-issue marker instead (has "resolved_title") --
    see the system prompt's own instructions on that below.

    include_fix requests an additional "fix" field (Deep Analysis) -- left off by default since
    asking the model to actually work out a remediation costs meaningfully more output tokens
    than just naming the problem.

    active_findings_by_container (db.get_active_findings_by_subject), when given, is a real-
    world-report-driven feature: a container's already-open findings persist forever otherwise,
    even once whatever caused them is actually fixed, since nothing else in this app ever
    re-examines an existing finding once it's been recorded. Listing them alongside the fresh
    excerpt lets the model judge whether the new evidence shows they've cleared up -- see
    log_watcher.py, which also makes sure a container with open findings still reaches this call
    even when its own new fetch had nothing suspicious to report on its own (log_filter.
    recent_tail), since otherwise there'd be no evidence at all to judge resolution against."""
    if not excerpts_by_container:
        return []

    active_findings_by_container = active_findings_by_container or {}
    sections = []
    for container_name, excerpt in excerpts_by_container.items():
        section = f"=== Container: {container_name} ==="
        tracked = active_findings_by_container.get(container_name)
        if tracked:
            tracked_lines = "\n".join(f'- "{f["title"]}": {f["description"]}' for f in tracked)
            section += f"\nAlready tracked -- check if still happening:\n{tracked_lines}"
        section += f"\n{excerpt}"
        sections.append(section)
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
this -- you're reviewing structure and configuration, not credentials.

Look for:
- Security issues: unnecessarily exposed ports, containers running as root when they don't need \
to, overly permissive volume mounts (e.g. mounting the whole filesystem or the Docker socket \
read-write when read-only would do).
- Reliability issues: missing restart policy, service dependencies that aren't declared via \
depends_on.
- Optimization opportunities: redundant or unused environment variables, network \
misconfiguration.

Before flagging a volume mount's read/write mode in either direction, re-read its actual current \
suffix character by character, directly from the line in the file as written (:ro, :rw, or no \
suffix at all, which defaults to read-write) -- never describe a mount as read-write if it \
already ends in :ro, and never recommend a change the file has already made. Quote the exact \
current suffix to yourself before deciding there's a finding here at all: if what you just \
quoted already matches the value you'd otherwise recommend, there is no finding -- not even one \
that says it's already correct or that no change is needed. A correctly-configured mount doesn't \
get a JSON object; it gets nothing.

Do NOT flag any of the following -- this homelab operator has explicitly decided none of these \
are worth reporting, even as a low-severity suggestion:
- Missing resource limits (CPU/memory limits).
- Image tag/version-pinning choice in either direction (floating :latest vs. a pinned version, \
or recommending one floating tag over another) -- assume it's a deliberate choice, not an \
oversight, and never invent a specific version number to suggest since you don't have real \
release data for these images.
- network_mode: host, in any form -- not as an optimization/convenience suggestion, and not as \
a security concern either. Plenty of real services (VPN/torrent stacks, media servers doing \
hardware transcoding, anything needing host-level network discovery) genuinely require it; \
assume it's a deliberate, informed choice on every service that uses it, full stop.
- Any missing healthcheck, in any form -- a service with no healthcheck defined at all, a \
depends_on entry with condition: service_healthy pointing at a dependency with no healthcheck, \
or any other "this would be more robust with a healthcheck" observation. This operator already \
knows and has decided it's not worth the added compose-file complexity; do not raise it, at any \
severity, framed any way.
- An environment value that's a `${{VARIABLE}}` or `${{VARIABLE:-default}}` reference to a name not \
otherwise defined in this same file. This is completely normal and expected -- the operator \
supplies these separately at deploy time (a Docker secret, a `.env` file, the shell environment), \
not inside the compose file itself. Never describe this as missing, undefined, broken, or likely \
to fail; always assume it resolves correctly wherever it's used.
- An empty `networks: {{}}` block. Several common stack-management tools (Dockge among them) \
insert this automatically; it's inert boilerplate, not something to flag or recommend removing.
- PUID, PGID, GUID, UID, or TZ environment variables as "redundant" or "unnecessary," on the \
theory that a particular base image (e.g. a Postgres or other non-linuxserver.io image) doesn't \
respond to them. This is an extremely common self-hosted convention (the linuxserver.io image \
family in particular) and you cannot reliably know from the compose file alone whether the \
specific image in use honors them -- getting this wrong means telling the operator to remove \
something their container actually depends on for correct file ownership. Never flag these as \
redundant; assume they're intentional.
- Adding an explicit ":rw" to a mount that already defaults to read-write (no suffix at all). \
NEVER recommend this, under any framing -- "makes the intent clearer," "defensive future-\
proofing," "prevents a later accidental edit," "best practice to be explicit" -- none of these \
change the mount's actual behavior, so none of them make it a real finding. An unsuffixed mount \
and a ":rw" mount are the identical, already-correct configuration; there is nothing to fix.
- Recommending read-only for a volume that's a service's OWN config, cache, database-data, or \
download directory -- the kind of path the service itself writes to in order to function (save \
settings, write cache files, write its database, save downloaded files). These categories need \
write access by definition; don't raise it as a security concern and then hedge in the fix that \
"rw is generally required so no change is recommended" -- if that's the honest conclusion, the \
finding was never real, so don't start writing it in the first place.
- Recommending read-only for a media library mount used by a media *manager* or *processor* \
service, as opposed to a pure playback server. A manager's whole job is renaming, moving, \
hardlinking, or deleting files inside that library -- it categorically is not "just reading" \
regardless of how the mount looks in isolation. Recognize these by service name, container_name, \
or image (allow for suffixes/variants like "sonarr-4k" or "lscr.io/linuxserver/sonarr"), \
including but not limited to: Sonarr, Radarr, Lidarr, Readarr, Whisparr, Bazarr, Prowlarr, \
Tdarr, FileFlows, Cleanuparr, Kapowarr, Audiobookshelf, Huntarr, Janitorr, Unpackerr, \
qBittorrent, and Qui. Reserve read-only recommendations for a mount you can tell is genuinely \
playback/reference-only for that specific service (a pure media server's own library mount, or \
one service reading another's data it doesn't own).

These exclusions are pre-filters, not second thoughts: decide whether a finding belongs in one \
of these categories BEFORE drafting its title/description/fix, not by writing it out and then \
noticing partway through that it doesn't hold up. Once you've started a finding object, you will \
tend to finish it even when your own reasoning no longer supports it -- so the discipline has to \
happen at the decision to start one, not at the end.

Only report things with real substance -- skip purely stylistic nitpicks or preferences with no \
functional difference. If the file looks fine, say so by returning an empty array.

Respond with ONLY a JSON array and nothing else -- no markdown fences, no preamble. Each element:
{{"title": "a short, specific title (under 8 words) naming the exact setting, service, or mount \
this finding is actually about -- it must match what the description and fix describe, never a \
different one (e.g. don't title something \"Docker socket mount\" when the finding is really \
about an unrelated volume)", "category": one of "security", \
"reliability", "optimization", "severity": one of "critical", "warning", "suggestion", \
"description": "1-3 sentences explaining the issue"{fix_field}}}"""

FIX_INSTRUCTION_COMPOSE = "a concrete suggested compose file change -- the specific key(s) to " \
    "add or edit, not generic advice. State it as a from-the-actual-current-value " \
    "to-the-recommended-value edit (e.g. \"change ':rw' to ':ro'\"), matching what the file " \
    "actually has right now -- re-read the current value from the file before writing the fix " \
    "so the direction of the change is never backwards from what the description says is wrong"
FIX_FIELD_COMPOSE = f', "fix": "{FIX_INSTRUCTION_COMPOSE}"'


_DOCKER_SOCKET_PATH = "/var/run/docker.sock"


def _docker_socket_mounts_are_all_read_only(redacted_yaml: str) -> bool | None:
    """Deterministically checks every service's docker.sock volume mount (if any) in this
    file, rather than trusting the AI to have read the suffix correctly -- a real, recurring
    report: the reviewer has twice claimed the socket was mounted read-write ("no explicit
    :ro suffix") on a file that plainly had :ro right there in the text it was given. Since
    this is the single highest-severity, most security-sensitive check this reviewer makes,
    and it's mechanically checkable (one path, one suffix), it gets a code-level guard instead
    of relying on prompt compliance alone -- see review_compose_file's own use of this.

    Returns True if every docker.sock mount found is :ro, False if at least one isn't, None if
    the file mentions no docker.sock mount at all (nothing to check or suppress)."""
    try:
        data = yaml.safe_load(redacted_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None

    found_any = False
    for service_def in (data.get("services") or {}).values():
        if not isinstance(service_def, dict):
            continue
        for volume in service_def.get("volumes") or []:
            if not isinstance(volume, str) or _DOCKER_SOCKET_PATH not in volume:
                continue
            found_any = True
            if not volume.rstrip().endswith(":ro"):
                return False
    return True if found_any else None


def _mentions_docker_socket(finding: dict) -> bool:
    text = " ".join(str(finding.get(k, "")) for k in ("title", "description", "fix")).lower()
    return "docker socket" in text or "docker.sock" in text


def review_compose_file(file_path: str, redacted_yaml: str, include_fix: bool = False) -> list[dict]:
    """Sends a secret-redacted compose file to Claude for a structural review. Returns a list
    of finding dicts, or an empty list if the file looks fine.

    include_fix requests an additional "fix" field (Deep Analysis) -- off by default for the
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
    findings = data if isinstance(data, list) else []

    if _docker_socket_mounts_are_all_read_only(redacted_yaml) is True:
        dropped = [f for f in findings if _mentions_docker_socket(f)]
        if dropped:
            logger.warning(
                "Dropped %d AI finding(s) about an already-:ro docker.sock mount for %s (misread despite correct input)",
                len(dropped), file_path,
            )
        findings = [f for f in findings if not _mentions_docker_socket(f)]

    return findings


FINDINGS_OVERVIEW_SYSTEM_PROMPT = """You are summarizing a set of findings for a homelab \
operator, all belonging to the same container or compose file. The individual findings are \
already listed separately below where this appears -- your job is a short combined overview, \
not a restatement of each one.

Write 2-4 sentences of plain prose: lead with the most important issue, note anything that's \
related or should probably be addressed together, and give an overall sense of how concerning \
the current state is. No markdown headers, no bullet list, no restating every title."""


def summarize_findings_overview(subject_display: str, findings: list[dict]) -> str:
    """Short combined AI overview shown above a subject's findings list. Only meaningful for
    2+ findings -- callers should skip calling this for 0 or 1."""
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
    if the API isn't configured or the model's answer doesn't match anything we gave it --
    never invents a name outside the actual service list."""
    if not service_names:
        return "Unnamed stack"
    if len(service_names) == 1 or not ai_provider.is_configured():
        return sorted(service_names)[0]

    prompt = (
        f"These services run together in one docker-compose stack: {', '.join(service_names)}.\n\n"
        "Reply with ONLY the name of the single most important or central service, exactly as "
        "written above -- no extra text, no punctuation, nothing else."
    )
    try:
        # 30 used to be the starting budget here, on the assumption a single echoed service
        # name needs almost nothing -- but a real-world report traced part of the Logs check's
        # slowness to exactly this call: Gemini's thinking-enabled models count internal
        # reasoning tokens against max_tokens too, so even this terse a prompt routinely blew
        # through 30 and needed two or three full retry round-trips (30 -> 60 -> 120 -> 240)
        # just to echo one name back. Bumping to 100 cut most of that, but a second real-world
        # dump showed even 100 still needed one retry (succeeding at 200) often enough that 200
        # is the better starting point -- the escalating retry in ai_provider.py still covers
        # genuine outliers.
        answer = ai_provider.complete_text(system=None, user_message=prompt, max_tokens=200).strip()
        if answer in service_names:
            return answer
    except Exception:
        # Deliberately non-fatal (the alphabetical fallback below is perfectly serviceable for
        # picking a representative name), but logged rather than silently swallowed -- this was
        # the one except-Exception block in the app that dropped the error on the floor
        # entirely, found by a systematic sweep.
        logger.warning("Stack name-pick AI call failed -- falling back to alphabetical", exc_info=True)
    return sorted(service_names)[0]


STACK_ANALYSIS_SYSTEM_PROMPT = """You are looking at one docker-compose stack for a homelab \
operator -- several services that run together and can affect each other, defined in the same \
file. You'll be given the full list of services in the stack, and for each one that has a \
pending update, its actual release notes or summary.

Read the release notes for anything that names a concrete requirement or effect on another \
service in the same stack -- a minimum version of another service/database it now needs, a \
required migration or config/env var change, a changed port or API contract another service \
in this stack calls, breaking changes to a shared volume or data format. Only real, specific \
findings from the text you were given -- never guess or speculate about services just because \
they happen to be in the same stack, and never comment on networking, "they share a network," \
or anything else that's true of every compose stack by definition.

In AT MOST 2 short sentences: if you found something concrete, name the specific requirement \
and which service(s) it affects. If nothing in the notes points to a real cross-service effect, \
respond with exactly: "No cross-service issues found." Do not restate the update itself, do not \
describe how each service works, do not explain your reasoning -- only state the conclusion.

No markdown, no headers, no bullet list."""


def analyze_stack_impact(stack_display_name: str, all_service_names: list[str], changed_summary_text: str) -> str:
    """Cross-service analysis for a compose stack -- only meaningful for stacks with 2+
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

    # See generate_stack_name's own comment on why this starts well above what a "2 short
    # sentences" reply looks like it should need -- Gemini's thinking-enabled models count
    # internal reasoning tokens against max_tokens too, so a too-tight starting budget here
    # just means paying for extra retry round-trips instead of a shorter real response.
    return ai_provider.complete_text(
        system=STACK_ANALYSIS_SYSTEM_PROMPT, user_message=user_message, max_tokens=400,
    ).strip()


LOG_STACK_ANALYSIS_SYSTEM_PROMPT = """You are looking at one docker-compose stack for a \
homelab operator -- several services that run together and can affect each other, defined in \
the same file. You'll be given the full list of services in the stack, and for each one with \
active log findings, those findings (crashes, errors, reliability issues, etc.).

Read the findings for anything that suggests one service's problem is causing or being caused \
by another service in the same stack -- a database connection failure in one service matching a \
crash in another that depends on it, a shared resource (disk, memory, network) being exhausted \
by one service and starving others, a cascading restart loop. Only real, specific connections \
grounded in the actual findings you were given -- never guess or speculate about services just \
because they happen to be in the same stack, and never comment on networking, "they share a \
network," or anything else that's true of every compose stack by definition.

In AT MOST 2 short sentences: if you found a real connection, name the specific services and \
what links their issues. If nothing in the findings points to a real cross-service effect, \
respond with exactly: "No cross-service issues found." Do not restate each finding, do not \
describe how each service works, do not explain your reasoning -- only state the conclusion.

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

    # See analyze_stack_impact's own comment (same reasoning-token overhead, same fix).
    return ai_provider.complete_text(
        system=LOG_STACK_ANALYSIS_SYSTEM_PROMPT, user_message=user_message, max_tokens=400,
    ).strip()

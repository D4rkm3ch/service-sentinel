"""In-app AI chat -- the "Ask Service Sentinel" widget's backend (front-end shell in base.html,
HTTP route in main.py). Answers questions about the operator's current system by handing the
model a fresh plain-text snapshot of live state on every turn, and can additionally PROPOSE (see
SYSTEM_PROMPT_HEADER, answer(), _extract_proposed_actions()) two narrow kinds of change an
operator can ask it to make: silencing findings that are already open, and adding a standing
rule for what should or shouldn't be flagged in future checks -- so each operator can shape the
Runtime/Configuration reviewers' judgment to their own setup instead of only ever being able to
talk about a mismatch, never fix it.

Still strictly read-only ITSELF, on purpose: this module only ever parses and validates the
model's own proposed JSON, it never executes one. A proposal only becomes a real change through
app/chat_actions.py, and only after the operator clicks Confirm in the UI (see main.py's POST
/chat/confirm-action) -- see that module's own docstring for why the mutating half of this
feature deliberately lives in a different file. Every db call THIS module makes is still a pure
read, enforced mechanically by test_chat.py's guardrail test, which greps this file's own source
for any mutating db.* call and fails if one appears -- unchanged by this feature, since the
model's text generation still can't reach app state by itself.

Deliberately mirrors summarizer.py/release_notes.py's shape: this module holds the feature
logic (snapshot + prompt assembly), main.py stays thin routing glue over answer()."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from app import ai_provider, db
from app.docker_client import get_container_logs_since
from app.schedule_spec import describe as describe_schedule

logger = logging.getLogger("service_sentinel.chat")

# Bounds on what a single request can send the model. A long-lived open panel would otherwise
# grow the conversation unboundedly, and each turn re-sends the whole history plus a fresh
# snapshot -- so both the number of turns and each turn's length are capped here (the front-end
# trims too, but the server never trusts the client to have done so). MAX_HISTORY_MESSAGES
# keeps the newest turns; MAX_MESSAGE_CHARS clips any single over-long message.
MAX_HISTORY_MESSAGES = 20
MAX_MESSAGE_CHARS = 4000

# Reply budget. Chat answers are prose, not the tightly-bounded JSON the check pipelines ask
# for, so this is roomier than those call sites -- but still finite, and _with_truncation_retry
# inside complete_chat grows it if a genuinely long answer gets cut off.
_MAX_TOKENS = 1200

# How many items to itemize per module before collapsing the rest into a "+N more" line -- the
# total count is always stated regardless, so the model never undercounts even when the list is
# clipped (a real system showed 20 runtime issues at once in testing).
_ITEMS_PER_SECTION = 12

# (feature key, human label) for each monitored module, in the order the Overview shows them.
_SECTIONS = (
    ("updates", "Versions"),
    ("logs", "Runtime"),
    ("compose", "Configuration"),
)

# Live log fetching (see _live_logs_for). Bounded hard on both axes: a couple of containers per
# question and a few thousand characters each, since this is appended to the snapshot on top of
# the conversation itself and every turn pays for it again. The window is deliberately short --
# "what is this container doing right now" is the question this answers, not "what happened
# last week", which is what the Runtime Health check itself is for.
_LIVE_LOG_MAX_CONTAINERS = 2
_LIVE_LOG_MAX_CHARS = 4000
_LIVE_LOG_MAX_LINES = 200
_LIVE_LOG_LOOKBACK_HOURS = 2

SYSTEM_PROMPT_HEADER = """You are the AI assistant built into Service Sentinel, a homelab \
Docker container monitoring tool. You're talking with the operator who runs this system. Be a \
knowledgeable, opinionated collaborator: diagnose problems, explain what's going on and why, \
recommend what you'd do about it, weigh options, and help think through plans. Give real \
advice and concrete steps -- that's the entire point of this conversation.

The one thing you cannot do is carry out changes yourself, with one narrow exception: you may \
PROPOSE (never perform) two specific kinds of change to how Service Sentinel's own reviewers \
judge findings -- silencing findings that are already open, or adding a standing rule for what \
should or shouldn't be flagged going forward. Everything else is off limits: you have no \
ability to edit files, modify or restart containers, change any other setting, or take any \
other action on this system or the operator's machine -- you can only read, plus those two \
narrow proposals below. So when you recommend anything else, describe what the operator should \
do and let them do it. Never claim to have done something, and never promise to do something \
later -- even a proposal you make below only takes effect once the operator clicks Confirm on \
it; until then, nothing has actually changed.

When, and only when, the operator explicitly asks you to silence/ignore/dismiss an existing \
finding, or to add a standing rule about what should always or never be flagged, respond \
normally first -- explain what you're proposing and why, in plain language -- then end your \
reply with exactly one fenced block labeled action-proposal containing a JSON object shaped \
{"actions": [...]}. Each element is either {"type": "silence_findings", "source": "logs" or \
"compose", "subjects": ["container or file name", ...], "title_contains": "text from the \
finding's own title", "reason": "one sentence for the operator"} (silences every currently \
active finding for those subjects whose title contains that text) or {"type": \
"add_custom_rule", "source": "logs" or "compose", "rule_type": "exclude" or "watch", "name": "a \
short label for this rule, a few words, for a settings list", "instruction": "the exact \
instruction text, written the way you'd want a future reviewer to read it verbatim", "reason": \
"one sentence for the operator"} ("exclude" means never flag it again anywhere; "watch" means \
always flag it). Never include an action-proposal block for \
anything else, or when the operator hasn't actually asked for one of these two things -- \
everyday questions and advice get a normal reply with no block at all, and a compose snippet or \
JSON example you're showing the operator for their own use must never use that fence label.

That limit is about actions, not about opinions. You are absolutely expected to say what you \
think, suggest specific fixes (including exact commands or compose changes for the operator to \
apply themselves), and offer your best judgment even when you're not certain -- just be honest \
about the uncertainty. "I can't advise you" is never the right answer; if you're unsure, reason \
it through out loud and say what you'd try first.

Ground your answers in the operator's real system: reference the actual container, service, and \
finding names from the information below rather than talking in generalities. If something \
isn't in the data below, say you don't have visibility into it rather than inventing it, but \
you may still reason about it from general knowledge, clearly flagged as such.

When the operator names a specific container, that container's most recent log output is \
fetched live and included below under "Live logs" -- read it and answer from what it actually \
says. If they ask about logs without naming a container, ask which one they mean, since only \
named containers get fetched. You do not have the raw compose files themselves, only the \
configuration findings summarized below.

Keep replies tight and readable -- short paragraphs or bullets, no filler preamble.

Current system state:
"""


def _updates_pending_count() -> int:
    """The same actionable-and-not-silenced set the Updates page's own count badge and the
    Overview hero use (see main._updates_pending_rows) -- counting only unread rows would
    undercount the moment a still-pending update has been viewed once."""
    rows = db.list_tracked_containers_with_status()
    return sum(1 for r in rows if r["status"] in ("update_available", "error") and not r.get("silenced"))


def _section_lines(feature: str, label: str) -> list[str]:
    if feature == "updates":
        count = _updates_pending_count()
        headline = f"{count} pending update{'s' if count != 1 else ''}" if count else "up to date"
    else:
        # Subject-level, non-silenced -- matches the module's own Issues count (see
        # main._build_card), which findings_health_summary's raw finding-row count doesn't.
        count = len(db.list_subjects_with_findings(feature))
        headline = f"{count} open issue{'s' if count != 1 else ''}" if count else "all clean"

    lines = [f"## {label}: {headline}"]

    enabled = db.get_feature_enabled(feature)
    if enabled:
        lines.append(f"Automatic checks: {describe_schedule(db.get_effective_schedule(feature))}.")
    else:
        lines.append("Automatic checks: off (feature disabled).")
    notify = db.get_notifications_enabled() and db.get_feature_notify_enabled(feature)
    lines.append(f"Notifications: {'on' if notify else 'off'}.")

    streak = db.get_feature_health_streak(feature)
    if streak.get("since"):
        lines.append(f"State: {'healthy' if streak['healthy'] else 'issues'} since {streak['since']}.")

    items = db.list_attention_items_for_feature(feature, limit=_ITEMS_PER_SECTION)
    for item in items:
        lines.append(f"- {item['name']}: {item['blurb']} ({item['severity']})")
    if count > len(items):
        lines.append(f"- ...and {count - len(items)} more not listed here.")

    return lines


def build_context_snapshot() -> str:
    """A fresh plain-text digest of current read-only state, rebuilt on every turn (state
    changes between messages, so it's never cached). Only ever reads the monitoring data the
    Overview page already surfaces -- never touches the AI-provider keys, webhook/Apprise URLs,
    auth secret, or any other credential, even though db has getters for all of them (see
    test_chat.py's secrets-exclusion test)."""
    sections = ["\n".join(_section_lines(feature, label)) for feature, label in _SECTIONS]
    return "\n\n".join(sections)


def _known_container_names() -> dict[str, str]:
    """Every container name the chat could be asked about, mapped from the name a person would
    actually type to the real Docker container name. Includes operator-assigned display names
    (the rename feature) alongside the real ones, since someone who renamed a container to
    "Media DB" will ask about "Media DB", not the raw name."""
    names: dict[str, str] = {}
    for row in db.list_tracked_containers_with_status():
        names[row["container_name"]] = row["container_name"]
    for finding in db.list_findings("logs"):
        names[finding["subject"]] = finding["subject"]
    display_names = db.get_container_display_names(list(names)) if names else {}
    for real, shown in display_names.items():
        if shown:
            names[shown] = real
    return names


def _containers_mentioned_in(message: str) -> list[str]:
    """Real container names referenced by the newest user message. Longest candidate first, so
    asking about "romm-db" doesn't match a container merely called "romm" instead; bounded so
    one question can't fan out into fetching logs for a whole fleet."""
    known = _known_container_names()
    if not known:
        return []
    lowered = message.lower()
    found: list[str] = []
    for candidate in sorted(known, key=len, reverse=True):
        real = known[candidate]
        if real in found:
            continue
        # Hyphens and underscores are part of a container name, not word boundaries, so \b
        # would happily match "db" inside "romm-db" -- these lookarounds treat them as name
        # characters instead.
        if re.search(r"(?<![\w.-])" + re.escape(candidate.lower()) + r"(?![\w.-])", lowered):
            found.append(real)
        if len(found) >= _LIVE_LOG_MAX_CONTAINERS:
            break
    return found


def _live_logs_for(message: str) -> str:
    """Recent raw log output for whichever containers the newest message names, ready to append
    to the system prompt -- the "read the logs in real time" path. Read-only (it's a Docker
    logs fetch, the same call the Runtime Health check already makes) and entirely best-effort:
    a container that's gone, an unreachable socket, or a fetch that raises simply contributes
    nothing rather than failing the whole answer.

    Unlike the Runtime Health check this does NOT keyword-filter or touch checkpoints -- the
    operator asked about this container specifically, so what's wanted is what it's actually
    saying right now, and reading logs here must never influence what the scheduled check
    considers already-seen."""
    names = _containers_mentioned_in(message)
    if not names:
        return ""

    since = (datetime.now(timezone.utc) - timedelta(hours=_LIVE_LOG_LOOKBACK_HOURS)).isoformat()
    sections = []
    for name in names:
        try:
            text = get_container_logs_since(name, since, _LIVE_LOG_MAX_LINES)
        except Exception:
            logger.exception("Live log fetch failed for %s", name)
            continue
        if not text:
            sections.append(f"### {name}\n(no log output in the last {_LIVE_LOG_LOOKBACK_HOURS} hours)")
            continue
        if len(text) > _LIVE_LOG_MAX_CHARS:
            text = "(truncated -- showing the most recent output)\n" + text[-_LIVE_LOG_MAX_CHARS:]
        sections.append(f"### {name}\n```\n{text}\n```")

    if not sections:
        return ""
    body = "\n\n".join(sections)
    return (
        f"\n\n## Live logs (fetched just now, last {_LIVE_LOG_LOOKBACK_HOURS} hours)\n\n{body}"
    )


def _clean_history(history) -> list[dict]:
    """Validates and bounds whatever the client sent: keeps only well-formed {role, content}
    turns (role user/assistant, content a non-empty string), trims each to MAX_MESSAGE_CHARS,
    and keeps only the newest MAX_HISTORY_MESSAGES. The server never trusts the front-end to
    have bounded this already."""
    if not isinstance(history, list):
        return []
    cleaned = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    return cleaned[-MAX_HISTORY_MESSAGES:]


# The one place the model can propose (never perform) a change -- see SYSTEM_PROMPT_HEADER's own
# description of the contract. Matches the LAST such block only, in case the model echoes the
# fence label while explaining the feature itself; DOTALL so the JSON body can span lines.
_ACTION_BLOCK_RE = re.compile(r"```action-proposal\s*\n(.*?)```", re.DOTALL)

# What answer() will actually pass through to chat_actions.execute() -- anything else parsed out
# of the model's own JSON is dropped rather than trusted, since the model's output is never
# authoritative on its own (a human still has to click Confirm, and chat_actions.py re-validates
# independently anyway, but there's no reason to carry unrecognized fields any further than here).
_SILENCE_FIELDS = ("source", "subjects", "title_contains", "reason")
_RULE_FIELDS = ("source", "rule_type", "name", "instruction", "reason")


def _extract_proposed_actions(reply: str) -> tuple[str, list[dict]]:
    """Splits a model reply into (display_text, actions) -- the fenced action-proposal block
    (see SYSTEM_PROMPT_HEADER) is parsed out and never shown to the operator as raw JSON, and
    each element is checked for the exact shape either action type needs before it's trusted
    with a Confirm button at all. A malformed or missing block just means no actions were
    proposed -- never a broken reply; the operator still gets the model's own prose either way."""
    match = _ACTION_BLOCK_RE.search(reply)
    if not match:
        return reply, []
    display_text = (reply[:match.start()] + reply[match.end():]).strip()
    try:
        payload = json.loads(match.group(1))
        raw_actions = payload.get("actions") if isinstance(payload, dict) else None
    except (json.JSONDecodeError, AttributeError):
        return display_text, []
    if not isinstance(raw_actions, list):
        return display_text, []

    actions: list[dict] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            continue
        action_type = raw.get("type")
        if action_type == "silence_findings":
            if raw.get("source") not in ("logs", "compose"):
                continue
            if not isinstance(raw.get("subjects"), list) or not raw["subjects"]:
                continue
            if not isinstance(raw.get("title_contains"), str) or not raw["title_contains"].strip():
                continue
            actions.append({"type": action_type, **{k: raw.get(k) for k in _SILENCE_FIELDS}})
        elif action_type == "add_custom_rule":
            if raw.get("source") not in ("logs", "compose"):
                continue
            if raw.get("rule_type") not in ("exclude", "watch"):
                continue
            if not isinstance(raw.get("name"), str) or not raw["name"].strip():
                continue
            if not isinstance(raw.get("instruction"), str) or not raw["instruction"].strip():
                continue
            actions.append({"type": action_type, **{k: raw.get(k) for k in _RULE_FIELDS}})
    return display_text, actions


def answer(history: list[dict]) -> tuple[str, list[dict]]:
    """Runs one chat turn: cleans/bounds the history, builds the system prompt (static header +
    a fresh live snapshot), and returns (markdown_reply, proposed_actions). Raises on an empty
    history (nothing to answer) or any provider failure -- the caller (main.py's /chat/send)
    checks ai_provider.is_configured() before ever reaching here and turns an exception into the
    route's JSON error shape. Provider-agnostic: complete_chat dispatches on the configured
    provider exactly like every other AI call site.

    proposed_actions is never executed here or anywhere else in this module -- extracting and
    validating the model's own JSON is still just reading. Turning one into a real change is
    app/chat_actions.py's job, and only ever runs after the operator clicks Confirm in the UI
    (see main.py's POST /chat/confirm-action) -- see that module's own docstring for why it's
    kept separate rather than folded in here."""
    messages = _clean_history(history)
    if not messages:
        raise ValueError("No message to answer.")
    # Live logs are keyed off the newest user turn only -- fetching for every container named
    # anywhere in the conversation would re-pull the same logs on every follow-up and grow
    # without bound.
    system = SYSTEM_PROMPT_HEADER + build_context_snapshot() + _live_logs_for(messages[-1]["content"])
    reply = ai_provider.complete_chat(system=system, messages=messages, max_tokens=_MAX_TOKENS)
    return _extract_proposed_actions(reply)

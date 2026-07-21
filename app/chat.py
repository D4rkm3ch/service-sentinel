"""In-app AI chat -- the read-only "Ask Service Sentinel" widget's backend (front-end shell in
base.html, HTTP route in main.py). Answers questions about the operator's current system by
handing the model a fresh plain-text snapshot of live state on every turn, rather than giving
it callable tools: there is no callable surface at all, so "the assistant can only look, never
touch" holds by construction, not by trusting a tool registry to stay read-only. Every db call
this module makes is a pure read -- enforced mechanically by test_chat.py's guardrail test,
which greps this file's own source for any mutating db.* call and fails if one appears.

Deliberately mirrors summarizer.py/release_notes.py's shape: this module holds the feature
logic (snapshot + prompt assembly), main.py stays thin routing glue over answer()."""

import logging

from app import ai_provider, db
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
    ("updates", "Updates"),
    ("logs", "Runtime Health"),
    ("compose", "Configuration Health"),
)

SYSTEM_PROMPT_HEADER = """You are the AI assistant built into Service Sentinel, a homelab \
Docker container monitoring tool. Answer the operator's questions about their system using the \
current system state provided below.

You are strictly read-only: you cannot change, fix, restart, silence, resolve, re-check, or \
configure anything, and you must never imply that you can or offer to do so. If the operator \
asks you to take an action like that, say plainly that you can't, and point them to where in \
the app they'd do it themselves (the Updates, Runtime, Configuration, or Settings pages).

Be concise and specific: reference the actual container, service, and finding names from the \
snapshot rather than giving generic advice, whenever the snapshot has something relevant. If \
the answer isn't in the snapshot, say you don't have that information rather than guessing -- \
you only see the summary below, not full container logs or the raw compose files themselves.

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


def answer(history: list[dict]) -> str:
    """Runs one chat turn: cleans/bounds the history, builds the system prompt (static header +
    a fresh live snapshot), and returns the model's raw markdown reply. Raises on an empty
    history (nothing to answer) or any provider failure -- the caller (main.py's /chat/send)
    checks ai_provider.is_configured() before ever reaching here and turns an exception into the
    route's JSON error shape. Provider-agnostic: complete_chat dispatches on the configured
    provider exactly like every other AI call site."""
    messages = _clean_history(history)
    if not messages:
        raise ValueError("No message to answer.")
    system = SYSTEM_PROMPT_HEADER + build_context_snapshot()
    return ai_provider.complete_chat(system=system, messages=messages, max_tokens=_MAX_TOKENS)

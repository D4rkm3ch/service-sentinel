"""app/chat.py -- the read-only in-app assistant's backend. Covers the read-only guardrail (a
source scan proving the module can never mutate state), that the live snapshot reflects real
monitoring data but never leaks secrets, and that answer() cleans/bounds history and dispatches
through ai_provider.complete_chat."""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app import chat, db

db.init_db()

CHAT_SRC = Path(__file__).resolve().parent.parent / "app" / "chat.py"


def _reset():
    db.reset_updates_data()
    db.reset_logs_data()
    db.reset_compose_data()


@pytest.fixture(autouse=True)
def clean_db():
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# The read-only guardrail -- enforced at the source level, not just by review
# ---------------------------------------------------------------------------

def test_chat_module_never_calls_a_mutating_db_function():
    """The whole feature's safety rests on chat.py only ever reading. A context-snapshot design
    means there's no callable surface the model can reach -- but this module's own code still
    has db in scope, so a future edit could accidentally call a writer. This scans the source
    for any db.<mutating-verb> call and fails if one appears, the same deterministic-guard
    spirit as the docker.sock :ro guard: catch the mistake mechanically, not by hoping a
    reviewer notices."""
    source = CHAT_SRC.read_text()
    mutating = re.findall(
        r"\bdb\.(set_|record_|upsert_|mark_|delete_|silence_|unsilence_|resolve_|reset_|"
        r"clear_|prune_|update_)\w*",
        source,
    )
    assert mutating == [], f"chat.py must never mutate state, found: {mutating}"


# ---------------------------------------------------------------------------
# The live snapshot
# ---------------------------------------------------------------------------

def _seed_pending_update():
    db.upsert_container_state("romm-db", "owner/romm", "latest", "sha256:new")
    db.record_update(
        container_name="romm-db", image_repo="owner/romm", tag="latest",
        old_digest="sha256:old", new_digest="sha256:new", summary_markdown="x",
        source_url=None, error=None, severity="feature", release_notes_raw="x",
        upgrade_guidance=None,
    )


def test_snapshot_reports_pending_updates_and_open_findings_by_name():
    _seed_pending_update()
    db.upsert_finding(
        source="logs", subject="sonarr", title="Failed to parse release title",
        category="reliability", severity="warning", description_markdown="parser error",
        suggested_fix=None,
    )
    db.upsert_finding(
        source="compose", subject="/compose/mousehole/compose.yaml",
        title="Unauthenticated access enabled", category="security", severity="critical",
        description_markdown="no auth", suggested_fix=None,
    )

    snapshot = chat.build_context_snapshot()

    assert "1 pending update" in snapshot
    assert "romm-db" in snapshot
    assert "1 open issue" in snapshot
    assert "sonarr" in snapshot
    assert "Failed to parse release title" in snapshot
    assert "Unauthenticated access enabled" in snapshot
    # The three module headers are always present, even the clean one.
    for label in ("Updates", "Runtime Health", "Configuration Health"):
        assert label in snapshot


def test_snapshot_reads_as_clean_when_nothing_is_open():
    snapshot = chat.build_context_snapshot()
    assert "up to date" in snapshot
    assert "all clean" in snapshot


def test_snapshot_never_leaks_configured_secrets():
    """build_context_snapshot only ever reads monitoring data. Seed every credential db can hold
    with a recognizable sentinel and assert none of them appear anywhere in the built snapshot,
    even though db has getters for all of them right next to the ones the snapshot does use."""
    db.set_anthropic_api_key("sk-ant-SECRETVALUE")
    db.set_openai_api_key("sk-openai-SECRETVALUE")
    db.set_gemini_api_key("AIza-SECRETVALUE")
    db.set_github_token("ghp-SECRETVALUE")
    db.set_apprise_urls("discord://SECRETVALUE@webhook")
    db.set_auth_secret("SECRETVALUE-auth")
    try:
        _seed_pending_update()
        snapshot = chat.build_context_snapshot()
        assert "SECRETVALUE" not in snapshot
    finally:
        db.set_anthropic_api_key("")
        db.set_openai_api_key("")
        db.set_gemini_api_key("")
        db.set_github_token("")
        db.set_apprise_urls("")
        db.clear_auth_secret()


# ---------------------------------------------------------------------------
# answer() -- history cleaning + provider dispatch
# ---------------------------------------------------------------------------

def test_clean_history_drops_malformed_turns_and_bounds_length():
    history = [
        {"role": "user", "content": "keep me"},
        {"role": "system", "content": "wrong role, drop"},
        {"role": "assistant", "content": ""},         # empty, drop
        "not a dict",                                    # drop
        {"role": "user", "content": "x" * 9999},       # kept but clipped
    ]
    cleaned = chat._clean_history(history)
    assert [t["role"] for t in cleaned] == ["user", "user"]
    assert cleaned[0]["content"] == "keep me"
    assert len(cleaned[1]["content"]) == chat.MAX_MESSAGE_CHARS


def test_clean_history_keeps_only_the_newest_turns():
    history = [{"role": "user", "content": f"msg {i}"} for i in range(chat.MAX_HISTORY_MESSAGES + 5)]
    cleaned = chat._clean_history(history)
    assert len(cleaned) == chat.MAX_HISTORY_MESSAGES
    assert cleaned[-1]["content"] == f"msg {chat.MAX_HISTORY_MESSAGES + 4}"


def test_answer_builds_the_system_prompt_and_dispatches_to_complete_chat():
    with patch("app.chat.ai_provider.complete_chat", return_value="the reply") as mock_chat:
        result = chat.answer([{"role": "user", "content": "what's pending?"}])

    assert result == "the reply"
    kwargs = mock_chat.call_args.kwargs
    assert kwargs["system"].startswith(chat.SYSTEM_PROMPT_HEADER)
    assert "## Updates" in kwargs["system"]  # the live snapshot is appended to the header
    assert kwargs["messages"] == [{"role": "user", "content": "what's pending?"}]


def test_answer_raises_on_empty_history():
    with pytest.raises(ValueError):
        chat.answer([])

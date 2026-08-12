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
    for label in ("Versions", "Runtime", "Configuration"):
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
        reply, actions = chat.answer([{"role": "user", "content": "what's pending?"}])

    assert reply == "the reply"
    assert actions == []
    kwargs = mock_chat.call_args.kwargs
    assert kwargs["system"].startswith(chat.SYSTEM_PROMPT_HEADER)
    assert "## Versions" in kwargs["system"]  # the live snapshot is appended to the header
    assert kwargs["messages"] == [{"role": "user", "content": "what's pending?"}]


def test_answer_raises_on_empty_history():
    with pytest.raises(ValueError):
        chat.answer([])


# ---------------------------------------------------------------------------
# Live log reading -- the AI pulls a named container's actual current logs
# ---------------------------------------------------------------------------

def _seed_container(name="romm-db"):
    db.upsert_container_state(name, "owner/romm", "latest", "sha256:a")


def test_naming_a_container_fetches_its_live_logs():
    _seed_container()
    with patch("app.chat.get_container_logs_since", return_value="FATAL: disk full") as mock_logs:
        block = chat._live_logs_for("why is romm-db unhealthy?")

    assert "Live logs" in block
    assert "romm-db" in block
    assert "FATAL: disk full" in block
    assert mock_logs.call_args.args[0] == "romm-db"


def test_a_question_naming_no_container_fetches_nothing():
    _seed_container()
    with patch("app.chat.get_container_logs_since") as mock_logs:
        assert chat._live_logs_for("how are things looking overall?") == ""
    mock_logs.assert_not_called()


def test_the_longest_matching_name_wins():
    """A container called "romm" must not swallow a question about "romm-db"."""
    _seed_container("romm")
    _seed_container("romm-db")
    assert chat._containers_mentioned_in("what about romm-db?") == ["romm-db"]


def test_matching_is_case_insensitive_and_ignores_surrounding_punctuation():
    _seed_container("Sonarr")
    assert chat._containers_mentioned_in("is SONARR ok?") == ["Sonarr"]


def test_an_operator_renamed_container_is_matched_by_its_display_name():
    _seed_container("romm-db")
    db.set_container_display_name("romm-db", "Media DB")
    try:
        # Asked by the name the operator actually sees, resolved to the real container.
        assert chat._containers_mentioned_in("what's up with Media DB?") == ["romm-db"]
    finally:
        db.reset_container_display_name("romm-db")


def test_live_log_fetching_is_bounded_to_a_couple_of_containers():
    for name in ("alpha", "beta", "gamma", "delta"):
        _seed_container(name)
    mentioned = chat._containers_mentioned_in("compare alpha beta gamma delta please")
    assert len(mentioned) == chat._LIVE_LOG_MAX_CONTAINERS


def test_an_over_long_log_is_truncated_to_the_most_recent_output():
    _seed_container()
    with patch("app.chat.get_container_logs_since", return_value="x" * 50000):
        block = chat._live_logs_for("romm-db logs?")
    assert "truncated" in block
    assert len(block) < 50000


def test_a_failing_log_fetch_never_breaks_the_answer():
    """An unreachable Docker socket must degrade to "no live logs", not a failed reply."""
    _seed_container()
    with patch("app.chat.get_container_logs_since", side_effect=RuntimeError("socket gone")):
        assert chat._live_logs_for("romm-db logs?") == ""


def test_a_container_with_no_recent_output_says_so_explicitly():
    _seed_container()
    with patch("app.chat.get_container_logs_since", return_value=""):
        block = chat._live_logs_for("romm-db logs?")
    assert "no log output" in block


def test_answer_appends_live_logs_to_the_system_prompt():
    _seed_container()
    with patch("app.chat.get_container_logs_since", return_value="ERROR connection reset"), \
         patch("app.chat.ai_provider.complete_chat", return_value="ok") as mock_chat:
        chat.answer([{"role": "user", "content": "what do romm-db's logs say?"}])

    system = mock_chat.call_args.kwargs["system"]
    assert "## Versions" in system            # the snapshot is still there
    assert "ERROR connection reset" in system  # and the live logs are appended to it


def test_live_logs_are_keyed_off_the_newest_turn_only():
    """A follow-up that names nothing must not re-pull logs for a container mentioned earlier --
    that would re-fetch the same output on every subsequent message."""
    _seed_container()
    with patch("app.chat.get_container_logs_since", return_value="stale") as mock_logs, \
         patch("app.chat.ai_provider.complete_chat", return_value="ok"):
        chat.answer([
            {"role": "user", "content": "tell me about romm-db"},
            {"role": "assistant", "content": "it's fine"},
            {"role": "user", "content": "and now?"},
        ])
    mock_logs.assert_not_called()


# ---------------------------------------------------------------------------
# The assistant advises; it just can't act
# ---------------------------------------------------------------------------

def test_prompt_asks_for_real_advice_not_just_observation():
    """A real-world report: asked "what do you think I should do?", the assistant answered "I
    cannot tell you what you should do, as I am an observation tool and cannot take action or
    offer advice." The read-only rule is about ACTIONS -- it was never meant to gag the model's
    opinions, which are the entire reason for talking to it. These pin the prompt's stance so a
    future edit can't quietly slide back into refusing to help."""
    prompt = chat.SYSTEM_PROMPT_HEADER.lower()
    # Explicitly invites advice/recommendations...
    assert "advice" in prompt
    assert "recommend" in prompt
    # ...and explicitly rules out the refusal that was actually observed.
    assert "i can't advise you" in prompt
    # The limit is scoped to actions, and said so in as many words.
    assert "about actions, not about opinions" in prompt


def test_prompt_still_forbids_taking_action_itself():
    """The advice-giving stance must not have loosened the actual constraint."""
    prompt = chat.SYSTEM_PROMPT_HEADER.lower()
    assert "cannot do is carry out changes yourself" in prompt
    assert "you can only read" in prompt
    assert "never claim to have done something" in prompt


def test_prompt_describes_the_two_narrow_proposal_exceptions():
    """Pinned so a future edit can't silently drop the model's own knowledge of the contract
    _extract_proposed_actions() below parses -- the two action types, the fence label, and that
    a proposal only takes effect on an explicit operator confirm."""
    prompt = chat.SYSTEM_PROMPT_HEADER.lower()
    assert "silence_findings" in prompt or "silence findings" in prompt
    assert "add_custom_rule" in prompt
    assert "action-proposal" in prompt
    assert "only takes effect once the operator clicks confirm" in prompt


# ---------------------------------------------------------------------------
# _extract_proposed_actions() -- parsing and validating the model's own proposal
# ---------------------------------------------------------------------------

def test_a_reply_with_no_action_block_returns_it_unchanged_with_no_actions():
    text, actions = chat._extract_proposed_actions("Just a normal reply, nothing to propose.")
    assert text == "Just a normal reply, nothing to propose."
    assert actions == []


def test_a_well_formed_silence_findings_proposal_is_parsed_and_stripped_from_the_text():
    reply = (
        "Sure, I'll propose silencing those.\n\n"
        "```action-proposal\n"
        '{"actions": [{"type": "silence_findings", "source": "logs", '
        '"subjects": ["radarr", "sonarr"], "title_contains": "Unable to parse", '
        '"reason": "Normal *arr/Prowlarr behavior."}]}\n'
        "```"
    )
    text, actions = chat._extract_proposed_actions(reply)
    assert "action-proposal" not in text
    assert "Sure, I'll propose silencing those." in text
    assert len(actions) == 1
    assert actions[0] == {
        "type": "silence_findings", "source": "logs", "subjects": ["radarr", "sonarr"],
        "title_contains": "Unable to parse", "reason": "Normal *arr/Prowlarr behavior.",
    }


def test_a_well_formed_add_custom_rule_proposal_is_parsed():
    reply = (
        "I'll add a standing rule.\n\n"
        "```action-proposal\n"
        '{"actions": [{"type": "add_custom_rule", "source": "logs", "rule_type": "exclude", '
        '"name": "Ignore *arr parse errors", '
        '"instruction": "Never flag Unable to parse for *arr apps.", "reason": "So it never comes back."}]}\n'
        "```"
    )
    text, actions = chat._extract_proposed_actions(reply)
    assert len(actions) == 1
    assert actions[0]["type"] == "add_custom_rule"
    assert actions[0]["rule_type"] == "exclude"
    assert actions[0]["name"] == "Ignore *arr parse errors"
    assert actions[0]["instruction"] == "Never flag Unable to parse for *arr apps."


def test_multiple_actions_in_one_block_are_all_parsed():
    reply = (
        "```action-proposal\n"
        '{"actions": ['
        '{"type": "silence_findings", "source": "logs", "subjects": ["radarr"], "title_contains": "x", "reason": "a"},'
        '{"type": "add_custom_rule", "source": "logs", "rule_type": "watch", "name": "n", "instruction": "y", "reason": "b"}'
        "]}\n"
        "```"
    )
    _, actions = chat._extract_proposed_actions(reply)
    assert [a["type"] for a in actions] == ["silence_findings", "add_custom_rule"]


def test_malformed_json_in_the_block_yields_no_actions_but_a_clean_reply():
    reply = "Here's my answer.\n\n```action-proposal\nnot valid json at all\n```"
    text, actions = chat._extract_proposed_actions(reply)
    assert "Here's my answer." in text
    assert actions == []


def test_an_action_missing_required_fields_is_dropped():
    reply = '```action-proposal\n{"actions": [{"type": "silence_findings", "source": "logs"}]}\n```'
    _, actions = chat._extract_proposed_actions(reply)
    assert actions == []


def test_an_add_custom_rule_missing_a_name_is_dropped():
    reply = (
        '```action-proposal\n{"actions": [{"type": "add_custom_rule", "source": "logs", '
        '"rule_type": "exclude", "instruction": "y", "reason": "z"}]}\n```'
    )
    _, actions = chat._extract_proposed_actions(reply)
    assert actions == []


def test_an_unrecognized_action_type_is_dropped():
    reply = '```action-proposal\n{"actions": [{"type": "delete_everything", "source": "logs"}]}\n```'
    _, actions = chat._extract_proposed_actions(reply)
    assert actions == []


def test_an_invalid_source_is_dropped():
    reply = (
        '```action-proposal\n{"actions": [{"type": "silence_findings", "source": "updates", '
        '"subjects": ["x"], "title_contains": "y", "reason": "z"}]}\n```'
    )
    _, actions = chat._extract_proposed_actions(reply)
    assert actions == []


def test_extra_unrecognized_fields_on_a_valid_action_are_dropped():
    """Only the fields chat_actions.py actually understands are carried through -- anything else
    the model happened to include is never trusted with a free ride into a Confirm button."""
    reply = (
        '```action-proposal\n{"actions": [{"type": "silence_findings", "source": "logs", '
        '"subjects": ["x"], "title_contains": "y", "reason": "z", "sneaky": "field"}]}\n```'
    )
    _, actions = chat._extract_proposed_actions(reply)
    assert "sneaky" not in actions[0]


def test_answer_returns_the_actions_parsed_from_the_models_reply():
    reply = (
        '```action-proposal\n{"actions": [{"type": "add_custom_rule", "source": "compose", '
        '"rule_type": "watch", "name": "Flag this", "instruction": "flag this", "reason": "why not"}]}\n```'
    )
    with patch("app.chat.ai_provider.complete_chat", return_value=reply):
        text, actions = chat.answer([{"role": "user", "content": "add a rule"}])
    assert "action-proposal" not in text
    assert len(actions) == 1
    assert actions[0]["source"] == "compose"

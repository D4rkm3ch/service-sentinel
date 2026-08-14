"""summarizer._custom_rules_prompt_block and its wiring into analyze_logs_batch/
review_compose_file's own system prompts -- the read-back half of the chat's action-proposal
feature (see app/chat.py, app/chat_actions.py, db.ai_custom_rules). A rule added through chat
must genuinely reshape the NEXT check's own AI call, not just today's already-open findings."""

from unittest.mock import patch

from app import db, summarizer

db.init_db()


def _clear():
    for rule in db.list_ai_custom_rules():
        db.remove_ai_custom_rule(rule["id"])


def setup_function(_):
    _clear()


def teardown_function(_):
    _clear()


# ---------------------------------------------------------------------------
# _custom_rules_prompt_block
# ---------------------------------------------------------------------------

def test_empty_when_there_are_no_rules():
    assert summarizer._custom_rules_prompt_block("logs") == ""


def test_formats_an_exclude_rule():
    db.add_ai_custom_rule("logs", "exclude", "Never flag Unable to...", "Never flag Unable to parse for *arr apps.")
    block = summarizer._custom_rules_prompt_block("logs")
    assert "Never flag this as an issue" in block
    assert "Never flag Unable to parse for *arr apps." in block


def test_formats_a_watch_rule():
    db.add_ai_custom_rule("compose", "watch", "Always flag ports pu...", "Always flag ports published without a firewall.")
    block = summarizer._custom_rules_prompt_block("compose")
    assert "Always flag this as an issue" in block
    assert "Always flag ports published without a firewall." in block


def test_multiple_rules_all_appear():
    db.add_ai_custom_rule("logs", "exclude", "rule one", "rule one")
    db.add_ai_custom_rule("logs", "watch", "rule two", "rule two")
    block = summarizer._custom_rules_prompt_block("logs")
    assert "rule one" in block
    assert "rule two" in block


def test_a_logs_rule_never_appears_in_the_compose_block():
    db.add_ai_custom_rule("logs", "exclude", "only for logs", "only for logs")
    assert "only for logs" not in summarizer._custom_rules_prompt_block("compose")


# ---------------------------------------------------------------------------
# Wired into the real review calls' system prompts
# ---------------------------------------------------------------------------

def test_analyze_logs_batch_includes_custom_rules_in_the_system_prompt():
    db.add_ai_custom_rule("logs", "exclude", "Never flag Unable to...", "Never flag Unable to parse for *arr apps.")
    captured = {}

    def _fake(system, user_message, max_tokens):
        captured["system"] = system
        return "[]"

    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake):
        summarizer.analyze_logs_batch({"radarr": "some log text"})

    assert "Never flag Unable to parse for *arr apps." in captured["system"]


def test_analyze_logs_batch_omits_the_block_with_no_rules():
    captured = {}

    def _fake(system, user_message, max_tokens):
        captured["system"] = system
        return "[]"

    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake):
        summarizer.analyze_logs_batch({"radarr": "some log text"})

    assert "standing rules" not in captured["system"]


def test_review_compose_file_includes_custom_rules_in_the_system_prompt():
    # A watch rule makes a second, real call to summarizer._enforce_custom_rules (see
    # test_summarizer_rule_enforcement.py) even when the primary pass found nothing -- so this
    # captures every system prompt seen, not just the last one, and checks the primary call's own
    # (the first).
    db.add_ai_custom_rule("compose", "watch", "Always flag ports pu...", "Always flag ports published without a firewall.")
    captured = []

    def _fake(system, user_message, max_tokens):
        captured.append(system)
        return "[]"

    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake):
        summarizer.review_compose_file("/compose/x.yaml", "services:\n  web:\n    image: nginx")

    assert "Always flag ports published without a firewall." in captured[0]


def test_logs_rules_never_leak_into_the_compose_review_prompt():
    db.add_ai_custom_rule("logs", "exclude", "only for logs", "only for logs")
    captured = {}

    def _fake(system, user_message, max_tokens):
        captured["system"] = system
        return "[]"

    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake):
        summarizer.review_compose_file("/compose/x.yaml", "services:\n  web:\n    image: nginx")

    assert "only for logs" not in captured["system"]

"""db.py's ai_custom_rules helpers -- an operator's own standing instructions to the Runtime/
Configuration AI reviewers, added through the chat widget's action-proposal feature (see
app/chat.py, app/chat_actions.py) and read back into every future review's own system prompt
(see summarizer.py's _custom_rules_prompt_block)."""

from app import db

db.init_db()


def _clear():
    for rule in db.list_ai_custom_rules():
        db.remove_ai_custom_rule(rule["id"])


def setup_function(_):
    _clear()


def teardown_function(_):
    _clear()


def test_no_rules_by_default():
    assert db.list_ai_custom_rules() == []
    assert db.list_ai_custom_rules("logs") == []


def test_add_and_list_round_trips():
    rule_id = db.add_ai_custom_rule(
        "logs", "exclude", "Ignore *arr parse errors", "Never flag 'Unable to parse' for *arr apps."
    )
    rules = db.list_ai_custom_rules("logs")
    assert len(rules) == 1
    assert rules[0]["id"] == rule_id
    assert rules[0]["source"] == "logs"
    assert rules[0]["rule_type"] == "exclude"
    assert rules[0]["name"] == "Ignore *arr parse errors"
    assert rules[0]["instruction"] == "Never flag 'Unable to parse' for *arr apps."
    assert rules[0]["created_at"]


def test_listing_is_scoped_by_source():
    db.add_ai_custom_rule("logs", "exclude", "n1", "a logs rule")
    db.add_ai_custom_rule("compose", "watch", "n2", "a compose rule")
    assert len(db.list_ai_custom_rules("logs")) == 1
    assert len(db.list_ai_custom_rules("compose")) == 1
    assert len(db.list_ai_custom_rules()) == 2


def test_remove_deletes_the_row():
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "temporary")
    db.remove_ai_custom_rule(rule_id)
    assert db.list_ai_custom_rules("logs") == []


def test_removing_a_nonexistent_id_is_a_harmless_no_op():
    db.remove_ai_custom_rule(999999)  # must not raise


def test_get_ai_custom_rule_returns_the_row():
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "i")
    row = db.get_ai_custom_rule(rule_id)
    assert row["id"] == rule_id
    assert row["name"] == "n"


def test_get_ai_custom_rule_returns_none_for_a_missing_id():
    assert db.get_ai_custom_rule(999999) is None


def test_update_ai_custom_rule_changes_name_type_and_instruction():
    rule_id = db.add_ai_custom_rule("logs", "exclude", "old name", "old instruction")
    db.update_ai_custom_rule(rule_id, "new name", "watch", "new instruction")
    row = db.get_ai_custom_rule(rule_id)
    assert row["name"] == "new name"
    assert row["rule_type"] == "watch"
    assert row["instruction"] == "new instruction"


def test_update_ai_custom_rule_leaves_source_untouched():
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "i")
    db.update_ai_custom_rule(rule_id, "n2", "watch", "i2")
    assert db.get_ai_custom_rule(rule_id)["source"] == "logs"


# ---------------------------------------------------------------------------
# Checkpoint rewind -- used whenever a custom rule changes so an ordinary Check now (not just
# the far more destructive Reset & re-check) picks it up. See app/chat_actions.py.
# ---------------------------------------------------------------------------

def test_rewind_logs_checkpoint_clears_it():
    db.set_log_watch_checkpoints(["custom-rules-test-container"])
    assert db.get_log_watch_checkpoints(["custom-rules-test-container"]) != {}
    db.rewind_logs_checkpoint()
    assert db.get_log_watch_checkpoints(["custom-rules-test-container"]) == {}


def test_rewind_logs_checkpoint_does_not_touch_findings():
    db.upsert_finding(
        source="logs", subject="custom-rules-test-container", title="t", category="c",
        severity="warning", description_markdown="d",
    )
    db.set_log_watch_checkpoints(["custom-rules-test-container"])
    db.rewind_logs_checkpoint()
    assert len(db.list_findings_for_subject("logs", "custom-rules-test-container", include_silenced=True)) == 1
    db.reset_logs_data(["custom-rules-test-container"])


def test_rewind_compose_checkpoint_clears_it():
    db.set_compose_file_hash("custom-rules-test.yaml", "abc123")
    assert db.get_compose_file_hash("custom-rules-test.yaml") == "abc123"
    db.rewind_compose_checkpoint()
    assert db.get_compose_file_hash("custom-rules-test.yaml") is None


def test_rewind_compose_checkpoint_does_not_touch_findings():
    db.upsert_finding(
        source="compose", subject="custom-rules-test.yaml", title="t", category="c",
        severity="warning", description_markdown="d",
    )
    db.set_compose_file_hash("custom-rules-test.yaml", "abc123")
    db.rewind_compose_checkpoint()
    assert len(db.list_findings_for_subject("compose", "custom-rules-test.yaml", include_silenced=True)) == 1
    db.reset_compose_data(["custom-rules-test.yaml"])

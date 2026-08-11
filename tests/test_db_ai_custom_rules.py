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
    rule_id = db.add_ai_custom_rule("logs", "exclude", "Never flag 'Unable to parse' for *arr apps.")
    rules = db.list_ai_custom_rules("logs")
    assert len(rules) == 1
    assert rules[0]["id"] == rule_id
    assert rules[0]["source"] == "logs"
    assert rules[0]["rule_type"] == "exclude"
    assert rules[0]["instruction"] == "Never flag 'Unable to parse' for *arr apps."
    assert rules[0]["created_at"]


def test_listing_is_scoped_by_source():
    db.add_ai_custom_rule("logs", "exclude", "a logs rule")
    db.add_ai_custom_rule("compose", "watch", "a compose rule")
    assert len(db.list_ai_custom_rules("logs")) == 1
    assert len(db.list_ai_custom_rules("compose")) == 1
    assert len(db.list_ai_custom_rules()) == 2


def test_remove_deletes_the_row():
    rule_id = db.add_ai_custom_rule("logs", "exclude", "temporary")
    db.remove_ai_custom_rule(rule_id)
    assert db.list_ai_custom_rules("logs") == []


def test_removing_a_nonexistent_id_is_a_harmless_no_op():
    db.remove_ai_custom_rule(999999)  # must not raise

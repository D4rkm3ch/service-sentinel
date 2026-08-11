"""Settings > Runtime/Configuration > "AI Custom Rules" -- the read-back half of the chat's
action-proposal feature (see app/chat.py, app/chat_actions.py, tests/test_db_ai_custom_rules.py,
tests/test_chat_actions.py). Adding a rule only ever happens through chat with an explicit
Confirm click; this page is read-only except for removal, covered here."""

from app import db

db.init_db()


def _clear():
    for rule in db.list_ai_custom_rules():
        db.remove_ai_custom_rule(rule["id"])


def setup_function(_):
    _clear()


def teardown_function(_):
    _clear()


def test_settings_shows_no_rules_by_default(client):
    text = client.get("/settings").text
    assert 'id="logs_custom_rules_empty"' in text
    assert 'id="compose_custom_rules_empty"' in text


def test_settings_lists_a_rule_for_its_own_module_only(client):
    db.add_ai_custom_rule("logs", "exclude", "Never flag Unable to parse for *arr apps.")
    text = client.get("/settings").text
    logs_section = text[text.index('id="logs_custom_rules_section"'):text.index('id="compose_custom_rules_section"')]
    assert "Never flag Unable to parse for *arr apps." in logs_section
    assert "Never flag" in logs_section  # the rule_type label
    compose_section = text[text.index('id="compose_custom_rules_section"'):]
    assert "Never flag Unable to parse for *arr apps." not in compose_section


def test_watch_rules_get_the_always_flag_label(client):
    db.add_ai_custom_rule("compose", "watch", "Always flag exposed ports without a firewall rule.")
    text = client.get("/settings").text
    assert "Always flag" in text
    assert "Always flag exposed ports without a firewall rule." in text


def test_updates_module_has_no_custom_rules_section(client):
    """Updates classifies release-note severity, not findings -- there's no reviewer of this
    kind for it to shape."""
    text = client.get("/settings").text
    assert 'id="updates_custom_rules_section"' not in text


def test_remove_route_deletes_the_rule(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "temporary rule")
    resp = client.post(f"/settings/ai-custom-rules/{rule_id}/remove")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert db.list_ai_custom_rules("logs") == []


def test_remove_route_reflected_on_the_page(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "temporary rule")
    assert "temporary rule" in client.get("/settings").text
    client.post(f"/settings/ai-custom-rules/{rule_id}/remove")
    assert "temporary rule" not in client.get("/settings").text


def test_remove_route_on_a_nonexistent_id_is_still_ok(client):
    resp = client.post("/settings/ai-custom-rules/999999/remove")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

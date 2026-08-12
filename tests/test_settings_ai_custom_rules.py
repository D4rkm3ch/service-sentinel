"""Settings > "AI Custom Rules" -- one consolidated panel (between Connections & Access and
Versions) covering both Runtime and Configuration rules, the read/edit/delete-back half of the
chat's action-proposal feature (see app/chat.py, app/chat_actions.py,
tests/test_db_ai_custom_rules.py, tests/test_chat_actions.py). Adding a new rule only ever
happens through chat with an explicit Confirm click; this page can edit or delete an existing
one, and undo a delete via restore_ai_custom_rule."""

from app import db

db.init_db()


def _clear():
    for rule in db.list_ai_custom_rules():
        db.remove_ai_custom_rule(rule["id"])


def setup_function(_):
    _clear()


def teardown_function(_):
    _clear()


def test_settings_shows_the_empty_state_by_default(client):
    text = client.get("/settings").text
    assert 'id="custom_rules_empty"' in text
    empty_idx = text.index('id="custom_rules_empty"')
    # Not hidden -- the empty state is what's actually shown when there are no rules.
    assert "hidden" not in text[empty_idx:empty_idx + 60]
    assert "No rules created yet" in text
    table_idx = text.index('id="custom_rules_table"')
    assert "hidden" in text[table_idx:table_idx + 60]


def test_settings_lists_a_rule_with_its_name_and_category(client):
    db.add_ai_custom_rule("logs", "exclude", "Ignore *arr parse errors", "Never flag Unable to parse for *arr apps.")
    text = client.get("/settings").text
    assert "Ignore *arr parse errors" in text
    assert "Never flag Unable to parse for *arr apps." in text
    assert "Runtime" in text
    assert "Never flag" in text  # the rule_type label


def test_a_compose_rule_shows_the_configuration_category(client):
    db.add_ai_custom_rule("compose", "watch", "Watch exposed ports", "Always flag exposed ports without a firewall rule.")
    text = client.get("/settings").text
    assert "Configuration" in text
    assert "Always flag" in text


def test_the_table_has_sortable_column_headers(client):
    text = client.get("/settings").text
    assert "sortCustomRulesTable('name')" in text
    assert "sortCustomRulesTable('rule_type')" in text
    assert "sortCustomRulesTable('category')" in text
    assert "sortCustomRulesTable('instruction')" in text


def test_settings_page_has_exactly_one_ai_custom_rules_panel(client):
    """It used to be duplicated per module (Runtime/Configuration each had their own) -- now
    there's exactly one consolidated panel covering both."""
    text = client.get("/settings").text
    assert text.count('id="settings-ai-custom-rules-body"') == 1


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

def test_remove_route_deletes_the_rule(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "temporary rule")
    resp = client.post(f"/settings/ai-custom-rules/{rule_id}/remove")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert db.list_ai_custom_rules("logs") == []


def test_remove_route_reflected_on_the_page(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "temporary rule")
    assert "temporary rule" in client.get("/settings").text
    client.post(f"/settings/ai-custom-rules/{rule_id}/remove")
    assert "temporary rule" not in client.get("/settings").text


def test_remove_route_on_a_nonexistent_id_is_still_ok(client):
    resp = client.post("/settings/ai-custom-rules/999999/remove")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_remove_route_rewinds_the_logs_checkpoint(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "i")
    db.set_log_watch_checkpoints(["settings-remove-test"])
    client.post(f"/settings/ai-custom-rules/{rule_id}/remove")
    assert db.get_log_watch_checkpoints(["settings-remove-test"]) == {}


def test_remove_route_rewinds_the_compose_checkpoint(client):
    rule_id = db.add_ai_custom_rule("compose", "watch", "n", "i")
    db.set_compose_file_hash("settings-remove-test.yaml", "abc")
    client.post(f"/settings/ai-custom-rules/{rule_id}/remove")
    assert db.get_compose_file_hash("settings-remove-test.yaml") is None


# ---------------------------------------------------------------------------
# Update (pencil-edit)
# ---------------------------------------------------------------------------

def test_update_route_changes_name_type_and_instruction(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "old name", "old instruction")
    resp = client.post(f"/settings/ai-custom-rules/{rule_id}/update", data={
        "name": "new name", "rule_type": "watch", "instruction": "new instruction",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["rule"]["name"] == "new name"
    assert data["rule"]["rule_type"] == "watch"
    assert data["rule"]["instruction"] == "new instruction"
    row = db.get_ai_custom_rule(rule_id)
    assert row["name"] == "new name"


def test_update_route_leaves_source_untouched(client):
    rule_id = db.add_ai_custom_rule("compose", "watch", "n", "i")
    client.post(f"/settings/ai-custom-rules/{rule_id}/update", data={
        "name": "n2", "rule_type": "exclude", "instruction": "i2",
    })
    assert db.get_ai_custom_rule(rule_id)["source"] == "compose"


def test_update_route_rejects_an_empty_name(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "i")
    resp = client.post(f"/settings/ai-custom-rules/{rule_id}/update", data={
        "name": "   ", "rule_type": "exclude", "instruction": "i",
    })
    assert resp.json()["ok"] is False
    assert db.get_ai_custom_rule(rule_id)["name"] == "n"


def test_update_route_rejects_an_unknown_rule_type(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "i")
    resp = client.post(f"/settings/ai-custom-rules/{rule_id}/update", data={
        "name": "n", "rule_type": "delete_everything", "instruction": "i",
    })
    assert resp.json()["ok"] is False


def test_update_route_rejects_an_empty_instruction(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "i")
    resp = client.post(f"/settings/ai-custom-rules/{rule_id}/update", data={
        "name": "n", "rule_type": "exclude", "instruction": "   ",
    })
    assert resp.json()["ok"] is False


def test_update_route_on_a_nonexistent_id_reports_failure(client):
    resp = client.post("/settings/ai-custom-rules/999999/update", data={
        "name": "n", "rule_type": "exclude", "instruction": "i",
    })
    assert resp.json()["ok"] is False


def test_update_route_rewinds_the_checkpoint_for_the_rules_source(client):
    rule_id = db.add_ai_custom_rule("logs", "exclude", "n", "i")
    db.set_log_watch_checkpoints(["settings-update-test"])
    client.post(f"/settings/ai-custom-rules/{rule_id}/update", data={
        "name": "n2", "rule_type": "watch", "instruction": "i2",
    })
    assert db.get_log_watch_checkpoints(["settings-update-test"]) == {}


# ---------------------------------------------------------------------------
# Restore (the other half of delete's Undo)
# ---------------------------------------------------------------------------

def test_restore_route_creates_a_new_rule(client):
    resp = client.post("/settings/ai-custom-rules/restore", data={
        "source": "logs", "rule_type": "exclude", "name": "n", "instruction": "i",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["rule"]["name"] == "n"
    assert data["rule"]["source"] == "logs"
    assert len(db.list_ai_custom_rules("logs")) == 1


def test_restore_route_rejects_an_unknown_source(client):
    resp = client.post("/settings/ai-custom-rules/restore", data={
        "source": "updates", "rule_type": "exclude", "name": "n", "instruction": "i",
    })
    assert resp.json()["ok"] is False
    assert db.list_ai_custom_rules() == []


def test_restore_route_rewinds_the_checkpoint(client):
    db.set_compose_file_hash("settings-restore-test.yaml", "abc")
    client.post("/settings/ai-custom-rules/restore", data={
        "source": "compose", "rule_type": "watch", "name": "n", "instruction": "i",
    })
    assert db.get_compose_file_hash("settings-restore-test.yaml") is None

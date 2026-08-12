"""app/chat_actions.py -- the mutating half of the chat's action-proposal feature (see
app/chat.py's own docstring). Every test here goes through execute(), the single entry point
main.py's POST /chat/confirm-action calls -- never chat.py's own extraction, since that module
stays strictly read-only and this is what turns a confirmed proposal into a real change."""

from app import chat_actions, db

db.init_db()


def _clear():
    for rule in db.list_ai_custom_rules():
        db.remove_ai_custom_rule(rule["id"])
    with db.get_conn() as conn:
        conn.execute("DELETE FROM findings WHERE subject LIKE 'chatactions-%'")


def setup_function(_):
    _clear()


def teardown_function(_):
    _clear()


def _seed_finding(subject, title, source="logs", status="active"):
    db.upsert_finding(
        source=source, subject=subject, title=title, category="reliability",
        severity="warning", description_markdown="desc", suggested_fix=None,
    )
    if status == "silenced":
        rows = db.list_findings_for_subject(source, subject, include_silenced=True)
        db.set_finding_status(rows[0]["id"], "silenced")


# ---------------------------------------------------------------------------
# silence_findings
# ---------------------------------------------------------------------------

def test_silence_findings_silences_every_matching_active_finding():
    _seed_finding("chatactions-radarr", "Unable to parse release title")
    _seed_finding("chatactions-sonarr", "Unable to parse release title")
    _seed_finding("chatactions-sonarr", "Totally unrelated issue")

    result = chat_actions.execute({
        "type": "silence_findings", "source": "logs",
        "subjects": ["chatactions-radarr", "chatactions-sonarr"],
        "title_contains": "Unable to parse",
    })

    assert result["ok"] is True
    assert "2" in result["message"]
    radarr = db.list_findings_for_subject("logs", "chatactions-radarr", include_silenced=True)
    sonarr = db.list_findings_for_subject("logs", "chatactions-sonarr", include_silenced=True)
    assert all(f["status"] == "silenced" for f in radarr if "Unable to parse" in f["title"])
    unrelated = [f for f in sonarr if f["title"] == "Totally unrelated issue"]
    assert unrelated[0]["status"] == "active"  # never touched


def test_silence_findings_matching_is_case_insensitive():
    _seed_finding("chatactions-lidarr", "unable to PARSE release")
    result = chat_actions.execute({
        "type": "silence_findings", "source": "logs", "subjects": ["chatactions-lidarr"],
        "title_contains": "Unable To Parse",
    })
    assert result["ok"] is True


def test_silence_findings_with_no_match_reports_nothing_changed():
    _seed_finding("chatactions-prowlarr", "Something else entirely")
    result = chat_actions.execute({
        "type": "silence_findings", "source": "logs", "subjects": ["chatactions-prowlarr"],
        "title_contains": "Unable to parse",
    })
    assert result["ok"] is False
    assert "nothing was changed" in result["message"]


def test_silence_findings_never_touches_an_already_silenced_finding():
    """list_findings_for_subject(include_silenced=False) already excludes these, but this pins
    the behavior at the execute() level too, not just the query."""
    _seed_finding("chatactions-bazarr", "Unable to parse subtitle", status="silenced")
    result = chat_actions.execute({
        "type": "silence_findings", "source": "logs", "subjects": ["chatactions-bazarr"],
        "title_contains": "Unable to parse",
    })
    assert result["ok"] is False


def test_silence_findings_rejects_a_bad_source():
    result = chat_actions.execute({
        "type": "silence_findings", "source": "updates", "subjects": ["x"], "title_contains": "y",
    })
    assert result["ok"] is False


def test_silence_findings_rejects_missing_subjects():
    result = chat_actions.execute({"type": "silence_findings", "source": "logs", "title_contains": "y"})
    assert result["ok"] is False


def test_silence_findings_rejects_empty_title_contains():
    result = chat_actions.execute({
        "type": "silence_findings", "source": "logs", "subjects": ["x"], "title_contains": "",
    })
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# add_custom_rule
# ---------------------------------------------------------------------------

def test_add_custom_rule_persists_the_rule():
    result = chat_actions.execute({
        "type": "add_custom_rule", "source": "logs", "rule_type": "exclude",
        "name": "Ignore *arr parse errors",
        "instruction": "Never flag Unable to parse for *arr apps.",
    })
    assert result["ok"] is True
    rules = db.list_ai_custom_rules("logs")
    assert len(rules) == 1
    assert rules[0]["rule_type"] == "exclude"
    assert rules[0]["name"] == "Ignore *arr parse errors"
    assert rules[0]["instruction"] == "Never flag Unable to parse for *arr apps."


def test_add_custom_rule_result_carries_the_new_rule_for_a_live_settings_table_update():
    """Settings' own AI Custom Rules table inserts this row live (see settings.html's
    insertCustomRuleRow) if the operator confirms a rule from chat while already on that page --
    needs the full row back, not just a success message."""
    result = chat_actions.execute({
        "type": "add_custom_rule", "source": "compose", "rule_type": "watch",
        "name": "n", "instruction": "i",
    })
    assert result["ok"] is True
    assert result["rule"]["name"] == "n"
    assert result["rule"]["source"] == "compose"
    assert result["rule"]["rule_type"] == "watch"
    assert result["rule"]["instruction"] == "i"
    assert result["rule"]["id"] == db.list_ai_custom_rules("compose")[0]["id"]


def test_add_custom_rule_message_mentions_the_right_module_name():
    result = chat_actions.execute({
        "type": "add_custom_rule", "source": "compose", "rule_type": "watch",
        "name": "n", "instruction": "x",
    })
    assert "Configuration" in result["message"]


def test_add_custom_rule_rejects_an_unknown_rule_type():
    result = chat_actions.execute({
        "type": "add_custom_rule", "source": "logs", "rule_type": "delete",
        "name": "n", "instruction": "x",
    })
    assert result["ok"] is False
    assert db.list_ai_custom_rules("logs") == []


def test_add_custom_rule_rejects_an_empty_instruction():
    result = chat_actions.execute({
        "type": "add_custom_rule", "source": "logs", "rule_type": "exclude",
        "name": "n", "instruction": "   ",
    })
    assert result["ok"] is False


def test_add_custom_rule_rejects_an_empty_name():
    result = chat_actions.execute({
        "type": "add_custom_rule", "source": "logs", "rule_type": "exclude",
        "name": "   ", "instruction": "x",
    })
    assert result["ok"] is False
    assert db.list_ai_custom_rules("logs") == []


def test_add_custom_rule_for_logs_rewinds_the_log_checkpoint():
    db.set_log_watch_checkpoints(["chatactions-radarr"])
    assert db.get_log_watch_checkpoints(["chatactions-radarr"]) != {}

    chat_actions.execute({
        "type": "add_custom_rule", "source": "logs", "rule_type": "exclude",
        "name": "n", "instruction": "x",
    })

    assert db.get_log_watch_checkpoints(["chatactions-radarr"]) == {}


def test_add_custom_rule_for_compose_rewinds_the_compose_checkpoint():
    db.set_compose_file_hash("chatactions-compose.yaml", "somehash")
    assert db.get_compose_file_hash("chatactions-compose.yaml") == "somehash"

    chat_actions.execute({
        "type": "add_custom_rule", "source": "compose", "rule_type": "watch",
        "name": "n", "instruction": "x",
    })

    assert db.get_compose_file_hash("chatactions-compose.yaml") is None


# ---------------------------------------------------------------------------
# execute() itself -- unknown/malformed input never raises
# ---------------------------------------------------------------------------

def test_execute_rejects_an_unrecognized_type():
    assert chat_actions.execute({"type": "do_something_else"})["ok"] is False


def test_execute_rejects_a_non_dict_action():
    assert chat_actions.execute(None)["ok"] is False
    assert chat_actions.execute("not a dict")["ok"] is False
    assert chat_actions.execute([1, 2, 3])["ok"] is False


def test_execute_rejects_an_action_with_no_type_at_all():
    assert chat_actions.execute({"source": "logs"})["ok"] is False

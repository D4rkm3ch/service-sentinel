"""One-time migration in db.init_db(): an install upgrading from before the AI provider moved
into Settings may still have ANTHROPIC_API_KEY/CLAUDE_MODEL set in its compose file -- carry
those into the database on first boot after the upgrade so the key keeps working without the
operator having to immediately re-enter it in the UI, but never overwrite a key already saved
from the Settings page. Runs init_db() against its own temp database file (monkeypatching
app.db.settings.db_path) rather than the shared session database every other test file uses,
since the whole point here is exercising what happens on a *fresh* database file with the env
vars present -- the shared database has long since had these settings seeded by other tests."""


from app import db


def test_env_key_and_model_are_carried_into_a_fresh_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "fresh.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-compose")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-8")

    db.init_db()

    assert db.get_anthropic_api_key() == "sk-ant-from-compose"
    assert db.get_anthropic_model() == "claude-opus-4-8"


def test_no_env_vars_leaves_a_fresh_database_at_ordinary_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "fresh.db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)

    db.init_db()

    assert db.get_anthropic_api_key() == ""
    assert db.get_anthropic_model() == "claude-sonnet-5"


def test_migration_never_overwrites_a_key_already_saved_from_settings(tmp_path, monkeypatch):
    db_path = tmp_path / "existing.db"
    monkeypatch.setattr(db.settings, "db_path", db_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    db.init_db()
    db.set_anthropic_api_key("sk-ant-chosen-in-the-ui")

    # Simulate a restart with the old compose-file env var still present -- re-running
    # init_db() (as happens on every app startup) must not clobber the UI-saved key.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-compose")
    db.init_db()

    assert db.get_anthropic_api_key() == "sk-ant-chosen-in-the-ui"


def test_migration_does_not_touch_a_model_already_changed_from_the_default(tmp_path, monkeypatch):
    db_path = tmp_path / "existing2.db"
    monkeypatch.setattr(db.settings, "db_path", db_path)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    db.init_db()
    db.set_anthropic_model("claude-haiku-4-5-20251001")

    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-8")
    db.init_db()

    assert db.get_anthropic_model() == "claude-haiku-4-5-20251001"

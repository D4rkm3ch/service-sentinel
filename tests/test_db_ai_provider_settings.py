"""db.py's get/set helpers for the AI provider settings (provider choice, each provider's API
key and model) -- moved off compose-file env vars and into the database so they're editable
from the Settings page without a redeploy. See app/ai_provider.py for how these get read."""

import pytest

from app import db

db.init_db()


@pytest.fixture(autouse=True)
def clean_settings():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_anthropic_model("claude-sonnet-5")
    db.set_anthropic_concurrency(db.AI_CONCURRENCY_DEFAULT)
    db.set_gemini_api_key("")
    db.set_gemini_model("gemini-2.5-flash")
    db.set_gemini_concurrency(db.AI_CONCURRENCY_DEFAULT)
    yield
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_anthropic_model("claude-sonnet-5")
    db.set_anthropic_concurrency(db.AI_CONCURRENCY_DEFAULT)
    db.set_gemini_api_key("")
    db.set_gemini_model("gemini-2.5-flash")
    db.set_gemini_concurrency(db.AI_CONCURRENCY_DEFAULT)


def test_ai_provider_defaults_to_anthropic():
    assert db.get_ai_provider() == "anthropic"


def test_ai_provider_round_trips():
    db.set_ai_provider("gemini")
    assert db.get_ai_provider() == "gemini"


def test_anthropic_key_defaults_empty_and_round_trips():
    assert db.get_anthropic_api_key() == ""
    db.set_anthropic_api_key("sk-ant-abc123")
    assert db.get_anthropic_api_key() == "sk-ant-abc123"


def test_anthropic_model_defaults_and_round_trips():
    assert db.get_anthropic_model() == "claude-sonnet-5"
    db.set_anthropic_model("claude-opus-4-8")
    assert db.get_anthropic_model() == "claude-opus-4-8"


def test_gemini_key_defaults_empty_and_round_trips():
    assert db.get_gemini_api_key() == ""
    db.set_gemini_api_key("AIzaSyABC123")
    assert db.get_gemini_api_key() == "AIzaSyABC123"


def test_gemini_model_defaults_and_round_trips():
    assert db.get_gemini_model() == "gemini-2.5-flash"
    db.set_gemini_model("gemini-2.5-pro")
    assert db.get_gemini_model() == "gemini-2.5-pro"


def test_anthropic_concurrency_defaults_and_round_trips():
    assert db.get_anthropic_concurrency() == db.AI_CONCURRENCY_DEFAULT
    db.set_anthropic_concurrency(9)
    assert db.get_anthropic_concurrency() == 9


def test_gemini_concurrency_defaults_and_round_trips():
    assert db.get_gemini_concurrency() == db.AI_CONCURRENCY_DEFAULT
    db.set_gemini_concurrency(1)
    assert db.get_gemini_concurrency() == 1


def test_concurrency_settings_are_independent_per_provider():
    db.set_anthropic_concurrency(3)
    db.set_gemini_concurrency(8)
    assert db.get_anthropic_concurrency() == 3
    assert db.get_gemini_concurrency() == 8

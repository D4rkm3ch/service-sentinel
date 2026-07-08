"""HTTP-level tests for the Settings page's new AI Provider panel: saving the active provider,
each provider's API key (write-only -- never echoed back to the page, "leave blank to keep"
semantics) and model choice, and that the page reflects current state (which provider is
active, whether each key is configured, which model is selected)."""

import pytest

from app import db


@pytest.fixture(autouse=True)
def clean_settings():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_anthropic_model("claude-sonnet-5")
    db.set_gemini_api_key("")
    db.set_gemini_model("gemini-2.5-flash")
    yield
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_anthropic_model("claude-sonnet-5")
    db.set_gemini_api_key("")
    db.set_gemini_model("gemini-2.5-flash")


def test_settings_page_shows_anthropic_selected_by_default(client):
    page = client.get("/settings")
    assert '<option value="anthropic" selected' in page.text
    assert "No API key configured yet." in page.text


def test_saving_the_provider_persists_and_reflects_on_the_page(client):
    resp = client.post("/settings/ai/provider", data={"ai_provider": "gemini"})
    assert resp.status_code == 200
    assert db.get_ai_provider() == "gemini"

    page = client.get("/settings")
    assert '<option value="gemini" selected' in page.text


def test_unknown_provider_is_rejected(client):
    resp = client.post("/settings/ai/provider", data={"ai_provider": "openai"})
    assert resp.status_code == 400
    assert db.get_ai_provider() == "anthropic"


def test_saving_an_anthropic_key_persists_it_but_never_echoes_it_back(client):
    resp = client.post("/settings/ai/anthropic-key", data={"api_key": "sk-ant-secret123"})
    assert resp.status_code == 200
    assert db.get_anthropic_api_key() == "sk-ant-secret123"

    page = client.get("/settings")
    assert "sk-ant-secret123" not in page.text
    assert "API key configured." in page.text


def test_blank_key_submission_does_not_overwrite_an_existing_key(client):
    db.set_anthropic_api_key("sk-ant-original")
    resp = client.post("/settings/ai/anthropic-key", data={"api_key": ""})
    assert resp.status_code == 200
    assert db.get_anthropic_api_key() == "sk-ant-original"


def test_saving_the_anthropic_model_persists_it(client):
    resp = client.post("/settings/ai/anthropic-model", data={"anthropic_model": "claude-opus-4-8"})
    assert resp.status_code == 200
    assert db.get_anthropic_model() == "claude-opus-4-8"


def test_unknown_anthropic_model_is_rejected(client):
    resp = client.post("/settings/ai/anthropic-model", data={"anthropic_model": "gpt-4"})
    assert resp.status_code == 400
    assert db.get_anthropic_model() == "claude-sonnet-5"


def test_saving_a_gemini_key_and_model_persists_both(client):
    resp = client.post("/settings/ai/gemini-key", data={"api_key": "AIzaSyABCDEF"})
    assert resp.status_code == 200
    assert db.get_gemini_api_key() == "AIzaSyABCDEF"

    resp = client.post("/settings/ai/gemini-model", data={"gemini_model": "gemini-2.5-pro"})
    assert resp.status_code == 200
    assert db.get_gemini_model() == "gemini-2.5-pro"

    page = client.get("/settings")
    assert "AIzaSyABCDEF" not in page.text


def test_unknown_gemini_model_is_rejected(client):
    resp = client.post("/settings/ai/gemini-model", data={"gemini_model": "gemini-1.0"})
    assert resp.status_code == 400
    assert db.get_gemini_model() == "gemini-2.5-flash"

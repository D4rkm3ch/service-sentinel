"""HTTP-level tests for the Settings page's AI Provider panel: saving the active provider, each
provider's API key and model, and the GitHub token -- all three keys are write-only (never
echoed back to the page) and, since the key-testing feature, tested live against the real
provider/GitHub before being persisted at all. A key that fails its test is never saved."""

from unittest.mock import patch

import pytest

from app import db


@pytest.fixture(autouse=True)
def clean_settings():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_anthropic_model("claude-sonnet-5")
    db.set_gemini_api_key("")
    db.set_gemini_model("gemini-2.5-flash")
    db.set_github_token("")
    yield
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_anthropic_model("claude-sonnet-5")
    db.set_gemini_api_key("")
    db.set_gemini_model("gemini-2.5-flash")
    db.set_github_token("")


def test_settings_page_shows_anthropic_selected_by_default(client):
    page = client.get("/settings")
    assert '<option value="anthropic" selected' in page.text


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


def test_a_key_that_passes_its_test_is_saved_but_never_echoed_back(client):
    with patch("app.main.ai_provider.test_anthropic_key", return_value=(True, "API key works.")):
        resp = client.post("/settings/ai/anthropic-key", data={"api_key": "sk-ant-secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "API key works."}
    assert db.get_anthropic_api_key() == "sk-ant-secret123"

    page = client.get("/settings")
    assert "sk-ant-secret123" not in page.text
    assert 'id="anthropic_api_key_field"' in page.text
    assert "disabled" in page.text[page.text.index('id="anthropic_api_key_field"'):page.text.index('id="anthropic_api_key_field"') + 300]


def test_a_key_that_fails_its_test_is_never_saved(client):
    with patch("app.main.ai_provider.test_anthropic_key", return_value=(False, "Invalid API key.")):
        resp = client.post("/settings/ai/anthropic-key", data={"api_key": "sk-ant-bad"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "message": "Invalid API key."}
    assert db.get_anthropic_api_key() == ""


def test_blank_key_submission_does_not_overwrite_an_existing_key_or_run_a_test(client):
    db.set_anthropic_api_key("sk-ant-original")
    with patch("app.main.ai_provider.test_anthropic_key") as mock_test:
        resp = client.post("/settings/ai/anthropic-key", data={"api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    mock_test.assert_not_called()
    assert db.get_anthropic_api_key() == "sk-ant-original"


def test_saving_the_anthropic_model_persists_it(client):
    resp = client.post("/settings/ai/anthropic-model", data={"anthropic_model": "claude-opus-4-8"})
    assert resp.status_code == 200
    assert db.get_anthropic_model() == "claude-opus-4-8"


def test_unknown_anthropic_model_is_rejected(client):
    resp = client.post("/settings/ai/anthropic-model", data={"anthropic_model": "gpt-4"})
    assert resp.status_code == 400
    assert db.get_anthropic_model() == "claude-sonnet-5"


def test_a_gemini_key_that_passes_its_test_is_saved(client):
    with patch("app.main.ai_provider.test_gemini_key", return_value=(True, "API key works.")):
        resp = client.post("/settings/ai/gemini-key", data={"api_key": "AIzaSyABCDEF"})
    assert resp.status_code == 200
    assert db.get_gemini_api_key() == "AIzaSyABCDEF"

    resp = client.post("/settings/ai/gemini-model", data={"gemini_model": "gemini-2.5-pro"})
    assert resp.status_code == 200
    assert db.get_gemini_model() == "gemini-2.5-pro"

    page = client.get("/settings")
    assert "AIzaSyABCDEF" not in page.text


def test_a_gemini_key_that_fails_its_test_is_never_saved(client):
    with patch("app.main.ai_provider.test_gemini_key", return_value=(False, "Invalid API key.")):
        resp = client.post("/settings/ai/gemini-key", data={"api_key": "AIzaBad"})
    assert resp.json() == {"ok": False, "message": "Invalid API key."}
    assert db.get_gemini_api_key() == ""


def test_unknown_gemini_model_is_rejected(client):
    resp = client.post("/settings/ai/gemini-model", data={"gemini_model": "gemini-1.0"})
    assert resp.status_code == 400
    assert db.get_gemini_model() == "gemini-2.5-flash"


def test_a_github_token_that_passes_its_test_is_saved_but_never_echoed_back(client):
    with patch("app.main.release_notes.test_github_token", return_value=(True, "Token works. Rate limit: 5000/hour.")):
        resp = client.post("/settings/ai/github-token", data={"api_key": "ghp_secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "Token works. Rate limit: 5000/hour."}
    assert db.get_github_token() == "ghp_secret123"

    page = client.get("/settings")
    assert "ghp_secret123" not in page.text


def test_a_github_token_that_fails_its_test_is_never_saved(client):
    with patch("app.main.release_notes.test_github_token", return_value=(False, "Invalid token.")):
        resp = client.post("/settings/ai/github-token", data={"api_key": "ghp_bad"})
    assert resp.json() == {"ok": False, "message": "Invalid token."}
    assert db.get_github_token() == ""


def test_blank_github_token_submission_does_not_run_a_test(client):
    with patch("app.main.release_notes.test_github_token") as mock_test:
        resp = client.post("/settings/ai/github-token", data={"api_key": ""})
    assert resp.json()["ok"] is False
    mock_test.assert_not_called()

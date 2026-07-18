"""HTTP-level tests for the Settings page's AI Provider panel: saving the active provider, each
provider's API key and model, and the GitHub token -- all three keys are write-only (never
echoed back to the page) and, since the key-testing feature, tested live against the real
provider/GitHub before being persisted at all. A key that fails its test is never saved."""

from unittest.mock import patch

import pytest

from app import db


def _reset_ai_settings():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_anthropic_model("claude-sonnet-5")
    db.set_anthropic_concurrency(db.AI_CONCURRENCY_DEFAULT)
    db.set_gemini_api_key("")
    db.set_gemini_model("gemini-2.5-flash")
    db.set_gemini_concurrency(db.AI_CONCURRENCY_DEFAULT)
    db.set_openai_api_key("")
    db.set_openai_model("gpt-5.1")
    db.set_openai_concurrency(db.AI_CONCURRENCY_DEFAULT)
    db.set_openai_compat_base_url("")
    db.set_openai_compat_model("")
    db.set_openai_compat_api_key("")
    db.set_openai_compat_concurrency(1)
    db.set_github_token("")


@pytest.fixture(autouse=True)
def clean_settings():
    _reset_ai_settings()
    yield
    _reset_ai_settings()


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
    resp = client.post("/settings/ai/provider", data={"ai_provider": "mistral"})
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


def test_saving_anthropic_concurrency_persists_it(client):
    resp = client.post("/settings/ai/anthropic-concurrency", data={"value": "7"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "value": 7}
    assert db.get_anthropic_concurrency() == 7


def test_saving_gemini_concurrency_persists_it(client):
    resp = client.post("/settings/ai/gemini-concurrency", data={"value": "1"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "value": 1}
    assert db.get_gemini_concurrency() == 1


def test_concurrency_boundary_values_are_accepted(client):
    resp = client.post("/settings/ai/anthropic-concurrency", data={"value": "10"})
    assert resp.json() == {"ok": True, "value": 10}
    resp = client.post("/settings/ai/anthropic-concurrency", data={"value": "1"})
    assert resp.json() == {"ok": True, "value": 1}


def test_concurrency_out_of_range_is_rejected_without_being_saved(client):
    resp = client.post("/settings/ai/anthropic-concurrency", data={"value": "11"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert db.get_anthropic_concurrency() == db.AI_CONCURRENCY_DEFAULT

    resp = client.post("/settings/ai/gemini-concurrency", data={"value": "0"})
    assert resp.json()["ok"] is False
    assert db.get_gemini_concurrency() == db.AI_CONCURRENCY_DEFAULT


def test_concurrency_non_numeric_value_is_rejected(client):
    resp = client.post("/settings/ai/anthropic-concurrency", data={"value": "abc"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert db.get_anthropic_concurrency() == db.AI_CONCURRENCY_DEFAULT


def test_openai_and_compat_are_selectable_providers(client):
    resp = client.post("/settings/ai/provider", data={"ai_provider": "openai"})
    assert resp.status_code == 200
    assert db.get_ai_provider() == "openai"

    resp = client.post("/settings/ai/provider", data={"ai_provider": "openai_compat"})
    assert resp.status_code == 200
    assert db.get_ai_provider() == "openai_compat"

    page = client.get("/settings")
    assert '<option value="openai_compat" selected' in page.text


def test_an_openai_key_that_passes_its_test_is_saved_but_never_echoed_back(client):
    with patch("app.main.ai_provider.test_openai_key", return_value=(True, "API key works.")):
        resp = client.post("/settings/ai/openai-key", data={"api_key": "sk-openai-secret1"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "API key works."}
    assert db.get_openai_api_key() == "sk-openai-secret1"

    page = client.get("/settings")
    assert "sk-openai-secret1" not in page.text


def test_an_openai_key_that_fails_its_test_is_never_saved(client):
    with patch("app.main.ai_provider.test_openai_key", return_value=(False, "Invalid API key.")):
        resp = client.post("/settings/ai/openai-key", data={"api_key": "sk-bad"})
    assert resp.json() == {"ok": False, "message": "Invalid API key."}
    assert db.get_openai_api_key() == ""


def test_saving_the_openai_model_persists_it_and_unknown_is_rejected(client):
    resp = client.post("/settings/ai/openai-model", data={"openai_model": "gpt-5-mini"})
    assert resp.status_code == 200
    assert db.get_openai_model() == "gpt-5-mini"

    resp = client.post("/settings/ai/openai-model", data={"openai_model": "gpt-3.5-turbo"})
    assert resp.status_code == 400
    assert db.get_openai_model() == "gpt-5-mini"


def test_openai_compat_endpoint_that_passes_its_test_is_saved(client):
    with patch("app.main.ai_provider.test_openai_compat", return_value=(True, "Endpoint works.")) as mock_test:
        resp = client.post("/settings/ai/openai-compat", data={
            "base_url": "http://ollama:11434/v1/", "model": "llama3.1:8b", "api_key": "tok-abc",
        })
    assert resp.json() == {"ok": True, "message": "Endpoint works."}
    # Trailing slash is normalized off before the test and the save.
    mock_test.assert_called_once_with("http://ollama:11434/v1", "tok-abc")
    assert db.get_openai_compat_base_url() == "http://ollama:11434/v1"
    assert db.get_openai_compat_model() == "llama3.1:8b"
    assert db.get_openai_compat_api_key() == "tok-abc"

    page = client.get("/settings")
    assert "tok-abc" not in page.text
    assert "http://ollama:11434/v1" in page.text


def test_openai_compat_blank_key_keeps_the_existing_key(client):
    db.set_openai_compat_api_key("tok-original")
    with patch("app.main.ai_provider.test_openai_compat", return_value=(True, "Endpoint works.")) as mock_test:
        resp = client.post("/settings/ai/openai-compat", data={
            "base_url": "http://lmstudio:1234/v1", "model": "qwen3-8b", "api_key": "",
        })
    assert resp.json()["ok"] is True
    # The already-saved key is what gets tested against the endpoint, and it survives the save.
    mock_test.assert_called_once_with("http://lmstudio:1234/v1", "tok-original")
    assert db.get_openai_compat_api_key() == "tok-original"


def test_openai_compat_requires_base_url_and_model(client):
    with patch("app.main.ai_provider.test_openai_compat") as mock_test:
        resp = client.post("/settings/ai/openai-compat", data={"base_url": "", "model": "x"})
    assert resp.json()["ok"] is False
    mock_test.assert_not_called()
    assert db.get_openai_compat_base_url() == ""


def test_openai_compat_failed_test_saves_nothing(client):
    with patch("app.main.ai_provider.test_openai_compat", return_value=(False, "Couldn't reach the endpoint.")):
        resp = client.post("/settings/ai/openai-compat", data={
            "base_url": "http://nowhere:1/v1", "model": "llama3.1:8b",
        })
    assert resp.json()["ok"] is False
    assert db.get_openai_compat_base_url() == ""
    assert db.get_openai_compat_model() == ""


def test_saving_openai_and_compat_concurrency_persists_them(client):
    resp = client.post("/settings/ai/openai-concurrency", data={"value": "6"})
    assert resp.json() == {"ok": True, "value": 6}
    assert db.get_openai_concurrency() == 6

    resp = client.post("/settings/ai/openai_compat-concurrency", data={"value": "2"})
    assert resp.json() == {"ok": True, "value": 2}
    assert db.get_openai_compat_concurrency() == 2


def test_concurrency_settings_are_independent_and_reflected_on_the_page(client):
    client.post("/settings/ai/anthropic-concurrency", data={"value": "6"})
    client.post("/settings/ai/gemini-concurrency", data={"value": "2"})
    assert db.get_anthropic_concurrency() == 6
    assert db.get_gemini_concurrency() == 2

    page = client.get("/settings")
    assert 'id="anthropic_concurrency_input"' in page.text
    assert 'id="gemini_concurrency_input"' in page.text

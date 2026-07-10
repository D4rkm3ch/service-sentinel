"""1. The API key field (Anthropic, Gemini, GitHub token) must render disabled with a "Change"
   button once a key is configured, and enabled with a "Test & Save" button when it isn't --
   so the user can tell at a glance whether a key is on file without an active, blank-looking
   textbox implying otherwise.
2. anthropic==0.34.2 (as pinned before this fix) is incompatible with httpx==0.28.1 -- every
   real anthropic.Anthropic() call raised TypeError: Client.__init__() got an unexpected keyword
   argument 'proxies' at client *construction* time, before any request even went out. Guards
   against that pin regressing, without needing real network access to prove it (the bug fires
   on construction, so no call has to actually complete)."""

from pathlib import Path
from unittest.mock import patch

import anthropic

from app import ai_provider, db

ROOT = Path(__file__).resolve().parent.parent


def test_configured_key_field_renders_disabled_with_a_change_button(client):
    db.set_anthropic_api_key("sk-ant-existing")
    try:
        page = client.get("/settings")
        field_start = page.text.index('id="anthropic_api_key_field"')
        field_tag = page.text[field_start:page.text.index(">", field_start)]
        assert "disabled" in field_tag
        assert "sk-ant-existing" not in page.text

        btn_start = page.text.index('id="anthropic_key_action_btn"')
        btn_html = page.text[btn_start:btn_start + 200]
        assert "Change" in btn_html
    finally:
        db.set_anthropic_api_key("")


def test_unconfigured_key_field_renders_enabled_with_a_test_and_save_button(client):
    db.set_anthropic_api_key("")
    page = client.get("/settings")
    field_start = page.text.index('id="anthropic_api_key_field"')
    field_tag = page.text[field_start:page.text.index(">", field_start)]
    assert "disabled" not in field_tag

    btn_start = page.text.index('id="anthropic_key_action_btn"')
    btn_html = page.text[btn_start:btn_start + 200]
    assert "Test &amp; Save" in btn_html or "Test & Save" in btn_html


def test_github_token_field_present_and_reflects_configured_state(client):
    db.set_github_token("ghp_existing")
    try:
        page = client.get("/settings")
        field_start = page.text.index('id="github_api_key_field"')
        field_tag = page.text[field_start:page.text.index(">", field_start)]
        assert "disabled" in field_tag
        assert "ghp_existing" not in page.text
    finally:
        db.set_github_token("")


def test_anthropic_client_construction_does_not_raise_the_httpx_proxies_type_error():
    """Reproduces the exact regression: anthropic==0.34.2 + httpx==0.28.1 raised a TypeError
    inside Anthropic.__init__ itself (httpx 0.28 dropped the 'proxies' kwarg anthropic 0.34.2
    still passed) -- before any network call. Constructing the client is enough to prove it;
    no request needs to actually go out."""
    anthropic.Anthropic(api_key="sk-ant-no-network-needed-for-this-check")


def test_test_anthropic_key_reports_invalid_key_cleanly_not_a_crash():
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.models.list.side_effect = anthropic.AuthenticationError(
            message="invalid x-api-key", response=_fake_response(401), body=None,
        )
        ok, message = ai_provider.test_anthropic_key("sk-ant-bad")
    assert ok is False
    assert "Invalid" in message


def test_test_anthropic_key_reports_success():
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.models.list.return_value = ["model-1"]
        ok, message = ai_provider.test_anthropic_key("sk-ant-good")
    assert ok is True


def test_test_gemini_key_reports_invalid_key_cleanly_not_a_crash():
    from google.genai import errors as genai_errors

    with patch("app.ai_provider.genai.Client") as mock_client_cls:
        mock_client_cls.return_value.models.list.side_effect = genai_errors.ClientError(
            code=400, response_json={"error": {"message": "API key not valid"}},
        )
        ok, message = ai_provider.test_gemini_key("bad-key")
    assert ok is False
    assert "Invalid" in message


def test_requirements_txt_pins_an_httpx_compatible_anthropic_version():
    text = (ROOT / "requirements.txt").read_text()
    for line in text.splitlines():
        if line.startswith("anthropic=="):
            version = line.split("==")[1].strip()
            major, minor = (int(p) for p in version.split(".")[:2])
            assert (major, minor) >= (0, 40), (
                f"anthropic {version} predates the httpx 0.28 'proxies' kwarg fix"
            )
            return
    assert False, "anthropic pin not found in requirements.txt"


def _fake_response(status_code):
    import httpx
    return httpx.Response(status_code, request=httpx.Request("GET", "https://api.anthropic.com/v1/models"))

"""Direct unit tests for app/ai_provider.py -- the provider-agnostic dispatcher every AI call
site in summarizer.py and release_notes.py goes through instead of instantiating a provider
SDK client directly. Mocks each provider's own SDK client shape (anthropic.Anthropic /
google.genai.Client) since this is the one module that's actually allowed to know what those
shapes look like."""

from unittest.mock import MagicMock, patch

import pytest

from app import ai_provider, db

db.init_db()


@pytest.fixture(autouse=True)
def clean_settings():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_gemini_api_key("")
    yield
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")
    db.set_gemini_api_key("")


def _anthropic_response(text: str):
    resp = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp.content = [block]
    return resp


def _gemini_response(text: str):
    resp = MagicMock()
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------------

def test_is_configured_false_with_no_key_on_either_provider():
    assert ai_provider.is_configured() is False


def test_is_configured_true_once_the_active_providers_key_is_set():
    db.set_anthropic_api_key("sk-test")
    assert ai_provider.is_configured() is True


def test_is_configured_checks_the_active_provider_only():
    db.set_ai_provider("gemini")
    db.set_anthropic_api_key("sk-test")  # set, but Anthropic isn't the active provider
    assert ai_provider.is_configured() is False

    db.set_gemini_api_key("AIza-test")
    assert ai_provider.is_configured() is True


# ---------------------------------------------------------------------------
# complete_text() -- Anthropic
# ---------------------------------------------------------------------------

def test_complete_text_anthropic_sends_system_and_model_and_returns_joined_text():
    db.set_anthropic_api_key("sk-test")
    db.set_anthropic_model("claude-sonnet-5")

    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _anthropic_response("hello world")
        result = ai_provider.complete_text(system="be terse", user_message="hi", max_tokens=100)

    assert result == "hello world"
    mock_client_cls.assert_called_once_with(api_key="sk-test")
    kwargs = mock_client_cls.return_value.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["system"] == "be terse"
    assert kwargs["max_tokens"] == 100
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_complete_text_anthropic_omits_system_kwarg_when_none():
    db.set_anthropic_api_key("sk-test")
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _anthropic_response("ok")
        ai_provider.complete_text(system=None, user_message="hi", max_tokens=30)

    kwargs = mock_client_cls.return_value.messages.create.call_args.kwargs
    assert "system" not in kwargs


# ---------------------------------------------------------------------------
# complete_text() -- Gemini
# ---------------------------------------------------------------------------

def test_complete_text_gemini_sends_model_and_returns_response_text():
    db.set_ai_provider("gemini")
    db.set_gemini_api_key("AIza-test")
    db.set_gemini_model("gemini-2.5-flash")

    with patch("app.ai_provider.genai.Client") as mock_client_cls:
        mock_client_cls.return_value.models.generate_content.return_value = _gemini_response("hello from gemini")
        result = ai_provider.complete_text(system="be terse", user_message="hi", max_tokens=100)

    assert result == "hello from gemini"
    mock_client_cls.assert_called_once_with(api_key="AIza-test")
    kwargs = mock_client_cls.return_value.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-flash"
    assert kwargs["contents"] == "hi"


def test_complete_text_gemini_handles_a_none_response_text():
    db.set_ai_provider("gemini")
    db.set_gemini_api_key("AIza-test")
    with patch("app.ai_provider.genai.Client") as mock_client_cls:
        mock_client_cls.return_value.models.generate_content.return_value = _gemini_response(None)
        result = ai_provider.complete_text(system=None, user_message="hi", max_tokens=30)

    assert result == ""


# ---------------------------------------------------------------------------
# web_search()
# ---------------------------------------------------------------------------

def test_web_search_anthropic_enables_the_web_search_tool():
    db.set_anthropic_api_key("sk-test")
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _anthropic_response('{"found": true}')
        result = ai_provider.web_search("find the notes", max_tokens=1200)

    assert result == '{"found": true}'
    kwargs = mock_client_cls.return_value.messages.create.call_args.kwargs
    assert kwargs["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]


def test_web_search_anthropic_joins_multiple_text_blocks_with_newlines():
    db.set_anthropic_api_key("sk-test")
    resp = MagicMock()
    block1, block2 = MagicMock(), MagicMock()
    block1.type = "text"
    block1.text = "searching..."
    block2.type = "text"
    block2.text = '{"found": true}'
    resp.content = [block1, block2]

    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = resp
        result = ai_provider.web_search("find the notes", max_tokens=1200)

    assert result == 'searching...\n{"found": true}'


def test_web_search_gemini_enables_google_search_grounding():
    db.set_ai_provider("gemini")
    db.set_gemini_api_key("AIza-test")
    with patch("app.ai_provider.genai.Client") as mock_client_cls:
        mock_client_cls.return_value.models.generate_content.return_value = _gemini_response('{"found": true}')
        result = ai_provider.web_search("find the notes", max_tokens=1200)

    assert result == '{"found": true}'
    kwargs = mock_client_cls.return_value.models.generate_content.call_args.kwargs
    tools = kwargs["config"].tools
    assert len(tools) == 1 and tools[0].google_search is not None

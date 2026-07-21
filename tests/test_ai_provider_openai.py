"""Unit tests for ai_provider.py's OpenAI and OpenAI-compatible branches -- the two providers
added on top of Anthropic/Gemini. The compatible branch is one implementation covering every
server that speaks OpenAI's chat-completions dialect (Ollama, LM Studio, llama.cpp, vLLM,
OpenRouter), so these tests pin down the dialect differences that actually matter: which
max-tokens parameter each branch sends, that reasoning effort is only sent to api.openai.com,
and that the compatible branch never pretends to have a web search tool."""

from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from app import ai_provider, db

db.init_db()


def _reset():
    db.set_ai_provider("anthropic")
    db.set_openai_api_key("")
    db.set_openai_model("gpt-5.1")
    db.set_openai_concurrency(db.AI_CONCURRENCY_DEFAULT)
    db.set_openai_compat_base_url("")
    db.set_openai_compat_model("")
    db.set_openai_compat_api_key("")
    db.set_openai_compat_concurrency(1)


@pytest.fixture(autouse=True)
def clean_settings():
    _reset()
    yield
    _reset()


def _chat_response(text: str, finish_reason: str = "stop"):
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    choice.finish_reason = finish_reason
    resp.choices = [choice]
    return resp


def _rate_limit_error():
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.RateLimitError(
        "rate limited", response=httpx.Response(429, request=request), body=None
    )


def _server_error():
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.InternalServerError(
        "overloaded", response=httpx.Response(500, request=request), body=None
    )


# ---------------------------------------------------------------------------
# is_configured() / concurrency_limit()
# ---------------------------------------------------------------------------

def test_is_configured_openai_needs_a_key():
    db.set_ai_provider("openai")
    assert ai_provider.is_configured() is False
    db.set_openai_api_key("sk-test")
    assert ai_provider.is_configured() is True


def test_is_configured_openai_compat_needs_url_and_model_but_no_key():
    db.set_ai_provider("openai_compat")
    assert ai_provider.is_configured() is False
    db.set_openai_compat_base_url("http://ollama:11434/v1")
    assert ai_provider.is_configured() is False
    db.set_openai_compat_model("llama3.1:8b")
    assert ai_provider.is_configured() is True


def test_concurrency_limit_dispatches_to_each_openai_provider():
    db.set_ai_provider("openai")
    db.set_openai_concurrency(6)
    assert ai_provider.concurrency_limit() == 6

    db.set_ai_provider("openai_compat")
    db.set_openai_compat_concurrency(2)
    assert ai_provider.concurrency_limit() == 2


def test_openai_compat_concurrency_defaults_to_one_for_local_hardware():
    assert db.get_openai_compat_concurrency() == 1


# ---------------------------------------------------------------------------
# complete_text() -- OpenAI
# ---------------------------------------------------------------------------

def test_complete_text_openai_sends_model_system_and_completion_token_budget():
    db.set_ai_provider("openai")
    db.set_openai_api_key("sk-test")
    db.set_openai_model("gpt-5.1")

    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.return_value = _chat_response("hello")
        result = ai_provider.complete_text(system="be terse", user_message="hi", max_tokens=100)

    assert result == "hello"
    assert mock_client_cls.call_args.kwargs["api_key"] == "sk-test"
    assert mock_client_cls.call_args.kwargs["max_retries"] == 0
    kwargs = mock_client_cls.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-5.1"
    # The GPT-5 family rejects the legacy max_tokens parameter.
    assert kwargs["max_completion_tokens"] == 100
    assert "max_tokens" not in kwargs
    assert kwargs["reasoning_effort"] == ai_provider._OPENAI_REASONING_EFFORT
    assert kwargs["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]


def test_complete_text_openai_omits_the_system_message_when_none():
    db.set_ai_provider("openai")
    db.set_openai_api_key("sk-test")
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.return_value = _chat_response("ok")
        ai_provider.complete_text(system=None, user_message="hi", max_tokens=30)

    kwargs = mock_client_cls.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_complete_text_openai_truncated_response_retries_with_double_the_budget():
    db.set_ai_provider("openai")
    db.set_openai_api_key("sk-test")
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.side_effect = [
            _chat_response("cut off mid-se", finish_reason="length"),
            _chat_response("the full response"),
        ]
        result = ai_provider.complete_text(system=None, user_message="hi", max_tokens=100)

    assert result == "the full response"
    calls = mock_client_cls.return_value.chat.completions.create.call_args_list
    assert [c.kwargs["max_completion_tokens"] for c in calls] == [100, 200]


def test_complete_text_openai_handles_a_none_message_content():
    db.set_ai_provider("openai")
    db.set_openai_api_key("sk-test")
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.return_value = _chat_response(None)
        assert ai_provider.complete_text(system=None, user_message="hi", max_tokens=30) == ""


# ---------------------------------------------------------------------------
# complete_text() -- OpenAI-compatible
# ---------------------------------------------------------------------------

def test_complete_text_compat_uses_the_base_url_and_legacy_max_tokens():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_base_url("http://ollama:11434/v1")
    db.set_openai_compat_model("llama3.1:8b")

    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.return_value = _chat_response("hello")
        result = ai_provider.complete_text(system="be terse", user_message="hi", max_tokens=100)

    assert result == "hello"
    assert mock_client_cls.call_args.kwargs["base_url"] == "http://ollama:11434/v1"
    # No key configured -- a placeholder is sent (the SDK refuses an empty key; local servers
    # ignore whatever is sent).
    assert mock_client_cls.call_args.kwargs["api_key"] == "unused"
    kwargs = mock_client_cls.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "llama3.1:8b"
    # The one parameter every OpenAI-compatible server understands.
    assert kwargs["max_tokens"] == 100
    assert "max_completion_tokens" not in kwargs
    # An arbitrary server may reject OpenAI-only parameters outright.
    assert "reasoning_effort" not in kwargs


def test_complete_text_compat_sends_a_configured_key():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_base_url("https://openrouter.ai/api/v1")
    db.set_openai_compat_model("meta-llama/llama-3.1-8b-instruct")
    db.set_openai_compat_api_key("sk-or-abc")

    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.return_value = _chat_response("ok")
        ai_provider.complete_text(system=None, user_message="hi", max_tokens=30)

    assert mock_client_cls.call_args.kwargs["api_key"] == "sk-or-abc"


# ---------------------------------------------------------------------------
# _call_openai() retry policy
# ---------------------------------------------------------------------------

def test_call_openai_retries_a_rate_limit_and_counts_it():
    ai_provider.reset_rate_limited_count()
    calls = [_rate_limit_error(), "ok"]

    def fn():
        result = calls.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("app.ai_provider.time.sleep") as mock_sleep:
        assert ai_provider._call_openai(fn) == "ok"

    mock_sleep.assert_called_once()
    assert ai_provider.rate_limited_count() == 1


def test_call_openai_retries_a_transient_server_error_without_counting_it():
    ai_provider.reset_rate_limited_count()
    calls = [_server_error(), "ok"]

    def fn():
        result = calls.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("app.ai_provider.time.sleep"):
        assert ai_provider._call_openai(fn) == "ok"

    assert ai_provider.rate_limited_count() == 0


def test_call_openai_gives_up_after_max_attempts():
    call_count = 0

    def fn():
        nonlocal call_count
        call_count += 1
        raise _rate_limit_error()

    with patch("app.ai_provider.time.sleep"):
        with pytest.raises(openai.RateLimitError):
            ai_provider._call_openai(fn)

    assert call_count == ai_provider._OPENAI_MAX_ATTEMPTS


def test_call_openai_never_retries_other_errors():
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    error = openai.AuthenticationError(
        "bad key", response=httpx.Response(401, request=request), body=None
    )
    call_count = 0

    def fn():
        nonlocal call_count
        call_count += 1
        raise error

    with patch("app.ai_provider.time.sleep") as mock_sleep:
        with pytest.raises(openai.AuthenticationError):
            ai_provider._call_openai(fn)

    assert call_count == 1
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# web_search()
# ---------------------------------------------------------------------------

def test_web_search_openai_uses_the_responses_api_with_the_search_tool():
    db.set_ai_provider("openai")
    db.set_openai_api_key("sk-test")
    resp = MagicMock()
    resp.output_text = '{"found": true}'
    resp.status = "completed"

    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.responses.create.return_value = resp
        result = ai_provider.web_search("find the notes", max_tokens=1200)

    assert result == '{"found": true}'
    kwargs = mock_client_cls.return_value.responses.create.call_args.kwargs
    assert kwargs["tools"] == [{"type": "web_search"}]
    assert kwargs["max_output_tokens"] == 1200


def test_web_search_openai_retries_when_cut_off_by_the_output_token_cap():
    db.set_ai_provider("openai")
    db.set_openai_api_key("sk-test")
    cut_off = MagicMock()
    cut_off.output_text = '{"incompl'
    cut_off.status = "incomplete"
    cut_off.incomplete_details.reason = "max_output_tokens"
    complete = MagicMock()
    complete.output_text = '{"found": true}'
    complete.status = "completed"

    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.responses.create.side_effect = [cut_off, complete]
        result = ai_provider.web_search("find the notes", max_tokens=1200)

    assert result == '{"found": true}'
    calls = mock_client_cls.return_value.responses.create.call_args_list
    assert [c.kwargs["max_output_tokens"] for c in calls] == [1200, 2400]


def test_web_search_compat_raises_rather_than_hallucinating():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_base_url("http://ollama:11434/v1")
    db.set_openai_compat_model("llama3.1:8b")
    with pytest.raises(RuntimeError, match="web search"):
        ai_provider.web_search("find the notes", max_tokens=1200)


# ---------------------------------------------------------------------------
# Key/endpoint tests
# ---------------------------------------------------------------------------

def test_test_openai_key_accepts_a_working_key():
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        ok, message = ai_provider.test_openai_key("sk-good")
    assert ok is True
    mock_client_cls.return_value.models.list.assert_called_once()


def test_test_openai_key_rejects_an_invalid_key():
    request = httpx.Request("GET", "https://api.openai.com/v1/models")
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.models.list.side_effect = openai.AuthenticationError(
            "bad", response=httpx.Response(401, request=request), body=None
        )
        ok, message = ai_provider.test_openai_key("sk-bad")
    assert ok is False
    assert message == "Invalid API key."


def test_test_openai_compat_rejects_a_non_http_url_without_a_network_call():
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        ok, message = ai_provider.test_openai_compat("ollama:11434", "")
    assert ok is False
    assert "http://" in message
    mock_client_cls.assert_not_called()


def test_test_openai_compat_reports_an_unreachable_endpoint():
    request = httpx.Request("GET", "http://nowhere:1/v1/models")
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.models.list.side_effect = openai.APIConnectionError(request=request)
        ok, message = ai_provider.test_openai_compat("http://nowhere:1/v1", "")
    assert ok is False
    assert "reach" in message


def test_test_openai_compat_accepts_a_working_endpoint():
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        ok, message = ai_provider.test_openai_compat("http://ollama:11434/v1", "")
    assert ok is True
    assert message == "Endpoint works."
    mock_client_cls.return_value.models.list.assert_called_once()


# ---------------------------------------------------------------------------
# Secrets at rest
# ---------------------------------------------------------------------------

def test_both_openai_keys_are_stored_encrypted():
    from app import secrets_crypto

    db.set_openai_api_key("sk-secret-one")
    db.set_openai_compat_api_key("sk-secret-two")

    with db.get_conn() as conn:
        rows = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM app_settings WHERE key IN ('openai_api_key', 'openai_compat_api_key')"
            ).fetchall()
        }
    assert rows["openai_api_key"] != "sk-secret-one"
    assert rows["openai_compat_api_key"] != "sk-secret-two"
    assert secrets_crypto.decrypt(rows["openai_api_key"]) == "sk-secret-one"
    assert secrets_crypto.decrypt(rows["openai_compat_api_key"]) == "sk-secret-two"


# ---------------------------------------------------------------------------
# complete_chat() -- OpenAI and OpenAI-compatible
# ---------------------------------------------------------------------------

_CHAT_HISTORY = [
    {"role": "user", "content": "why is romm-db unhealthy?"},
    {"role": "assistant", "content": "It's logging connection timeouts."},
    {"role": "user", "content": "since when?"},
]


def test_complete_chat_openai_prepends_system_and_uses_completion_token_budget():
    db.set_ai_provider("openai")
    db.set_openai_api_key("sk-test")
    db.set_openai_model("gpt-5.1")

    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.return_value = _chat_response("a while")
        result = ai_provider.complete_chat(system="be helpful", messages=_CHAT_HISTORY, max_tokens=500)

    assert result == "a while"
    kwargs = mock_client_cls.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-5.1"
    assert kwargs["max_completion_tokens"] == 500
    assert "max_tokens" not in kwargs
    assert kwargs["reasoning_effort"] == ai_provider._OPENAI_REASONING_EFFORT
    # System is prepended, then the conversation follows verbatim (roles already match).
    assert kwargs["messages"] == [{"role": "system", "content": "be helpful"}, *_CHAT_HISTORY]


def test_complete_chat_compat_uses_legacy_max_tokens_and_no_openai_only_params():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_base_url("http://ollama:11434/v1")
    db.set_openai_compat_model("llama3.1:8b")

    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.return_value = _chat_response("since Tuesday")
        result = ai_provider.complete_chat(system=None, messages=_CHAT_HISTORY, max_tokens=400)

    assert result == "since Tuesday"
    assert mock_client_cls.call_args.kwargs["base_url"] == "http://ollama:11434/v1"
    kwargs = mock_client_cls.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "llama3.1:8b"
    assert kwargs["max_tokens"] == 400
    assert "max_completion_tokens" not in kwargs
    assert "reasoning_effort" not in kwargs
    # No system given -- the array is just the conversation turns.
    assert kwargs["messages"] == _CHAT_HISTORY


def test_complete_chat_openai_truncated_reply_retries_with_double_the_budget():
    db.set_ai_provider("openai")
    db.set_openai_api_key("sk-test")
    with patch("app.ai_provider.openai.OpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create.side_effect = [
            _chat_response("half an ans", finish_reason="length"),
            _chat_response("the whole answer"),
        ]
        result = ai_provider.complete_chat(system="s", messages=_CHAT_HISTORY, max_tokens=100)

    assert result == "the whole answer"
    calls = mock_client_cls.return_value.chat.completions.create.call_args_list
    assert [c.kwargs["max_completion_tokens"] for c in calls] == [100, 200]

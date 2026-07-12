"""Direct unit tests for app/ai_provider.py -- the provider-agnostic dispatcher every AI call
site in summarizer.py and release_notes.py goes through instead of instantiating a provider
SDK client directly. Mocks each provider's own SDK client shape (anthropic.Anthropic /
google.genai.Client) since this is the one module that's actually allowed to know what those
shapes look like."""

from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors as genai_errors

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
    assert mock_client_cls.call_args.kwargs["api_key"] == "AIza-test"
    kwargs = mock_client_cls.return_value.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-flash"
    assert kwargs["contents"] == "hi"


def test_gemini_client_disables_the_sdks_own_retry():
    """The SDK's built-in retry can only key off HTTP status code, not the quota metadata in
    the response body that distinguishes a worth-retrying per-minute limit from a pointless-
    to-retry exhausted daily quota -- see _call_gemini(). Proves that distinction is handled
    entirely by our own wrapper by confirming the SDK's own retry is turned off."""
    db.set_ai_provider("gemini")
    db.set_gemini_api_key("AIza-test")
    with patch("app.ai_provider.genai.Client") as mock_client_cls:
        mock_client_cls.return_value.models.generate_content.return_value = _gemini_response("ok")
        ai_provider.complete_text(system=None, user_message="hi", max_tokens=30)

    http_options = mock_client_cls.call_args.kwargs["http_options"]
    assert http_options.retry_options.attempts == 1


def _per_minute_429(retry_delay="2s"):
    return genai_errors.ClientError(429, {
        "error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                    {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"},
                ]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
            ],
        },
    })


def _per_day_429():
    return genai_errors.ClientError(429, {
        "error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                    {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"},
                ]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "16s"},
            ],
        },
    })


def test_call_gemini_retries_a_transient_per_minute_rate_limit_and_succeeds():
    calls = [_per_minute_429("0.01s"), "ok"]

    def fn():
        result = calls.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("app.ai_provider.time.sleep") as mock_sleep:
        result = ai_provider._call_gemini(fn)

    assert result == "ok"
    mock_sleep.assert_called_once()


def test_call_gemini_fails_immediately_on_an_exhausted_daily_quota():
    """No amount of waiting inside this same run fixes an exhausted daily quota -- must not
    burn through the retry budget (or any real time) on something that can't succeed today."""
    call_count = 0

    def fn():
        nonlocal call_count
        call_count += 1
        raise _per_day_429()

    with patch("app.ai_provider.time.sleep") as mock_sleep:
        with pytest.raises(genai_errors.ClientError):
            ai_provider._call_gemini(fn)

    assert call_count == 1
    mock_sleep.assert_not_called()


def test_call_gemini_gives_up_after_max_attempts_for_persistent_per_minute_limiting():
    call_count = 0

    def fn():
        nonlocal call_count
        call_count += 1
        raise _per_minute_429("0.01s")

    with patch("app.ai_provider.time.sleep"):
        with pytest.raises(genai_errors.ClientError):
            ai_provider._call_gemini(fn)

    assert call_count == ai_provider._GEMINI_MAX_ATTEMPTS


def test_call_gemini_never_retries_a_non_rate_limit_error():
    def fn():
        raise genai_errors.ClientError(500, {"error": {"code": 500, "status": "INTERNAL"}})

    with patch("app.ai_provider.time.sleep") as mock_sleep:
        with pytest.raises(genai_errors.ClientError):
            ai_provider._call_gemini(fn)

    mock_sleep.assert_not_called()


def test_call_gemini_retries_a_transient_server_overload_and_succeeds():
    """A 503 ('experiencing high demand') is Google's infrastructure, not quota -- worth a
    short retry rather than failing outright and waiting for the next check's auto-retry."""
    calls = [genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}), "ok"]

    def fn():
        result = calls.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("app.ai_provider.time.sleep") as mock_sleep:
        result = ai_provider._call_gemini(fn)

    assert result == "ok"
    mock_sleep.assert_called_once_with(ai_provider._GEMINI_SERVER_ERROR_DELAY)


def test_call_gemini_gives_up_after_max_attempts_for_persistent_server_overload():
    call_count = 0

    def fn():
        nonlocal call_count
        call_count += 1
        raise genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})

    with patch("app.ai_provider.time.sleep"):
        with pytest.raises(genai_errors.ServerError):
            ai_provider._call_gemini(fn)

    assert call_count == ai_provider._GEMINI_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# concurrency_limit()
# ---------------------------------------------------------------------------

def test_concurrency_limit_uses_the_configured_value_for_gemini():
    """No longer forced to 1 -- that was specifically a free-tier accommodation; the retry
    logic in _call_gemini() handles occasional rate-limiting under concurrency gracefully."""
    db.set_ai_provider("gemini")
    with patch("app.ai_provider.settings.ai_summarize_concurrency", 7):
        assert ai_provider.concurrency_limit() == 7


def test_concurrency_limit_uses_the_configured_value_for_anthropic():
    db.set_ai_provider("anthropic")
    with patch("app.ai_provider.settings.ai_summarize_concurrency", 7):
        assert ai_provider.concurrency_limit() == 7


def test_complete_text_gemini_handles_a_none_response_text():
    db.set_ai_provider("gemini")
    db.set_gemini_api_key("AIza-test")
    with patch("app.ai_provider.genai.Client") as mock_client_cls:
        mock_client_cls.return_value.models.generate_content.return_value = _gemini_response(None)
        result = ai_provider.complete_text(system=None, user_message="hi", max_tokens=30)

    assert result == ""


# ---------------------------------------------------------------------------
# Truncation retry -- a response that hits max_tokens exactly is cut off mid-sentence, so it's
# automatically retried with an escalating budget (doubling each round, up to
# _MAX_TRUNCATION_RETRIES times) rather than silently returned truncated after a single retry --
# a genuinely verbose real response (many features + several breaking changes + a detailed
# checklist) can still blow past 2x the original budget.
# ---------------------------------------------------------------------------

def test_anthropic_truncated_response_keeps_retrying_across_multiple_rounds():
    """A single retry wasn't enough for a real, unusually verbose changelog -- this proves the
    budget keeps escalating (100 -> 200 -> 400) across more than one retry until it succeeds."""
    db.set_anthropic_api_key("sk-test")
    cut_off_1 = _anthropic_response("cut off very early")
    cut_off_1.stop_reason = "max_tokens"
    cut_off_2 = _anthropic_response("cut off a bit later this time")
    cut_off_2.stop_reason = "max_tokens"
    complete = _anthropic_response("finally, the full response")
    complete.stop_reason = "end_turn"

    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.side_effect = [cut_off_1, cut_off_2, complete]
        result = ai_provider.complete_text(system=None, user_message="hi", max_tokens=100)

    assert result == "finally, the full response"
    calls = mock_client_cls.return_value.messages.create.call_args_list
    assert len(calls) == 3
    assert [c.kwargs["max_tokens"] for c in calls] == [100, 200, 400]


def test_anthropic_gives_up_at_the_hard_ceiling_and_returns_the_last_attempt():
    """A pathological case that never stops truncating must not retry forever or bill an
    unbounded amount -- capped at _MAX_TOKENS_CEILING, returning whatever the last attempt got
    rather than raising, since a truncated response is still more useful than none at all."""
    db.set_anthropic_api_key("sk-test")

    def _always_truncated(*args, **kwargs):
        resp = _anthropic_response(f"still cut off at {kwargs['max_tokens']}")
        resp.stop_reason = "max_tokens"
        return resp

    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.side_effect = _always_truncated
        result = ai_provider.complete_text(system=None, user_message="hi", max_tokens=5000)

    calls = mock_client_cls.return_value.messages.create.call_args_list
    # 5000 -> 8192 (capped) -- one attempt at the ceiling, then it stops rather than retrying
    # forever at a budget that can't get any larger.
    budgets = [c.kwargs["max_tokens"] for c in calls]
    assert budgets == [5000, ai_provider._MAX_TOKENS_CEILING]
    assert result == f"still cut off at {ai_provider._MAX_TOKENS_CEILING}"


def test_anthropic_truncated_response_retries_once_with_double_the_budget():
    db.set_anthropic_api_key("sk-test")
    cut_off = _anthropic_response("this got cut off mid-se")
    cut_off.stop_reason = "max_tokens"
    complete = _anthropic_response("this is the full response")
    complete.stop_reason = "end_turn"

    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.side_effect = [cut_off, complete]
        result = ai_provider.complete_text(system=None, user_message="hi", max_tokens=100)

    assert result == "this is the full response"
    calls = mock_client_cls.return_value.messages.create.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["max_tokens"] == 100
    assert calls[1].kwargs["max_tokens"] == 200


def test_anthropic_response_that_finishes_naturally_is_not_retried():
    db.set_anthropic_api_key("sk-test")
    resp = _anthropic_response("a complete answer")
    resp.stop_reason = "end_turn"

    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = resp
        result = ai_provider.complete_text(system=None, user_message="hi", max_tokens=100)

    assert result == "a complete answer"
    mock_client_cls.return_value.messages.create.assert_called_once()


def test_gemini_truncated_response_retries_once_with_double_the_budget():
    from google.genai import types as genai_types

    db.set_ai_provider("gemini")
    db.set_gemini_api_key("AIza-test")

    cut_off = _gemini_response("cut off mid-se")
    cut_off_candidate = MagicMock()
    cut_off_candidate.finish_reason = genai_types.FinishReason.MAX_TOKENS
    cut_off.candidates = [cut_off_candidate]

    complete = _gemini_response("the full response")
    complete_candidate = MagicMock()
    complete_candidate.finish_reason = genai_types.FinishReason.STOP
    complete.candidates = [complete_candidate]

    with patch("app.ai_provider.genai.Client") as mock_client_cls:
        mock_client_cls.return_value.models.generate_content.side_effect = [cut_off, complete]
        result = ai_provider.complete_text(system=None, user_message="hi", max_tokens=100)

    assert result == "the full response"
    calls = mock_client_cls.return_value.models.generate_content.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["config"].max_output_tokens == 100
    assert calls[1].kwargs["config"].max_output_tokens == 200


def test_web_search_truncation_also_retries():
    db.set_anthropic_api_key("sk-test")
    cut_off = _anthropic_response('{"incompl')
    cut_off.stop_reason = "max_tokens"
    complete = _anthropic_response('{"found": true}')
    complete.stop_reason = "end_turn"

    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.side_effect = [cut_off, complete]
        result = ai_provider.web_search("find the notes", max_tokens=1200)

    assert result == '{"found": true}'
    assert mock_client_cls.return_value.messages.create.call_count == 2


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

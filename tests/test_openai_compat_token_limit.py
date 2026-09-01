"""OpenAI-compatible token/context limit handling (Settings -> OpenAI-compatible): a real-world
report that checking updates on a local model threw a hard "AI provider error" whenever a
truncation retry kept re-asking for more output tokens than the model's own (often much smaller)
context window had left. Covers three pieces:

1. An operator-configurable ceiling (db.get_openai_compat_max_tokens, "" = no limit) that caps
   both the initial request and how high a truncation retry is allowed to grow, only ever for the
   openai_compat provider (ai_provider._effective_max_tokens_ceiling).
2. A clearer topbar message when that limit is hit anyway (ai_provider._classify_ai_error).
3. An opt-in auto-retry-with-a-smaller-request recovery (ai_provider._with_context_limit_shrink,
   db.get_openai_compat_auto_shrink_enabled) -- off by default, and only ever for openai_compat
   regardless of the setting, since retrying costs nothing against a local model but real money
   against a paid one."""

from unittest.mock import patch

import pytest

from app import ai_provider, db

db.init_db()


def _reset():
    db.set_ai_provider("anthropic")
    db.set_openai_compat_max_tokens(None)
    db.set_openai_compat_auto_shrink_enabled(False)


@pytest.fixture(autouse=True)
def clean_settings():
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# db layer
# ---------------------------------------------------------------------------

def test_max_tokens_defaults_to_no_limit():
    assert db.get_openai_compat_max_tokens() is None


def test_set_and_get_max_tokens():
    db.set_openai_compat_max_tokens(2000)
    assert db.get_openai_compat_max_tokens() == 2000


def test_clearing_max_tokens_back_to_none():
    db.set_openai_compat_max_tokens(2000)
    db.set_openai_compat_max_tokens(None)
    assert db.get_openai_compat_max_tokens() is None


def test_auto_shrink_defaults_to_disabled():
    assert db.get_openai_compat_auto_shrink_enabled() is False


def test_set_and_get_auto_shrink():
    db.set_openai_compat_auto_shrink_enabled(True)
    assert db.get_openai_compat_auto_shrink_enabled() is True


# ---------------------------------------------------------------------------
# _effective_max_tokens_ceiling
# ---------------------------------------------------------------------------

def test_ceiling_is_the_builtin_default_for_hosted_providers_regardless_of_the_setting():
    db.set_openai_compat_max_tokens(500)  # only ever meant for openai_compat
    for provider in ("anthropic", "gemini", "openai"):
        db.set_ai_provider(provider)
        assert ai_provider._effective_max_tokens_ceiling() == ai_provider._MAX_TOKENS_CEILING


def test_ceiling_is_the_builtin_default_for_compat_when_unset():
    db.set_ai_provider("openai_compat")
    assert ai_provider._effective_max_tokens_ceiling() == ai_provider._MAX_TOKENS_CEILING


def test_ceiling_uses_the_configured_value_for_compat_when_set():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_max_tokens(1500)
    assert ai_provider._effective_max_tokens_ceiling() == 1500


def test_ceiling_never_exceeds_the_builtin_default_even_if_configured_higher():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_max_tokens(999999)
    assert ai_provider._effective_max_tokens_ceiling() == ai_provider._MAX_TOKENS_CEILING


# ---------------------------------------------------------------------------
# _is_context_length_error / _classify_ai_error
# ---------------------------------------------------------------------------

def test_is_context_length_error_matches_common_phrasings():
    assert ai_provider._is_context_length_error(RuntimeError("This model's maximum context length is 8192 tokens"))
    assert ai_provider._is_context_length_error(RuntimeError("context_length_exceeded: too many tokens"))
    assert not ai_provider._is_context_length_error(RuntimeError("connection refused"))


def test_classify_context_error_mentions_the_setting_for_openai_compat():
    db.set_ai_provider("openai_compat")
    message, fatal = ai_provider._classify_ai_error(RuntimeError("maximum context length exceeded"))
    assert "Max Response Tokens" in message
    assert fatal is False


def test_classify_context_error_is_generic_for_other_providers():
    db.set_ai_provider("anthropic")
    message, fatal = ai_provider._classify_ai_error(RuntimeError("maximum context length exceeded"))
    assert "Max Response Tokens" not in message
    assert "context/token limit" in message
    assert fatal is False


# ---------------------------------------------------------------------------
# _with_context_limit_shrink
# ---------------------------------------------------------------------------

def test_shrink_disabled_by_default_raises_immediately():
    db.set_ai_provider("openai_compat")
    calls = []

    def run(budget):
        calls.append(budget)
        raise RuntimeError("maximum context length exceeded")

    with pytest.raises(RuntimeError):
        ai_provider._with_context_limit_shrink(run, 4000)
    assert calls == [4000]


def test_shrink_enabled_but_provider_not_compat_never_shrinks():
    db.set_ai_provider("anthropic")
    db.set_openai_compat_auto_shrink_enabled(True)
    calls = []

    def run(budget):
        calls.append(budget)
        raise RuntimeError("maximum context length exceeded")

    with pytest.raises(RuntimeError):
        ai_provider._with_context_limit_shrink(run, 4000)
    assert calls == [4000]


def test_shrink_enabled_retries_with_a_halved_budget_until_success():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_auto_shrink_enabled(True)
    calls = []

    def run(budget):
        calls.append(budget)
        if budget > 1000:
            raise RuntimeError("maximum context length exceeded")
        return "ok"

    result = ai_provider._with_context_limit_shrink(run, 4000)
    assert result == "ok"
    assert calls == [4000, 2000, 1000]


def test_shrink_gives_up_at_the_floor_and_raises_the_last_error():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_auto_shrink_enabled(True)
    calls = []

    def run(budget):
        calls.append(budget)
        raise RuntimeError("maximum context length exceeded")

    with pytest.raises(RuntimeError):
        ai_provider._with_context_limit_shrink(run, 4000)
    # Never shrinks below _CONTEXT_SHRINK_MIN_TOKENS, and never exceeds the attempt cap.
    assert all(b >= ai_provider._CONTEXT_SHRINK_MIN_TOKENS for b in calls)
    assert len(calls) <= ai_provider._CONTEXT_SHRINK_MAX_ATTEMPTS


def test_shrink_never_retries_a_different_kind_of_error():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_auto_shrink_enabled(True)
    calls = []

    def run(budget):
        calls.append(budget)
        raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        ai_provider._with_context_limit_shrink(run, 4000)
    assert calls == [4000]


# ---------------------------------------------------------------------------
# Wired into complete_text/complete_chat
# ---------------------------------------------------------------------------

def test_complete_text_clamps_the_initial_budget_to_the_configured_ceiling():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_base_url("http://ollama:11434/v1")
    db.set_openai_compat_model("llama3.1:8b")
    db.set_openai_compat_max_tokens(500)
    seen_budgets = []

    def fake_chat_send(client, model, messages, extra):
        seen_budgets.append(extra.get("max_tokens"))
        return "reply", False

    with patch("app.ai_provider._openai_compat_client"), \
         patch("app.ai_provider._openai_chat_send", side_effect=fake_chat_send):
        ai_provider.complete_text("system", "hello", max_tokens=2000)

    assert seen_budgets == [500]


def test_complete_text_auto_shrinks_on_a_real_context_error_when_enabled():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_base_url("http://ollama:11434/v1")
    db.set_openai_compat_model("llama3.1:8b")
    db.set_openai_compat_auto_shrink_enabled(True)
    seen_budgets = []

    def fake_chat_send(client, model, messages, extra):
        budget = extra.get("max_tokens")
        seen_budgets.append(budget)
        if budget > 500:
            raise RuntimeError("maximum context length exceeded")
        return "reply", False

    with patch("app.ai_provider._openai_compat_client"), \
         patch("app.ai_provider._openai_chat_send", side_effect=fake_chat_send):
        result = ai_provider.complete_text("system", "hello", max_tokens=2000)

    assert result == "reply"
    assert seen_budgets == [2000, 1000, 500]


def test_complete_chat_clamps_the_initial_budget_too():
    db.set_ai_provider("openai_compat")
    db.set_openai_compat_base_url("http://ollama:11434/v1")
    db.set_openai_compat_model("llama3.1:8b")
    db.set_openai_compat_max_tokens(700)
    seen_budgets = []

    def fake_chat_send(client, model, messages, extra):
        seen_budgets.append(extra.get("max_tokens"))
        return "reply", False

    with patch("app.ai_provider._openai_compat_client"), \
         patch("app.ai_provider._openai_chat_send", side_effect=fake_chat_send):
        ai_provider.complete_chat("system", [{"role": "user", "content": "hi"}], max_tokens=3000)

    assert seen_budgets == [700]


# ---------------------------------------------------------------------------
# Settings routes
# ---------------------------------------------------------------------------

def test_save_max_tokens_route_sets_a_value(client):
    resp = client.post("/settings/ai/openai-compat-max-tokens", data={"value": "1500"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "value": 1500}
    assert db.get_openai_compat_max_tokens() == 1500


def test_save_max_tokens_route_blank_means_no_limit(client):
    db.set_openai_compat_max_tokens(1500)
    resp = client.post("/settings/ai/openai-compat-max-tokens", data={"value": ""})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "value": ""}
    assert db.get_openai_compat_max_tokens() is None


def test_save_max_tokens_route_rejects_non_numeric(client):
    resp = client.post("/settings/ai/openai-compat-max-tokens", data={"value": "abc"})
    assert resp.json()["ok"] is False
    assert db.get_openai_compat_max_tokens() is None


def test_save_max_tokens_route_rejects_zero_and_negative(client):
    for bad in ("0", "-5"):
        resp = client.post("/settings/ai/openai-compat-max-tokens", data={"value": bad})
        assert resp.json()["ok"] is False
    assert db.get_openai_compat_max_tokens() is None


def test_save_auto_shrink_route_updates_the_setting(client):
    resp = client.post("/settings/ai/openai-compat-auto-shrink", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_openai_compat_auto_shrink_enabled() is True

    resp = client.post("/settings/ai/openai-compat-auto-shrink", data={})
    assert db.get_openai_compat_auto_shrink_enabled() is False


def test_settings_page_reflects_current_values(client):
    db.set_openai_compat_max_tokens(1234)
    db.set_openai_compat_auto_shrink_enabled(True)
    resp = client.get("/settings")
    assert 'value="1234"' in resp.text
    assert 'id="openai_compat_auto_shrink_enabled"' in resp.text
    assert "checked" in resp.text.split('id="openai_compat_auto_shrink_enabled"')[1][:120]


def test_settings_page_shows_blank_field_when_no_limit_set(client):
    db.set_openai_compat_max_tokens(None)
    resp = client.get("/settings")
    assert 'id="openai_compat_max_tokens_input"' in resp.text
    section = resp.text.split('id="openai_compat_max_tokens_input"')[1][:200]
    assert 'value=""' in section

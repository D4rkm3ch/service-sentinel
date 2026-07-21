"""Pluggable AI provider -- Anthropic, Gemini, OpenAI, or any OpenAI-compatible endpoint
(Ollama, LM Studio, llama.cpp, vLLM, OpenRouter, ...), chosen (along with each provider's own
API key and model) on the Settings page rather than baked in at deploy time via compose-file
env vars. Every AI call site in summarizer.py and release_notes.py's web search fallback goes
through complete_text()/web_search() here instead of instantiating a provider SDK client
directly, so switching providers in Settings takes effect for every feature immediately, with
no redeploy -- the whole point being able to switch away from a provider that's temporarily
out of credits without touching the compose file at all.

Deliberately just a dispatch over the known providers, not a plugin registry -- a new provider
means one more branch here, not a new abstraction. The OpenAI-compatible provider already
covers the long tail of local/aggregator servers in a single branch.
"""

import logging
import threading
import time

import anthropic
import openai
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app import db

# Every value the ai_provider setting can hold -- main.py validates the Settings dropdown's
# POST against this, and is_configured()/concurrency_limit()/complete_text()/web_search()
# below dispatch on it.
PROVIDERS = ("anthropic", "gemini", "openai", "openai_compat")

logger = logging.getLogger("service_sentinel.ai_provider")

# Gemini's free tier turned out to have two separate 429 causes, only one of which is worth
# retrying: a per-minute burst limit (a handful of concurrent calls trips it instantly, but it
# clears within a minute) and a per-day cap per model (as low as 20/day observed in practice)
# that a 429 retry can never wait out -- it only resets on Google's own daily cycle. The SDK's
# own built-in retry can only key off HTTP status code, not which of these two a given 429
# actually was, so it's disabled here (attempts=1) in favor of _call_gemini() below, which
# reads the structured quota violation in the response body to tell them apart: retry with the
# delay Google itself reports for the per-minute case, fail immediately for the per-day case
# rather than burning a couple of minutes of retries that were never going to succeed today.
_GEMINI_MAX_ATTEMPTS = 5
_GEMINI_NO_RETRY = genai_types.HttpRetryOptions(attempts=1)

# A count of every 429 _call_gemini() has hit during the current check, surfaced through each
# feature's result dict (see run_claimed_updates_check/run_log_check_for/run_compose_check_for)
# and from there into check_state.format_summary()'s status-badge text -- so hitting a rate
# limit is visible in the UI itself instead of only in the container logs, which is the reason
# an operator would ever think to lower a provider's concurrency setting in the first place.
# Reset at the start of each top-level check and read once at the end -- see this module's
# docstring note wherever reset_rate_limited_count()/rate_limited_count() are called for why a
# single shared counter (rather than one scoped per call) is good enough here: the app already
# treats "a check is running" as one sitewide lock (see base.html), so genuinely concurrent
# checks from two different features racing each other is an accepted, pre-existing edge case,
# not a new one this introduces.
_rate_limit_lock = threading.Lock()
_rate_limited_count = 0


def reset_rate_limited_count() -> None:
    global _rate_limited_count
    with _rate_limit_lock:
        _rate_limited_count = 0


def rate_limited_count() -> int:
    with _rate_limit_lock:
        return _rate_limited_count


def _note_rate_limited() -> None:
    global _rate_limited_count
    with _rate_limit_lock:
        _rate_limited_count += 1


def _gemini_client() -> "genai.Client":
    return genai.Client(
        api_key=db.get_gemini_api_key(),
        http_options=genai_types.HttpOptions(retry_options=_GEMINI_NO_RETRY),
    )


def _gemini_quota_info(exc: "genai_errors.ClientError") -> tuple[bool, float]:
    """Parses a 429's structured error detail list. Returns (is_daily_quota, retry_after) --
    is_daily_quota True means every quota violation named in the response is a per-day cap, so
    retrying within this same run is pointless; retry_after is the delay Google itself reports
    (falls back to a conservative 5s if the response didn't include one, e.g. a malformed or
    unexpected error shape)."""
    error_details = (exc.details or {}).get("error", {}).get("details", [])
    quota_ids: list[str] = []
    retry_after = 5.0
    for detail in error_details:
        type_name = detail.get("@type", "")
        if type_name.endswith("QuotaFailure"):
            quota_ids.extend(v.get("quotaId", "") for v in detail.get("violations", []))
        elif type_name.endswith("RetryInfo"):
            try:
                retry_after = float(detail.get("retryDelay", "").rstrip("s"))
            except ValueError:
                pass
    is_daily_quota = bool(quota_ids) and all("PerDay" in q for q in quota_ids)
    return is_daily_quota, retry_after


# A 503 ("This model is currently experiencing high demand") is Google's own infrastructure
# being momentarily overloaded -- nothing to do with quota or anything this app controls, and
# in practice clears within a few seconds. Worth a short, fixed retry rather than immediately
# giving up and waiting for the next check's auto-retry (see _needs_summary_retry in
# persist.py) to paper over it a day later.
_GEMINI_SERVER_ERROR_DELAY = 3.0


def _call_gemini(fn):
    """Runs a single Gemini API call through the daily-vs-per-minute-aware retry described
    above, plus a short retry for transient server-side overload (503). Any other error --
    including a 429 that isn't a rate limit at all, which shouldn't happen but the check is
    cheap -- is raised immediately. Retries of either kind are capped at _GEMINI_MAX_ATTEMPTS."""
    for attempt in range(_GEMINI_MAX_ATTEMPTS):
        try:
            return fn()
        except genai_errors.ServerError:
            if attempt == _GEMINI_MAX_ATTEMPTS - 1:
                raise
            logger.info(
                "Gemini returned a transient server error, retrying in %.1fs (attempt %d/%d)",
                _GEMINI_SERVER_ERROR_DELAY, attempt + 1, _GEMINI_MAX_ATTEMPTS,
            )
            time.sleep(_GEMINI_SERVER_ERROR_DELAY)
        except genai_errors.ClientError as exc:
            if exc.code != 429:
                raise
            _note_rate_limited()
            is_daily_quota, retry_after = _gemini_quota_info(exc)
            if is_daily_quota:
                logger.warning("Gemini free-tier daily quota exhausted -- not retrying until it resets")
                raise
            if attempt == _GEMINI_MAX_ATTEMPTS - 1:
                raise
            logger.info(
                "Gemini rate-limited, retrying in %.1fs (attempt %d/%d)",
                retry_after, attempt + 1, _GEMINI_MAX_ATTEMPTS,
            )
            time.sleep(min(retry_after, 30.0))


def _openai_client() -> "openai.OpenAI":
    """max_retries=0 disables the SDK's own silent retries so the RateLimitError handling in
    _call_openai() below sees every 429 and can count it toward the UI's rate-limit badge --
    the same reason the Gemini SDK's built-in retry is disabled above."""
    return openai.OpenAI(api_key=db.get_openai_api_key(), max_retries=0)


def _openai_compat_client(base_url: str | None = None, api_key: str | None = None) -> "openai.OpenAI":
    """The api_key falls back to a placeholder rather than empty: the SDK refuses to construct
    a client with no key at all (it would otherwise silently read OPENAI_API_KEY from the
    environment), but local servers like Ollama accept and ignore any value."""
    base = base_url if base_url is not None else db.get_openai_compat_base_url()
    key = api_key if api_key is not None else db.get_openai_compat_api_key()
    return openai.OpenAI(base_url=base, api_key=key or "unused", max_retries=0)


_OPENAI_MAX_ATTEMPTS = 5
_OPENAI_SERVER_ERROR_DELAY = 3.0
_OPENAI_RATE_LIMIT_DELAY = 5.0


def _call_openai(fn):
    """Retry policy for both OpenAI-dialect providers, same shape as _call_gemini(): count and
    retry rate limits with a short fixed delay, retry transient server-side errors, raise
    anything else immediately."""
    for attempt in range(_OPENAI_MAX_ATTEMPTS):
        try:
            return fn()
        except openai.RateLimitError:
            _note_rate_limited()
            if attempt == _OPENAI_MAX_ATTEMPTS - 1:
                raise
            logger.info(
                "OpenAI-compatible endpoint rate-limited, retrying in %.1fs (attempt %d/%d)",
                _OPENAI_RATE_LIMIT_DELAY, attempt + 1, _OPENAI_MAX_ATTEMPTS,
            )
            time.sleep(_OPENAI_RATE_LIMIT_DELAY)
        except openai.InternalServerError:
            if attempt == _OPENAI_MAX_ATTEMPTS - 1:
                raise
            logger.info(
                "OpenAI-compatible endpoint returned a transient server error, retrying in %.1fs (attempt %d/%d)",
                _OPENAI_SERVER_ERROR_DELAY, attempt + 1, _OPENAI_MAX_ATTEMPTS,
            )
            time.sleep(_OPENAI_SERVER_ERROR_DELAY)


def test_anthropic_key(key: str) -> tuple[bool, str]:
    """Validates a candidate Anthropic key with a free metadata call (lists models, no tokens
    billed) rather than a real completion -- cheap enough to run on every save attempt."""
    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
        return True, "API key works."
    except anthropic.AuthenticationError:
        return False, "Invalid API key."
    except anthropic.APIError as exc:
        return False, f"Couldn't verify key: {exc}"


def test_gemini_key(key: str) -> tuple[bool, str]:
    """Same idea as test_anthropic_key -- list() is a free metadata call. Wrapped in list() to
    force the (possibly lazily-paginated) result to actually be fetched, since the error only
    surfaces once the request is made."""
    try:
        list(genai.Client(api_key=key).models.list())
        return True, "API key works."
    except genai_errors.ClientError:
        return False, "Invalid API key."
    except genai_errors.APIError as exc:
        return False, f"Couldn't verify key: {exc}"


def test_openai_key(key: str) -> tuple[bool, str]:
    """Same idea as test_anthropic_key -- models.list() is a free metadata call."""
    try:
        openai.OpenAI(api_key=key).models.list()
        return True, "API key works."
    except openai.AuthenticationError:
        return False, "Invalid API key."
    except openai.OpenAIError as exc:
        return False, f"Couldn't verify key: {exc}"


def test_openai_compat(base_url: str, key: str) -> tuple[bool, str]:
    """Validates an OpenAI-compatible endpoint by listing its models -- every server this
    provider targets (Ollama, LM Studio, llama.cpp, vLLM, OpenRouter) implements /v1/models.
    Unlike the key tests above, failure here usually means the URL is wrong or the server is
    unreachable rather than a bad key, so the messages say so."""
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return False, "Base URL must start with http:// or https://"
    try:
        _openai_compat_client(base_url=base_url, api_key=key).models.list()
        return True, "Endpoint works."
    except openai.AuthenticationError:
        return False, "The endpoint rejected the API key."
    except openai.APIConnectionError:
        return False, "Couldn't reach the endpoint - check the URL and that the server is running."
    except openai.OpenAIError as exc:
        return False, f"Couldn't verify the endpoint: {exc}"


def is_configured() -> bool:
    """True if the currently-selected provider has what it needs to make a call. Every call
    site in summarizer.py/release_notes.py already early-outs on this before spending any
    effort building a prompt, exactly like they used to early-out on settings.anthropic_api_key.
    The OpenAI-compatible provider needs a URL and a model but no key -- local servers don't
    check one."""
    provider = db.get_ai_provider()
    if provider == "gemini":
        return bool(db.get_gemini_api_key())
    if provider == "openai":
        return bool(db.get_openai_api_key())
    if provider == "openai_compat":
        return bool(db.get_openai_compat_base_url()) and bool(db.get_openai_compat_model())
    return bool(db.get_anthropic_api_key())


def concurrency_limit() -> int:
    """How many AI calls persist.py's fan-out phases (release notes web search fallback,
    summarization) should run at once, for whichever provider is currently active. Used to be
    forced to 1 for Gemini regardless of tier -- the free tier's request/minute cap is tight
    enough that a handful of concurrent calls exhausts it almost immediately -- but a paid
    Gemini key has no such constraint (nor does Anthropic), and _call_gemini()'s own
    retry/backoff already handles an occasional 429 or 503 gracefully under concurrency, so
    there's no longer a blanket reason to serialize.

    Per-provider (not one global value) and UI-editable in Settings -- see db.get_anthropic_
    concurrency/get_gemini_concurrency -- since the right number genuinely differs by provider
    and by tier within a provider, not something one constant can fit."""
    provider = db.get_ai_provider()
    if provider == "gemini":
        return db.get_gemini_concurrency()
    if provider == "openai":
        return db.get_openai_concurrency()
    if provider == "openai_compat":
        return db.get_openai_compat_concurrency()
    return db.get_anthropic_concurrency()


# If a response hits its max_tokens ceiling exactly rather than finishing naturally, it's been
# cut off mid-sentence -- worse than the extra cost of retrying at a larger budget. A single
# doubled retry wasn't enough in practice for a genuinely verbose real changelog (a release with
# many features, several breaking changes, and a detailed upgrade checklist can still blow past
# 2x); this keeps doubling for a few more rounds instead of giving up after one, capped at
# _MAX_TOKENS_CEILING so a pathological case can't balloon into an unbounded bill. Every call
# site still picks max_tokens sized for the typical case -- this is what makes the occasional
# much-longer-than-typical response not truncate in practice, rather than each site
# over-budgeting "just in case" (which would still have a ceiling that could eventually be hit).
_TRUNCATION_RETRY_MULTIPLIER = 2
_MAX_TRUNCATION_RETRIES = 3
_MAX_TOKENS_CEILING = 8192

# Gemini's "thinking" models (2.5 Flash/Pro) spend an unpredictable number of internal
# reasoning tokens before writing the actual answer, and those thinking tokens count against
# max_output_tokens too -- so even a budget sized generously for the expected answer length can
# still get eaten alive by thinking and trip the truncation retry above. Real production traffic
# hit this repeatedly at several unrelated call sites (Logs' stack-naming and cross-service
# calls, then separately Updates' summarize_update/generate_upgrade_guidance calls) -- each
# fixed one at a time by raising that site's starting max_tokens, but the same failure kept
# resurfacing at the next site, since raising one budget does nothing for the others. Capping the
# thinking budget here, once, for every Gemini call, fixes the actual cause instead of chasing it
# site by site. 512 leaves real room for the model to reason about a genuinely ambiguous case
# without letting thinking alone consume an entire small output budget; 0 (fully disabled) isn't
# used here since Gemini 2.5 Pro rejects it outright (Flash models allow 0, Pro requires >0).
_GEMINI_THINKING_BUDGET = 512


_COMPLETE_FNS = {
    "anthropic": lambda system, user, mt: _complete_anthropic(system, user, mt),
    "gemini": lambda system, user, mt: _complete_gemini(system, user, mt),
    "openai": lambda system, user, mt: _complete_openai(system, user, mt),
    "openai_compat": lambda system, user, mt: _complete_openai_compat(system, user, mt),
}


def complete_text(system: str | None, user_message: str, max_tokens: int) -> str:
    """Single-turn text completion, provider-agnostic. Raises on failure -- same contract the
    direct anthropic.Anthropic() calls this replaces always had; every caller already handles
    an exception here as "this attempt failed," regardless of which provider raised it."""
    fn = _COMPLETE_FNS.get(db.get_ai_provider(), _COMPLETE_FNS["anthropic"])
    return _with_truncation_retry(lambda mt: fn(system, user_message, mt), max_tokens)


_WEB_SEARCH_FNS = {
    "anthropic": lambda user, mt: _web_search_anthropic(user, mt),
    "gemini": lambda user, mt: _web_search_gemini(user, mt),
    "openai": lambda user, mt: _web_search_openai(user, mt),
    "openai_compat": lambda user, mt: _web_search_openai_compat(user, mt),
}


def web_search(user_message: str, max_tokens: int) -> str:
    """Same shape as complete_text, but with the provider's own web-search/grounding tool
    enabled -- backs release_notes.py's web search fallback. Billed per search the model
    actually performs on top of the normal completion cost, which is why this is only ever
    reached behind its own opt-in Settings toggle -- gating that is the caller's job, not this
    function's. The OpenAI-compatible provider raises: a local model has no live search, and a
    plain completion here would just invite hallucinated release notes."""
    fn = _WEB_SEARCH_FNS.get(db.get_ai_provider(), _WEB_SEARCH_FNS["anthropic"])
    return _with_truncation_retry(lambda mt: fn(user_message, mt), max_tokens)


_CHAT_FNS = {
    "anthropic": lambda system, messages, mt: _chat_anthropic(system, messages, mt),
    "gemini": lambda system, messages, mt: _chat_gemini(system, messages, mt),
    "openai": lambda system, messages, mt: _chat_openai(system, messages, mt),
    "openai_compat": lambda system, messages, mt: _chat_openai_compat(system, messages, mt),
}


def complete_chat(system: str | None, messages: list[dict], max_tokens: int) -> str:
    """Multi-turn text completion, provider-agnostic -- the conversational sibling of
    complete_text (single user_message) that backs the in-app chat widget (see app/chat.py).
    messages is [{"role": "user"|"assistant", "content": "..."}, ...] in chronological order,
    ending on the newest user turn; system carries the chat's instructions plus the live
    read-only system-state snapshot chat.py builds fresh each turn. Same provider dispatch and
    same truncation-retry wrapper as complete_text; raises on failure identically, so the route
    that calls this handles a provider error the same way every other AI call site does."""
    fn = _CHAT_FNS.get(db.get_ai_provider(), _CHAT_FNS["anthropic"])
    return _with_truncation_retry(lambda mt: fn(system, messages, mt), max_tokens)


def _with_truncation_retry(fn, max_tokens: int) -> str:
    text = ""
    budget = max_tokens
    for attempt in range(_MAX_TRUNCATION_RETRIES + 1):
        text, truncated = fn(budget)
        if not truncated:
            return text
        if budget >= _MAX_TOKENS_CEILING:
            logger.warning(
                "Response still truncated at the %d-token ceiling after %d attempt(s) -- giving up",
                _MAX_TOKENS_CEILING, attempt + 1,
            )
            break
        next_budget = min(budget * _TRUNCATION_RETRY_MULTIPLIER, _MAX_TOKENS_CEILING)
        logger.warning(
            "Response hit max_tokens (%d) and was cut off -- retrying with %d (attempt %d/%d)",
            budget, next_budget, attempt + 1, _MAX_TRUNCATION_RETRIES,
        )
        budget = next_budget
    return text


def _complete_anthropic(system: str | None, user_message: str, max_tokens: int) -> tuple[str, bool]:
    client = anthropic.Anthropic(api_key=db.get_anthropic_api_key())
    kwargs = {
        "model": db.get_anthropic_model(), "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}],
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, response.stop_reason == "max_tokens"


def _complete_gemini(system: str | None, user_message: str, max_tokens: int) -> tuple[str, bool]:
    client = _gemini_client()
    config = genai_types.GenerateContentConfig(
        max_output_tokens=max_tokens, system_instruction=system,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=_GEMINI_THINKING_BUDGET),
    )
    response = _call_gemini(lambda: client.models.generate_content(
        model=db.get_gemini_model(), contents=user_message, config=config,
    ))
    return response.text or "", _gemini_hit_max_tokens(response)


def _web_search_anthropic(user_message: str, max_tokens: int) -> tuple[str, bool]:
    client = anthropic.Anthropic(api_key=db.get_anthropic_api_key())
    response = client.messages.create(
        model=db.get_anthropic_model(), max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_message}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
    )
    # The model often narrates its search process across several text blocks and only puts the
    # final answer in the last one -- joined with newlines (not concatenated bare) so a stray
    # brace in the narration can't fuse onto the real JSON answer and confuse extract_json.
    text_blocks = [block.text for block in response.content if block.type == "text"]
    return "\n".join(text_blocks), response.stop_reason == "max_tokens"


def _web_search_gemini(user_message: str, max_tokens: int) -> tuple[str, bool]:
    client = _gemini_client()
    config = genai_types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
        thinking_config=genai_types.ThinkingConfig(thinking_budget=_GEMINI_THINKING_BUDGET),
    )
    response = _call_gemini(lambda: client.models.generate_content(
        model=db.get_gemini_model(), contents=user_message, config=config,
    ))
    return response.text or "", _gemini_hit_max_tokens(response)


def _gemini_hit_max_tokens(response) -> bool:
    candidates = getattr(response, "candidates", None)
    return bool(candidates) and candidates[0].finish_reason == genai_types.FinishReason.MAX_TOKENS


# GPT-5-family models spend internal reasoning tokens that count against max_completion_tokens,
# the same failure mode as Gemini's thinking budget above -- a generous answer budget can still
# be eaten alive by reasoning before a single output character is written. There's no separate
# reasoning-token cap in the chat completions API, but "low" effort keeps the burn small without
# disabling reasoning outright. Only sent to api.openai.com (whose curated model list all
# accepts it) -- an arbitrary OpenAI-compatible server may reject the parameter entirely.
_OPENAI_REASONING_EFFORT = "low"


def _openai_messages(system: str | None, turns: list[dict]) -> list[dict]:
    """Assembles OpenAI's messages array: an optional leading system message, then the
    conversation turns as-is (their user/assistant role names already match OpenAI's). Shared by
    the single-message completion path (one synthetic user turn) and the multi-turn chat path."""
    messages = [{"role": "system", "content": system}] if system else []
    messages.extend(turns)
    return messages


def _openai_chat_send(client, model: str, messages: list[dict], extra: dict) -> tuple[str, bool]:
    response = _call_openai(lambda: client.chat.completions.create(
        model=model, messages=messages, **extra,
    ))
    choice = response.choices[0]
    return choice.message.content or "", choice.finish_reason == "length"


def _openai_chat_completion(client, model: str, system: str | None,
                            user_message: str, extra: dict) -> tuple[str, bool]:
    messages = _openai_messages(system, [{"role": "user", "content": user_message}])
    return _openai_chat_send(client, model, messages, extra)


def _complete_openai(system: str | None, user_message: str, max_tokens: int) -> tuple[str, bool]:
    # max_completion_tokens, not max_tokens: the GPT-5 family rejects the legacy parameter.
    return _openai_chat_completion(
        _openai_client(), db.get_openai_model(), system, user_message,
        {"max_completion_tokens": max_tokens, "reasoning_effort": _OPENAI_REASONING_EFFORT},
    )


def _complete_openai_compat(system: str | None, user_message: str, max_tokens: int) -> tuple[str, bool]:
    # The legacy max_tokens parameter here, deliberately: it's the one every OpenAI-compatible
    # server understands, while max_completion_tokens support is far from universal.
    return _openai_chat_completion(
        _openai_compat_client(), db.get_openai_compat_model(), system, user_message,
        {"max_tokens": max_tokens},
    )


def _web_search_openai(user_message: str, max_tokens: int) -> tuple[str, bool]:
    """Web search on OpenAI means the Responses API -- chat completions has no search tool."""
    client = _openai_client()
    response = _call_openai(lambda: client.responses.create(
        model=db.get_openai_model(), input=user_message,
        tools=[{"type": "web_search"}], max_output_tokens=max_tokens,
        reasoning={"effort": _OPENAI_REASONING_EFFORT},
    ))
    truncated = (
        getattr(response, "status", None) == "incomplete"
        and getattr(getattr(response, "incomplete_details", None), "reason", None) == "max_output_tokens"
    )
    return response.output_text or "", truncated


def _web_search_openai_compat(user_message: str, max_tokens: int) -> tuple[str, bool]:
    raise RuntimeError(
        "The OpenAI-compatible provider has no web search tool - "
        "disable the web search fallback in Settings or switch providers."
    )


# Multi-turn chat impls (see complete_chat above). Each mirrors its single-turn _complete_*
# sibling exactly -- same client, model getter, retry wrapper, thinking/reasoning cap, and
# truncation signal -- differing only in passing the whole conversation array instead of one
# user_message.


def _chat_anthropic(system: str | None, messages: list[dict], max_tokens: int) -> tuple[str, bool]:
    client = anthropic.Anthropic(api_key=db.get_anthropic_api_key())
    kwargs = {"model": db.get_anthropic_model(), "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, response.stop_reason == "max_tokens"


def _chat_gemini(system: str | None, messages: list[dict], max_tokens: int) -> tuple[str, bool]:
    client = _gemini_client()
    # Gemini names the assistant role "model" and wants each turn as {"role", "parts": [...]}.
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    config = genai_types.GenerateContentConfig(
        max_output_tokens=max_tokens, system_instruction=system,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=_GEMINI_THINKING_BUDGET),
    )
    response = _call_gemini(lambda: client.models.generate_content(
        model=db.get_gemini_model(), contents=contents, config=config,
    ))
    return response.text or "", _gemini_hit_max_tokens(response)


def _chat_openai(system: str | None, messages: list[dict], max_tokens: int) -> tuple[str, bool]:
    return _openai_chat_send(
        _openai_client(), db.get_openai_model(), _openai_messages(system, messages),
        {"max_completion_tokens": max_tokens, "reasoning_effort": _OPENAI_REASONING_EFFORT},
    )


def _chat_openai_compat(system: str | None, messages: list[dict], max_tokens: int) -> tuple[str, bool]:
    return _openai_chat_send(
        _openai_compat_client(), db.get_openai_compat_model(), _openai_messages(system, messages),
        {"max_tokens": max_tokens},
    )

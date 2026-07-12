"""Pluggable AI provider -- Anthropic or Gemini, chosen (along with each provider's own API
key and model) on the Settings page rather than baked in at deploy time via compose-file env
vars. Every AI call site in summarizer.py and release_notes.py's web search fallback goes
through complete_text()/web_search() here instead of instantiating a provider SDK client
directly, so switching providers in Settings takes effect for every feature immediately, with
no redeploy -- the whole point being able to switch away from a provider that's temporarily
out of credits without touching the compose file at all.

Deliberately just an if/else dispatch over two known providers, not a plugin registry -- there
are exactly two providers to support, and a third would still only mean one more branch here,
not a new abstraction.
"""

import logging
import time

import anthropic
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app import db
from app.config import settings

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


def is_configured() -> bool:
    """True if the currently-selected provider has an API key on file. Every call site in
    summarizer.py/release_notes.py already early-outs on this before spending any effort
    building a prompt, exactly like they used to early-out on settings.anthropic_api_key."""
    if db.get_ai_provider() == "gemini":
        return bool(db.get_gemini_api_key())
    return bool(db.get_anthropic_api_key())


def concurrency_limit() -> int:
    """How many AI calls persist.py's fan-out phases (release notes web search fallback,
    summarization) should run at once. Used to be forced to 1 for Gemini regardless of tier --
    the free tier's request/minute cap is tight enough that a handful of concurrent calls
    exhausts it almost immediately -- but a paid Gemini key has no such constraint (nor does
    Anthropic), and _call_gemini()'s own retry/backoff already handles an occasional 429 or
    503 gracefully under concurrency, so there's no longer a blanket reason to serialize."""
    return settings.ai_summarize_concurrency


# If a response hits its max_tokens ceiling exactly rather than finishing naturally, it's been
# cut off mid-sentence -- worse than the modest extra cost of one retry at a larger budget. Every
# call site picks max_tokens sized for the typical case, not the occasional longer one, so this
# is what actually makes truncation not happen in practice rather than each site over-budgeting
# "just in case" (which would still have a ceiling that could eventually be hit).
_TRUNCATION_RETRY_MULTIPLIER = 2


def complete_text(system: str | None, user_message: str, max_tokens: int) -> str:
    """Single-turn text completion, provider-agnostic. Raises on failure -- same contract the
    direct anthropic.Anthropic() calls this replaces always had; every caller already handles
    an exception here as "this attempt failed," regardless of which provider raised it."""
    fn = _complete_gemini if db.get_ai_provider() == "gemini" else _complete_anthropic
    return _with_truncation_retry(lambda mt: fn(system, user_message, mt), max_tokens)


def web_search(user_message: str, max_tokens: int) -> str:
    """Same shape as complete_text, but with the provider's own web-search/grounding tool
    enabled -- backs release_notes.py's web search fallback. Billed per search the model
    actually performs on top of the normal completion cost for both providers, which is why
    this is only ever reached behind its own opt-in Settings toggle -- gating that is the
    caller's job, not this function's."""
    fn = _web_search_gemini if db.get_ai_provider() == "gemini" else _web_search_anthropic
    return _with_truncation_retry(lambda mt: fn(user_message, mt), max_tokens)


def _with_truncation_retry(fn, max_tokens: int) -> str:
    text, truncated = fn(max_tokens)
    if truncated:
        retry_tokens = max_tokens * _TRUNCATION_RETRY_MULTIPLIER
        logger.warning(
            "Response hit max_tokens (%d) and was cut off -- retrying once with %d",
            max_tokens, retry_tokens,
        )
        text, _ = fn(retry_tokens)
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
    config = genai_types.GenerateContentConfig(max_output_tokens=max_tokens, system_instruction=system)
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
    )
    response = _call_gemini(lambda: client.models.generate_content(
        model=db.get_gemini_model(), contents=user_message, config=config,
    ))
    return response.text or "", _gemini_hit_max_tokens(response)


def _gemini_hit_max_tokens(response) -> bool:
    candidates = getattr(response, "candidates", None)
    return bool(candidates) and candidates[0].finish_reason == genai_types.FinishReason.MAX_TOKENS

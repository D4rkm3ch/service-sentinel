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

import anthropic
from google import genai
from google.genai import types as genai_types

from app import db

logger = logging.getLogger("release_radar.ai_provider")


def is_configured() -> bool:
    """True if the currently-selected provider has an API key on file. Every call site in
    summarizer.py/release_notes.py already early-outs on this before spending any effort
    building a prompt, exactly like they used to early-out on settings.anthropic_api_key."""
    if db.get_ai_provider() == "gemini":
        return bool(db.get_gemini_api_key())
    return bool(db.get_anthropic_api_key())


def complete_text(system: str | None, user_message: str, max_tokens: int) -> str:
    """Single-turn text completion, provider-agnostic. Raises on failure -- same contract the
    direct anthropic.Anthropic() calls this replaces always had; every caller already handles
    an exception here as "this attempt failed," regardless of which provider raised it."""
    if db.get_ai_provider() == "gemini":
        return _complete_gemini(system, user_message, max_tokens)
    return _complete_anthropic(system, user_message, max_tokens)


def web_search(user_message: str, max_tokens: int) -> str:
    """Same shape as complete_text, but with the provider's own web-search/grounding tool
    enabled -- backs release_notes.py's web search fallback. Billed per search the model
    actually performs on top of the normal completion cost for both providers, which is why
    this is only ever reached behind its own opt-in Settings toggle -- gating that is the
    caller's job, not this function's."""
    if db.get_ai_provider() == "gemini":
        return _web_search_gemini(user_message, max_tokens)
    return _web_search_anthropic(user_message, max_tokens)


def _complete_anthropic(system: str | None, user_message: str, max_tokens: int) -> str:
    client = anthropic.Anthropic(api_key=db.get_anthropic_api_key())
    kwargs = {
        "model": db.get_anthropic_model(), "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}],
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    return "".join(block.text for block in response.content if block.type == "text")


def _complete_gemini(system: str | None, user_message: str, max_tokens: int) -> str:
    client = genai.Client(api_key=db.get_gemini_api_key())
    config = genai_types.GenerateContentConfig(max_output_tokens=max_tokens, system_instruction=system)
    response = client.models.generate_content(
        model=db.get_gemini_model(), contents=user_message, config=config,
    )
    return response.text or ""


def _web_search_anthropic(user_message: str, max_tokens: int) -> str:
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
    return "\n".join(text_blocks)


def _web_search_gemini(user_message: str, max_tokens: int) -> str:
    client = genai.Client(api_key=db.get_gemini_api_key())
    config = genai_types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
    )
    response = client.models.generate_content(
        model=db.get_gemini_model(), contents=user_message, config=config,
    )
    return response.text or ""

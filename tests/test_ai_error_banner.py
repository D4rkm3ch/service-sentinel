"""AI provider failures hit during a background check (scheduled, Check Now, Regenerate) get
surfaced in the topbar instead of only ever landing in the container logs -- a real-world report
that a bad key or an exhausted quota silently produced empty results with nothing telling the
operator why. The interactive chat widget already had this covered (a failure there becomes an
inline chat bubble, see test_chat_route.py) -- this is the other half: everywhere else.

The record is server-side (app.db.get_last_ai_error/set_last_ai_error/dismiss_last_ai_error), not
held in whichever browser tab happened to be open when the failure occurred, so it survives the
once-a-second topbar poll and any page navigation, and is only ever cleared by an explicit
dismissal (POST /ai-error/dismiss) -- never by time, and never by a later check simply succeeding
without the operator having looked."""

from unittest.mock import MagicMock, patch

import anthropic
import openai
import pytest

from app import ai_provider, db

db.init_db()


@pytest.fixture(autouse=True)
def clean_state():
    db.dismiss_last_ai_error()
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("sk-test")
    yield
    db.dismiss_last_ai_error()
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")


# ---------------------------------------------------------------------------
# db.py round trip
# ---------------------------------------------------------------------------

def test_no_error_recorded_by_default():
    assert db.get_last_ai_error() is None


def test_set_and_get_round_trips_the_message():
    db.set_last_ai_error("The AI provider rejected the configured API key.")
    error = db.get_last_ai_error()
    assert error["message"] == "The AI provider rejected the configured API key."
    assert error["at"]  # a timestamp was recorded too


def test_dismiss_clears_it():
    db.set_last_ai_error("Something went wrong.")
    db.dismiss_last_ai_error()
    assert db.get_last_ai_error() is None


def test_a_later_set_overwrites_rather_than_stacking():
    """One slot, not a log -- only the most recent failure matters for a banner that just says
    "something's wrong right now"."""
    db.set_last_ai_error("First failure.")
    db.set_last_ai_error("Second failure.")
    assert db.get_last_ai_error()["message"] == "Second failure."


# ---------------------------------------------------------------------------
# ai_provider._friendly_ai_error -- classification
# ---------------------------------------------------------------------------

def test_anthropic_auth_error_reads_as_a_bad_key():
    exc = anthropic.AuthenticationError("bad key", response=MagicMock(), body=None)
    assert "rejected" in ai_provider._friendly_ai_error(exc).lower()


def test_openai_auth_error_reads_as_a_bad_key():
    exc = openai.AuthenticationError("bad key", response=MagicMock(status_code=401), body=None)
    assert "rejected" in ai_provider._friendly_ai_error(exc).lower()


def test_openai_rate_limit_error_mentions_rate_limiting():
    exc = openai.RateLimitError("429", response=MagicMock(status_code=429), body=None)
    assert "rate-limiting" in ai_provider._friendly_ai_error(exc).lower()


def test_openai_connection_error_mentions_reachability():
    exc = openai.APIConnectionError(request=MagicMock())
    assert "reach" in ai_provider._friendly_ai_error(exc).lower()


def test_context_length_message_is_recognized_even_from_a_generic_exception():
    exc = ValueError("This model's maximum context length is 8192 tokens")
    assert "context" in ai_provider._friendly_ai_error(exc).lower()


def test_an_unrecognized_exception_still_produces_a_readable_fallback():
    exc = ValueError("something truly unexpected")
    message = ai_provider._friendly_ai_error(exc)
    assert "something truly unexpected" in message


# ---------------------------------------------------------------------------
# complete_text() / web_search() record on failure; complete_chat() deliberately doesn't
# ---------------------------------------------------------------------------

def test_complete_text_records_the_error_and_still_raises():
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.side_effect = anthropic.AuthenticationError(
            "bad key", response=MagicMock(), body=None
        )
        with pytest.raises(anthropic.AuthenticationError):
            ai_provider.complete_text(system=None, user_message="hi", max_tokens=100)
    error = db.get_last_ai_error()
    assert error is not None
    assert "rejected" in error["message"].lower()


def test_web_search_records_the_error_and_still_raises():
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.side_effect = anthropic.AuthenticationError(
            "bad key", response=MagicMock(), body=None
        )
        with pytest.raises(anthropic.AuthenticationError):
            ai_provider.web_search("hi", max_tokens=100)
    assert db.get_last_ai_error() is not None


def test_complete_chat_does_not_record_to_the_topbar_banner():
    """The chat widget already shows its own failures inline as a chat bubble (see app/chat.py,
    POST /chat/send) -- recording here too would show the same failure twice, once in the chat
    window and once as a topbar banner nobody asked for."""
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.side_effect = anthropic.AuthenticationError(
            "bad key", response=MagicMock(), body=None
        )
        with pytest.raises(anthropic.AuthenticationError):
            ai_provider.complete_chat(system=None, messages=[{"role": "user", "content": "hi"}], max_tokens=100)
    assert db.get_last_ai_error() is None


def test_a_successful_call_does_not_touch_a_previously_recorded_error():
    """complete_text only ever records on its OWN failure -- it doesn't clear a stale error left
    by an earlier failed check just because this particular call happened to succeed. Only an
    explicit dismissal (or a later failure overwriting it) changes the record."""
    db.set_last_ai_error("An earlier failure.")

    def _fake_response(text):
        resp = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = text
        resp.content = [block]
        return resp

    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _fake_response("ok")
        ai_provider.complete_text(system=None, user_message="hi", max_tokens=100)
    assert db.get_last_ai_error()["message"] == "An earlier failure."


# ---------------------------------------------------------------------------
# Routes: GET /checks/status carries it, POST /ai-error/dismiss clears it
# ---------------------------------------------------------------------------

def test_checks_status_carries_no_error_by_default(client):
    data = client.get("/checks/status").json()
    assert data["ai_error"] is None


def test_checks_status_carries_the_recorded_error(client):
    db.set_last_ai_error("The AI provider rejected the configured API key.")
    data = client.get("/checks/status").json()
    assert data["ai_error"] == "The AI provider rejected the configured API key."


def test_dismiss_route_clears_it_and_the_next_poll_reflects_that(client):
    db.set_last_ai_error("Something went wrong.")
    assert client.get("/checks/status").json()["ai_error"] == "Something went wrong."

    resp = client.post("/ai-error/dismiss")
    assert resp.status_code == 200

    assert client.get("/checks/status").json()["ai_error"] is None

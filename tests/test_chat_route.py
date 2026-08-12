"""The /chat/send route -- thin glue over chat.answer(). Covers the not-configured early-out,
the sanitized-html success shape, that a provider failure never leaks a raw exception, and that
the route is included in the sitewide rate limit (AI spend, same as the check routes)."""

from unittest.mock import patch

from app import db, main

db.init_db()


def _configure_anthropic():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("sk-test")


def _unconfigure():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")


def test_chat_send_reports_when_no_provider_is_configured(client):
    _unconfigure()
    resp = client.post("/chat/send", json={"history": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Settings" in body["error"]


def test_chat_send_returns_reply_markdown_and_sanitized_html(client):
    _configure_anthropic()
    try:
        with patch("app.chat.ai_provider.complete_chat", return_value="**romm-db** is unhealthy"):
            resp = client.post(
                "/chat/send", json={"history": [{"role": "user", "content": "what's wrong?"}]}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["markdown"] == "**romm-db** is unhealthy"
        # Rendered through the same sanitizer every other AI block uses -- bold becomes <strong>.
        assert "<strong>romm-db</strong>" in body["html"]
    finally:
        _unconfigure()


def test_chat_send_sanitizes_script_shaped_model_output(client):
    """Belt-and-suspenders: the reply goes through render_markdown, so even if the model echoed
    HTML/script-shaped text, the returned html is sanitized (same stored-XSS protection as
    release notes / findings)."""
    _configure_anthropic()
    try:
        with patch("app.chat.ai_provider.complete_chat", return_value="<script>alert(1)</script> hi"):
            resp = client.post(
                "/chat/send", json={"history": [{"role": "user", "content": "x"}]}
            )
        body = resp.json()
        assert "<script>" not in body["html"]
    finally:
        _unconfigure()


def test_chat_send_never_leaks_a_raw_exception_on_provider_failure(client):
    _configure_anthropic()
    try:
        with patch("app.chat.ai_provider.complete_chat", side_effect=RuntimeError("secret-provider-detail 401")):
            resp = client.post(
                "/chat/send", json={"history": [{"role": "user", "content": "hi"}]}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "secret-provider-detail" not in body["error"]
        assert "401" not in body["error"]
    finally:
        _unconfigure()


def test_chat_send_handles_an_empty_history_gracefully(client):
    _configure_anthropic()
    try:
        resp = client.post("/chat/send", json={"history": []})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
    finally:
        _unconfigure()


def test_chat_send_is_included_in_the_rate_limited_paths():
    assert main._is_rate_limited_path("/chat/send")


def test_confirm_action_is_included_in_the_rate_limited_paths():
    assert main._is_rate_limited_path("/chat/confirm-action")


# ---------------------------------------------------------------------------
# pending_actions -- the action-proposal feature (see app/chat.py, app/chat_actions.py)
# ---------------------------------------------------------------------------

def test_chat_send_passes_through_no_pending_actions_for_a_normal_reply(client):
    _configure_anthropic()
    try:
        with patch("app.chat.ai_provider.complete_chat", return_value="just an answer"):
            resp = client.post("/chat/send", json={"history": [{"role": "user", "content": "hi"}]})
        assert resp.json()["pending_actions"] == []
    finally:
        _unconfigure()


def test_chat_send_passes_through_a_proposed_action(client):
    _configure_anthropic()
    reply = (
        '```action-proposal\n{"actions": [{"type": "add_custom_rule", "source": "logs", '
        '"rule_type": "exclude", "name": "n", "instruction": "x", "reason": "y"}]}\n```'
    )
    try:
        with patch("app.chat.ai_provider.complete_chat", return_value=reply):
            resp = client.post("/chat/send", json={"history": [{"role": "user", "content": "hi"}]})
        body = resp.json()
        assert body["ok"] is True
        assert "action-proposal" not in body["markdown"]
        assert len(body["pending_actions"]) == 1
        assert body["pending_actions"][0]["type"] == "add_custom_rule"
    finally:
        _unconfigure()


# ---------------------------------------------------------------------------
# POST /chat/confirm-action -- only reachable via an explicit Confirm click
# ---------------------------------------------------------------------------

def test_confirm_action_executes_a_valid_action(client):
    resp = client.post("/chat/confirm-action", json={
        "action": {"type": "add_custom_rule", "source": "logs", "rule_type": "exclude", "name": "n", "instruction": "x"},
    })
    try:
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["rule"]["name"] == "n"
        assert db.list_ai_custom_rules("logs")
    finally:
        for rule in db.list_ai_custom_rules("logs"):
            db.remove_ai_custom_rule(rule["id"])


def test_confirm_action_rejects_a_malformed_payload(client):
    resp = client.post("/chat/confirm-action", json={"action": None})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_confirm_action_rejects_malformed_json_entirely(client):
    resp = client.post("/chat/confirm-action", content=b"not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400

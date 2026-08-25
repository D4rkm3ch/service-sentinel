"""The "Suggest Ignore Rule" button on a finding's own detail page -- a shortcut into the
existing chat/action-proposal pipeline (app/chat.py, app/chat_actions.py), not a new way to
change anything. Clicking it seeds the chat panel with this exact finding's text and sends it as
an ordinary chat message (see base.html's askServiceSentinelChat/data-chat-ask wiring); the
operator still talks it over and clicks Confirm on the same proposal card any other rule request
would produce. main.py's finding_detail route builds the seed text server-side (rather than
leaving it to the chat's own context snapshot) since that snapshot only itemizes a handful of
findings per module -- a busy dashboard's finding could easily be one it never actually lists."""

from pathlib import Path

from app import db

_TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_finding_detail_page_has_the_suggest_ignore_rule_button(client):
    fid, _ = db.upsert_finding("logs", "ignore-rule-btn-test", "Some parse failure", "error", "critical", "desc text")
    resp = client.get(f"/findings/{fid}")
    assert resp.status_code == 200
    assert "Suggest Ignore Rule" in resp.text
    assert 'data-chat-ask="' in resp.text
    db.set_finding_status(fid, "silenced")


def test_the_seed_prompt_reflects_the_actual_finding_text(client):
    fid, _ = db.upsert_finding(
        "logs", "ignore-rule-btn-content", "Release title parsing errors", "error", "critical",
        "Lidarr is unable to parse the title of some releases.",
    )
    resp = client.get(f"/findings/{fid}")
    assert "ignore-rule-btn-content" in resp.text
    assert "Release title parsing errors" in resp.text
    assert "Lidarr is unable to parse the title of some releases." in resp.text
    # Phrased as an explicit standing-rule request -- matches chat.py's SYSTEM_PROMPT_HEADER
    # worked example closely enough that the model treats it as one, not just a question.
    assert "standing rule" in resp.text
    assert "Runtime" in resp.text
    db.set_finding_status(fid, "silenced")


def test_the_seed_prompt_names_configuration_for_a_compose_finding(client):
    fid, _ = db.upsert_finding(
        "compose", "/stacks/ignore-rule-btn-compose.yml", "Missing restart policy", "reliability", "warning", "desc",
    )
    resp = client.get(f"/findings/{fid}")
    assert "Configuration" in resp.text
    db.set_finding_status(fid, "silenced")


def test_the_seed_prompt_is_html_escaped_in_the_attribute(client):
    """A finding's title/description are AI-authored text, not trusted input -- if either ever
    contained a literal quote or angle bracket, it must not be able to break out of the
    data-chat-ask="..." attribute (same discipline as the rest of this app's AI-authored content,
    see test_markdown_xss_sanitization.py)."""
    fid, _ = db.upsert_finding(
        "logs", "ignore-rule-btn-xss", 'Title with " and <script>alert(1)</script>', "error", "critical",
        'Description with " quotes and <b>tags</b>.',
    )
    resp = client.get(f"/findings/{fid}")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
    db.set_finding_status(fid, "silenced")


def test_a_missing_description_does_not_break_the_page(client):
    fid, _ = db.upsert_finding("logs", "ignore-rule-btn-nodesc", "No description case", "error", "warning", "")
    resp = client.get(f"/findings/{fid}")
    assert resp.status_code == 200
    assert "Suggest Ignore Rule" in resp.text
    db.set_finding_status(fid, "silenced")


# ---------------------------------------------------------------------------
# The shared front-end wiring in base.html
# ---------------------------------------------------------------------------

def test_base_html_exposes_the_ask_chat_hook_and_listens_for_data_chat_ask():
    text = (_TEMPLATES / "base.html").read_text()
    assert "window.askServiceSentinelChat" in text
    assert "data-chat-ask" in text
    assert "closest(\"[data-chat-ask]\")" in text

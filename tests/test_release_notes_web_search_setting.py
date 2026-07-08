"""The Settings toggle for the release-notes web search fallback (Stage 8, brought forward) --
off by default, persisted via db.py the same way every other Deep-Analysis-style toggle is."""

import pytest

from app import db

db.init_db()


@pytest.fixture(autouse=True)
def reset_setting():
    db.set_release_notes_web_search_enabled(False)
    yield
    db.set_release_notes_web_search_enabled(False)


def test_settings_page_reflects_the_current_value(client):
    page = client.get("/settings")
    assert 'id="release_notes_web_search"' in page.text
    assert "checked" not in page.text[page.text.index('id="release_notes_web_search"'):page.text.index(">", page.text.index('id="release_notes_web_search"'))]

    db.set_release_notes_web_search_enabled(True)
    page = client.get("/settings")
    start = page.text.index('id="release_notes_web_search"')
    end = page.text.index(">", start)
    assert "checked" in page.text[start:end]


def test_posting_the_toggle_persists_the_setting(client):
    assert db.get_release_notes_web_search_enabled() is False

    resp = client.post("/settings/release-notes/web-search", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_release_notes_web_search_enabled() is True

    resp = client.post("/settings/release-notes/web-search", data={})
    assert resp.status_code == 200
    assert db.get_release_notes_web_search_enabled() is False

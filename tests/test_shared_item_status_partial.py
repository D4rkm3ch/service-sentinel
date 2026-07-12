"""Every scoped action (Updates item/stack, Logs container/stack, Compose file) renders its
spinner/progress fragment from ONE shared pair of templates -- _item_status.html and
_item_status_poll.html -- parameterized by poll_url. Previously five byte-identical copies of
each existed, one per scope, differing only in a hardcoded poll URL; they inevitably drift
apart the moment someone edits one and forgets the other four. These tests pin both halves:
the copies stay deleted, and each scope's endpoint still wires in its own correct poll URL."""

from pathlib import Path

from fastapi.testclient import TestClient

from app import check_state, db
from app.main import app

db.init_db()

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"

client = TestClient(app)


def _fresh(item_key: str, label: str):
    check_state.clear_item(item_key)
    check_state.start_item(item_key, label)
    check_state.set_item_progress(item_key, "checking", 1, 2)


def test_the_scope_specific_status_partial_copies_stay_deleted():
    leftovers = [
        p.name for p in TEMPLATES.glob("*.html")
        if p.name.endswith(("item_status.html", "item_status_poll.html"))
        and p.name not in ("_item_status.html", "_item_status_poll.html")
    ]
    assert leftovers == [], f"scope-specific status partial copies crept back in: {leftovers}"


def test_shared_partials_use_poll_url_not_a_hardcoded_endpoint():
    for name in ("_item_status.html", "_item_status_poll.html"):
        text = (TEMPLATES / name).read_text()
        assert 'hx-get="{{ poll_url }}"' in text
        assert "/status-poll" not in text, f"{name} hardcodes an endpoint again"


def test_each_scopes_poll_endpoint_rearms_with_its_own_url_while_running():
    cases = [
        ("update:77", "sonarr", "/updates/77/recheck-status-poll",
         "/updates/77/recheck-status-poll"),
        ("stack:media", "media", "/updates/stack/status-poll?stack_id=media",
         "/updates/stack/status-poll?stack_id=media"),
        ("logitem:sonarr", "sonarr", "/logs/container/sonarr/status-poll",
         "/logs/container/sonarr/status-poll"),
        ("logstack:media", "media", "/logs/stack/status-poll?stack_id=media",
         "/logs/stack/status-poll?stack_id=media"),
        ("composeitem:/mnt/a.yaml", "/mnt/a.yaml", "/compose/file/status-poll?path=/mnt/a.yaml",
         "/compose/file/status-poll?path=/mnt/a.yaml"),
    ]
    for item_key, label, endpoint, rearm_url in cases:
        _fresh(item_key, label)
        try:
            resp = client.get(endpoint)
            assert resp.status_code == 200
            assert f'hx-get="{rearm_url}"' in resp.text, f"{endpoint} lost its own poll URL"
            assert "Checking" in resp.text  # the shared progress text actually rendered
        finally:
            check_state.clear_item(item_key)


def test_finished_scoped_polls_still_redirect_to_their_own_pages():
    cases = [
        ("stack:media", "/updates/stack/status-poll?stack_id=media", "/updates/stack?id=media"),
        ("logitem:sonarr", "/logs/container/sonarr/status-poll", "/logs/container/sonarr"),
        ("logstack:media", "/logs/stack/status-poll?stack_id=media", "/logs/stack?id=media"),
        ("composeitem:/mnt/a.yaml", "/compose/file/status-poll?path=/mnt/a.yaml",
         "/compose/file?path=/mnt/a.yaml"),
    ]
    for item_key, endpoint, redirect in cases:
        check_state.clear_item(item_key)  # no running item -> the finished branch
        resp = client.get(endpoint)
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") == redirect

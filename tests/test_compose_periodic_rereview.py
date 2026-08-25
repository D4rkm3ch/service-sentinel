"""Compose periodic re-review (Settings -> Configuration -> "Periodic Re-review", off by
default): an unchanged compose file is normally skipped entirely, never re-sent to the AI at
all, so a finding for one can only ever clear by editing that file, silencing it by hand, or a
Reset & re-check. Turning this on re-reviews a file on a schedule even with no edit, so something
fixed another way (a new standing rule, a config change elsewhere) can eventually clear on its
own the same evidence-based way Logs already can."""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import compose_reviewer, db
from app.config import settings

db.init_db()


def _compose_file(name: str, content: str) -> Path:
    path = Path(settings.compose_root) / name
    path.write_text(content)
    return path


def _backdate_review(path_str: str, days: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.get_conn() as conn:
        conn.execute("UPDATE compose_file_state SET last_reviewed_at = ? WHERE file_path = ?", (ts, path_str))


def setup_function(_):
    db.set_compose_rereview_after("off")


def teardown_function(_):
    db.set_compose_rereview_after("off")


# ---------------------------------------------------------------------------
# db layer
# ---------------------------------------------------------------------------

def test_defaults_to_off():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'compose_rereview_after_days'")
    assert db.get_compose_rereview_after() == "off"
    assert db.get_compose_rereview_after_days() is None


def test_set_and_get():
    db.set_compose_rereview_after("30")
    assert db.get_compose_rereview_after() == "30"
    assert db.get_compose_rereview_after_days() == 30


def test_get_compose_file_last_reviewed_is_batched_and_empty_for_unknown_paths():
    assert db.get_compose_file_last_reviewed([]) == {}
    assert db.get_compose_file_last_reviewed(["/nonexistent/path.yml"]) == {}


# ---------------------------------------------------------------------------
# Wiring into run_compose_check_for's fast pass
# ---------------------------------------------------------------------------

def test_an_unchanged_file_is_skipped_when_rereview_is_off():
    content = "services:\n  rereview-off-test:\n    image: owner/rereview-off-test\n"
    path = _compose_file("rereview-off-test.yml", content)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    db.set_compose_file_hash(str(path), content_hash)
    _backdate_review(str(path), 90)
    try:
        with patch("app.compose_reviewer.review_compose_file") as fake:
            result = compose_reviewer.run_compose_check_for([path])
        fake.assert_not_called()
        assert result["reviewed"] == 0
    finally:
        path.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(path),))


def test_an_unchanged_file_past_the_threshold_is_rereviewed_when_enabled():
    content = "services:\n  rereview-due-test:\n    image: owner/rereview-due-test\n"
    path = _compose_file("rereview-due-test.yml", content)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    db.set_compose_file_hash(str(path), content_hash)
    _backdate_review(str(path), 90)
    db.set_compose_rereview_after("30")
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[]) as fake:
            result = compose_reviewer.run_compose_check_for([path])
        fake.assert_called_once()
        assert result["reviewed"] == 1
        # A forced re-review resets the periodic timer -- last_reviewed_at should be fresh again.
        refreshed = db.get_compose_file_last_reviewed([str(path)])[str(path)]
        assert refreshed > (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    finally:
        path.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(path),))
            conn.execute("DELETE FROM findings WHERE subject = ?", (str(path),))


def test_an_unchanged_file_not_yet_due_stays_skipped_even_when_enabled():
    content = "services:\n  rereview-not-due-test:\n    image: owner/rereview-not-due-test\n"
    path = _compose_file("rereview-not-due-test.yml", content)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    db.set_compose_file_hash(str(path), content_hash)
    _backdate_review(str(path), 5)  # reviewed 5 days ago
    db.set_compose_rereview_after("30")  # due after 30
    try:
        with patch("app.compose_reviewer.review_compose_file") as fake:
            result = compose_reviewer.run_compose_check_for([path])
        fake.assert_not_called()
        assert result["reviewed"] == 0
    finally:
        path.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(path),))


# ---------------------------------------------------------------------------
# Settings control
# ---------------------------------------------------------------------------

def test_settings_page_reflects_the_current_value(client):
    db.set_compose_rereview_after("30")
    resp = client.get("/settings")
    assert '<option value="30" selected>' in resp.text


def test_save_route_updates_the_setting(client):
    resp = client.post("/settings/compose-rereview-after", data={"compose_rereview_after_days": "30"})
    assert resp.status_code == 200
    assert db.get_compose_rereview_after() == "30"


def test_save_route_rejects_an_unknown_value(client):
    resp = client.post("/settings/compose-rereview-after", data={"compose_rereview_after_days": "not-a-real-choice"})
    assert resp.status_code == 400
    assert db.get_compose_rereview_after() != "not-a-real-choice"


def test_a_genuinely_changed_file_is_still_reviewed_regardless_of_the_setting():
    """The periodic setting only widens WHEN an unchanged file gets re-sent -- it must never
    narrow the existing "content actually changed" path."""
    old_content = "services:\n  rereview-changed-test:\n    image: owner/old\n"
    path = _compose_file("rereview-changed-test.yml", old_content)
    db.set_compose_file_hash(str(path), hashlib.sha256(old_content.encode()).hexdigest())
    new_content = "services:\n  rereview-changed-test:\n    image: owner/new\n"
    path.write_text(new_content)
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[]) as fake:
            result = compose_reviewer.run_compose_check_for([path])
        fake.assert_called_once()
        assert result["reviewed"] == 1
    finally:
        path.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(path),))
            conn.execute("DELETE FROM findings WHERE subject = ?", (str(path),))

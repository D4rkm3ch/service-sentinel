"""Auto-silence findings for removed containers (Settings -> Runtime -> "Removed Containers",
off by default): a container that's stopped, been renamed, or no longer exists drops out of
list_running_containers_for_logs() entirely, so nothing ever sends its findings fresh evidence
again -- the resolution-checking run_log_check_for already does (see its own docstring) can only
judge a finding against evidence it actually receives, and a removed container never provides
any. Left alone, such a finding stays "active" forever with no path to clearing it besides the
operator noticing by hand. Turning this on silences it once the container has genuinely been
gone past a configurable grace period."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import db, log_watcher

db.init_db()


def _set_checkpoint(container_name: str, days_ago: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db.set_log_watch_checkpoints([container_name], at=ts)


def setup_function(_):
    db.set_logs_auto_silence_removed_enabled(False)
    db.set_logs_auto_silence_removed_after("7")


def teardown_function(_):
    db.set_logs_auto_silence_removed_enabled(False)
    db.set_logs_auto_silence_removed_after("7")


def _cleanup(name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM findings WHERE subject = ?", (name,))
        conn.execute("DELETE FROM log_watch_state WHERE container_name = ?", (name,))


# ---------------------------------------------------------------------------
# db layer
# ---------------------------------------------------------------------------

def test_defaults_to_disabled_with_a_7_day_grace_period():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key IN "
                     "('logs_auto_silence_removed_enabled', 'logs_auto_silence_removed_after_days')")
    assert db.get_logs_auto_silence_removed_enabled() is False
    assert db.get_logs_auto_silence_removed_after_days() == 7


def test_set_and_get_enabled():
    db.set_logs_auto_silence_removed_enabled(True)
    assert db.get_logs_auto_silence_removed_enabled() is True


def test_set_and_get_grace_period():
    db.set_logs_auto_silence_removed_after("30")
    assert db.get_logs_auto_silence_removed_after() == "30"
    assert db.get_logs_auto_silence_removed_after_days() == 30


# ---------------------------------------------------------------------------
# _auto_silence_removed_containers_safely directly
# ---------------------------------------------------------------------------

def test_disabled_does_nothing_even_for_a_long_gone_container():
    name = "auto-silence-disabled-test"
    fid, _ = db.upsert_finding("logs", name, "some issue", "error", "warning", "d")
    _set_checkpoint(name, 30)
    try:
        log_watcher._auto_silence_removed_containers_safely([])
        assert db.get_finding(fid)["status"] == "active"
    finally:
        _cleanup(name)


def test_enabled_silences_a_container_past_its_grace_period():
    name = "auto-silence-due-test"
    fid, _ = db.upsert_finding("logs", name, "some issue", "error", "warning", "d")
    _set_checkpoint(name, 30)
    db.set_logs_auto_silence_removed_enabled(True)
    db.set_logs_auto_silence_removed_after("7")
    try:
        log_watcher._auto_silence_removed_containers_safely([])
        assert db.get_finding(fid)["status"] == "silenced"
    finally:
        _cleanup(name)


def test_enabled_leaves_a_recently_seen_container_alone():
    name = "auto-silence-not-due-test"
    fid, _ = db.upsert_finding("logs", name, "some issue", "error", "warning", "d")
    _set_checkpoint(name, 2)  # last checked 2 days ago, grace is 7
    db.set_logs_auto_silence_removed_enabled(True)
    db.set_logs_auto_silence_removed_after("7")
    try:
        log_watcher._auto_silence_removed_containers_safely([])
        assert db.get_finding(fid)["status"] == "active"
    finally:
        _cleanup(name)


def test_enabled_leaves_a_still_running_container_alone():
    name = "auto-silence-still-running-test"
    fid, _ = db.upsert_finding("logs", name, "some issue", "error", "warning", "d")
    _set_checkpoint(name, 30)
    db.set_logs_auto_silence_removed_enabled(True)
    try:
        log_watcher._auto_silence_removed_containers_safely([name])
        assert db.get_finding(fid)["status"] == "active"
    finally:
        _cleanup(name)


def test_never_touches_an_already_silenced_finding_differently_than_expected():
    name = "auto-silence-already-silenced-test"
    fid, _ = db.upsert_finding("logs", name, "some issue", "error", "warning", "d")
    db.set_finding_status(fid, "silenced")
    _set_checkpoint(name, 30)
    db.set_logs_auto_silence_removed_enabled(True)
    try:
        # Should not raise, and the already-silenced finding stays silenced.
        log_watcher._auto_silence_removed_containers_safely([])
        assert db.get_finding(fid)["status"] == "silenced"
    finally:
        _cleanup(name)


def test_never_raises_even_if_something_inside_goes_wrong():
    db.set_logs_auto_silence_removed_enabled(True)
    with patch("app.log_watcher.db.list_subjects_with_findings", side_effect=RuntimeError("boom")):
        log_watcher._auto_silence_removed_containers_safely([])  # must not raise


# ---------------------------------------------------------------------------
# Settings control
# ---------------------------------------------------------------------------

def test_settings_page_reflects_the_current_values(client):
    db.set_logs_auto_silence_removed_enabled(True)
    db.set_logs_auto_silence_removed_after("30")
    resp = client.get("/settings")
    assert 'id="logs_auto_silence_removed_enabled"' in resp.text
    assert 'checked' in resp.text.split('id="logs_auto_silence_removed_enabled"')[1][:120]
    assert '<option value="30" selected>' in resp.text


def test_save_enabled_route_updates_the_setting(client):
    resp = client.post("/settings/logs-auto-silence-removed", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_logs_auto_silence_removed_enabled() is True


def test_save_after_route_updates_the_setting(client):
    resp = client.post("/settings/logs-auto-silence-removed-after", data={"logs_auto_silence_removed_after_days": "30"})
    assert resp.status_code == 200
    assert db.get_logs_auto_silence_removed_after() == "30"


def test_save_after_route_rejects_an_unknown_value(client):
    resp = client.post("/settings/logs-auto-silence-removed-after", data={"logs_auto_silence_removed_after_days": "bogus"})
    assert resp.status_code == 400
    assert db.get_logs_auto_silence_removed_after() != "bogus"


# ---------------------------------------------------------------------------
# Wired end to end through run_log_check
# ---------------------------------------------------------------------------

def test_run_log_check_silences_a_removed_containers_findings_when_enabled():
    name = "auto-silence-e2e-test"
    fid, _ = db.upsert_finding("logs", name, "some issue", "error", "warning", "d")
    _set_checkpoint(name, 30)
    db.set_logs_auto_silence_removed_enabled(True)
    db.set_logs_auto_silence_removed_after("7")
    try:
        with patch("app.log_watcher.list_running_containers_for_logs", return_value=[]):
            log_watcher.run_log_check()
        assert db.get_finding(fid)["status"] == "silenced"
    finally:
        _cleanup(name)


def test_run_log_check_leaves_findings_alone_when_the_setting_is_off():
    name = "auto-silence-e2e-off-test"
    fid, _ = db.upsert_finding("logs", name, "some issue", "error", "warning", "d")
    _set_checkpoint(name, 30)
    try:
        with patch("app.log_watcher.list_running_containers_for_logs", return_value=[]):
            log_watcher.run_log_check()
        assert db.get_finding(fid)["status"] == "active"
    finally:
        _cleanup(name)

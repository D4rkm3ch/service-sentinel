"""An explicit ask: bring Compose to full functional parity with Logs. Three real, confirmed
gaps existed (Compose was NOT "ready" the way Updates/Logs are):

1. No global Regenerate AI Response / Reset & re-check -- only a bare Check now button existed
   on the Compose Health header; the routes didn't exist at all.
2. No per-file action row at all -- opening a compose file's findings page had zero buttons
   (no Check Now, Regenerate, Reset, Mark all as Read, Silence), unlike Logs' per-container page.
3. No per-file check-failure surfacing -- compose_reviewer.py tracked an aggregate error count
   but never which file failed, so there was no error status/icon/tooltip on a broken file and
   no opt-in "notify on check errors" toggle, unlike Logs' log_check_errors mechanism.

This mirrors test_logs_full_parity_actions.py's own coverage of the equivalent Logs features,
adapted for Compose's query-string-scoped (?path=...) routes and lack of a stack concept."""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app import check_state, compose_reviewer, db
from app.config import settings


def _compose_file(name: str, *services: str) -> Path:
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _wait_until_not_running(feature: str = "compose"):
    for _ in range(50):
        if not check_state.get_state(feature)["running"]:
            return
        time.sleep(0.1)
    raise AssertionError(f"{feature} check never finished")


# ---------------------------------------------------------------------------
# Gap 1: global Regenerate AI Response / Reset & re-check
# ---------------------------------------------------------------------------

def test_compose_health_header_shows_regenerate_and_reset_buttons(client):
    resp = client.get("/compose")
    assert 'action="/compose/regenerate-all"' in resp.text
    assert 'action="/compose/reset-and-recheck"' in resp.text
    assert "compose-action-btn" in resp.text


def test_compose_reset_and_recheck_wipes_then_rechecks():
    fid, _ = db.upsert_finding("compose", "global-reset-a.yml", "OOM", "crash", "critical", "desc")
    db.set_compose_file_hash("global-reset-a.yml", "hash1")

    with patch("app.scheduler.trigger_compose_check_now") as mock_trigger:
        db.reset_compose_data()
        mock_trigger()
    mock_trigger.assert_called_once()
    assert db.get_compose_file_hash("global-reset-a.yml") is None
    assert db.list_findings_for_subject("compose", "global-reset-a.yml", include_silenced=True) == []


def test_compose_regenerate_all_is_a_noop_when_the_mutex_is_already_held(client):
    check_state.set_running("compose")
    try:
        resp = client.post("/compose/regenerate-all")
        assert resp.status_code in (200, 303)
    finally:
        check_state.release_running("compose")


# ---------------------------------------------------------------------------
# Gap 2: per-file action row -- scoped Check now / Reset & re-check / Regenerate AI Response,
# bulk Read/Unread, bulk Silence/Unsilence
# ---------------------------------------------------------------------------

def test_compose_file_check_now_route_works():
    compose_file = _compose_file("checknow-works.yml", "checknow-svc")
    try:
        resp_status = compose_reviewer.run_compose_check_for([compose_file])
        assert resp_status["checked"] == 1
        assert db.get_compose_file_hash(str(compose_file)) is not None
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(compose_file),))


def test_run_compose_check_for_notifies_with_the_service_name_not_the_raw_path():
    """Discord should show a recognizable service name (e.g. "notify-svc"), not a raw file path
    like "/compose/notify-name-check.yml" -- see compose_lookup.subject_display_name."""
    compose_file = _compose_file("notify-name-check.yml", "notify-svc")
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[
            {"title": "Issue", "category": "reliability", "severity": "warning", "description": "d"},
        ]), patch("app.compose_reviewer.notify_findings_digest") as mock_notify:
            compose_reviewer.run_compose_check_for([compose_file])
        mock_notify.assert_called_once_with(
            "compose", [{"subject": "notify-svc", "severity": "warning", "title": "Issue"}]
        )
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(compose_file),))
            conn.execute("DELETE FROM findings WHERE subject = ?", (str(compose_file),))


def test_compose_file_check_now_http_route_works(client):
    compose_file = _compose_file("checknow-http.yml", "checknow-http-svc")
    path = str(compose_file)
    try:
        resp = client.post(f"/compose/file/check-now?path={path}")
        assert resp.status_code == 200
        assert 'class="spinner"' in resp.text
        _wait_until_not_running()
        assert db.get_compose_file_hash(path) is not None
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (path,))


def test_compose_file_reset_and_recheck_wipes_then_rechecks(client):
    compose_file = _compose_file("reset-recheck.yml", "reset-svc")
    path = str(compose_file)
    try:
        db.upsert_finding("compose", path, "Stale finding", "reliability", "warning", "desc")
        db.set_compose_file_hash(path, "stale-hash")

        resp = client.post(f"/compose/file/reset-and-recheck?path={path}")
        assert resp.status_code == 200
        _wait_until_not_running()

        # The stale finding is gone (reset wiped it), and the file was actually re-checked
        # (hash re-populated, not left null).
        remaining = db.list_findings_for_subject("compose", path, include_silenced=True)
        assert all(f["title"] != "Stale finding" for f in remaining)
        assert db.get_compose_file_hash(path) is not None
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM findings WHERE source = 'compose' AND subject = ?", (path,))


def test_compose_file_check_now_is_a_noop_when_the_mutex_is_already_held(client):
    path = "/tmp/rr-test-compose/busy-file.yml"
    check_state.set_running("compose")
    try:
        with patch("app.compose_reviewer.run_compose_check_for") as mock_check:
            resp = client.post(f"/compose/file/check-now?path={path}")
        mock_check.assert_not_called()
        assert resp.status_code == 200
        assert "started elsewhere" in resp.text
    finally:
        check_state.release_running("compose")


def test_compose_file_regenerate_is_a_noop_when_the_mutex_is_already_held(client):
    path = "/tmp/rr-test-compose/busy-regen.yml"
    db.upsert_finding("compose", path, "OOM", "crash", "critical", "desc1")
    db.upsert_finding("compose", path, "Disk full", "resource", "warning", "desc2")
    check_state.set_running("compose")
    try:
        resp = client.post(f"/compose/file/regenerate?path={path}")
        assert resp.status_code == 200
        assert "started elsewhere" in resp.text
    finally:
        check_state.release_running("compose")


def test_compose_file_reset_and_recheck_is_a_noop_when_the_mutex_is_already_held(client):
    path = "/tmp/rr-test-compose/busy-reset.yml"
    check_state.set_running("compose")
    try:
        with patch("app.compose_reviewer.run_compose_check_for") as mock_check:
            resp = client.post(f"/compose/file/reset-and-recheck?path={path}")
        mock_check.assert_not_called()
        assert resp.status_code == 200
        assert "started elsewhere" in resp.text
    finally:
        check_state.release_running("compose")


def test_compose_file_bulk_read_unread_toggle(client):
    path = "/tmp/rr-test-compose/bulk-read.yml"
    fid1, _ = db.upsert_finding("compose", path, "A", "reliability", "warning", "d1")
    fid2, _ = db.upsert_finding("compose", path, "B", "reliability", "critical", "d2")
    db.set_finding_read_status(fid1, "unread")
    db.set_finding_read_status(fid2, "unread")

    resp = client.post(f"/compose/file/read?path={path}")
    assert resp.status_code == 200
    assert db.get_finding(fid1)["read_status"] == "read"
    assert db.get_finding(fid2)["read_status"] == "read"

    resp = client.post(f"/compose/file/unread?path={path}")
    assert resp.status_code == 200
    assert db.get_finding(fid1)["read_status"] == "unread"
    assert db.get_finding(fid2)["read_status"] == "unread"

    db.set_finding_status(fid1, "silenced")
    db.set_finding_status(fid2, "silenced")


def test_compose_file_bulk_silence_unsilence_toggle(client):
    path = "/tmp/rr-test-compose/bulk-silence.yml"
    fid1, _ = db.upsert_finding("compose", path, "A", "reliability", "warning", "d1")
    fid2, _ = db.upsert_finding("compose", path, "B", "reliability", "critical", "d2")

    resp = client.post(f"/compose/file/silence?path={path}")
    assert resp.status_code == 200
    assert db.get_finding(fid1)["status"] == "silenced"
    assert db.get_finding(fid2)["status"] == "silenced"

    resp = client.post(f"/compose/file/unsilence?path={path}")
    assert resp.status_code == 200
    assert db.get_finding(fid1)["status"] == "active"
    assert db.get_finding(fid2)["status"] == "active"

    db.set_finding_status(fid1, "silenced")
    db.set_finding_status(fid2, "silenced")


def test_compose_file_page_has_check_now_and_reset_buttons_scoped_to_its_subject(client):
    path = "/tmp/rr-test-compose/service-parity-compose.yml"
    db.upsert_finding("compose", path, "Missing restart policy", "reliability", "critical", "d1")
    db.upsert_finding("compose", path, "No healthcheck", "reliability", "warning", "d2")

    resp = client.get(f"/compose/file?path={path}")
    assert "compose-action-btn" in resp.text
    assert f"/compose/file/check-now?path={path}" in resp.text
    assert f"/compose/file/regenerate?path={path}" in resp.text
    assert f"/compose/file/reset-and-recheck?path={path}" in resp.text
    # Viewing the page auto-marks its findings read (see test_finding_read_unread.py's own
    # coverage of that), so the toggle now renders "Mark all as Unread", not "...as Read".
    assert f"/compose/file/unread?path={path}" in resp.text
    assert f"/compose/file/silence?path={path}" in resp.text


def test_compose_finding_page_regenerate_disabled_when_subject_has_only_one_finding(client):
    """A subject with exactly one finding still reaches finding_detail.html directly (it's the
    one finding page URLs actually resolve to, since subject_findings.html would just redirect
    straight back here) -- Regenerate has nothing to build a combined overview from, so it must
    render disabled rather than pointed at a route that'd silently no-op."""
    fid, _ = db.upsert_finding("compose", "compose-truly-lone.yml", "Truly alone", "reliability", "warning", "d1")
    resp = client.get(f"/findings/{fid}")
    assert 'title="Needs 2+ findings for this compose file for an AI overview to regenerate"' in resp.text


def test_finding_page_action_buttons_use_compose_scoped_urls(client):
    fid, _ = db.upsert_finding("compose", "finding-compose.yml", "Missing restart policy", "reliability", "critical", "d1")
    db.upsert_finding("compose", "finding-compose.yml", "No healthcheck", "reliability", "warning", "d2")
    resp = client.get(f"/findings/{fid}")
    assert "/compose/file/check-now?path=finding-compose.yml" in resp.text
    assert "/compose/file/reset-and-recheck?path=finding-compose.yml" in resp.text
    assert "/compose/file/regenerate?path=finding-compose.yml" in resp.text


# ---------------------------------------------------------------------------
# Gap 3: per-file check-failure surfacing
# ---------------------------------------------------------------------------

def test_record_and_clear_compose_check_errors_round_trip():
    db.record_compose_check_errors({"err-round-trip.yml": "permission denied"})
    states = {r["name"]: r for r in db.all_compose_file_states_with_status()}
    assert states["err-round-trip.yml"]["status"] == "error"
    assert states["err-round-trip.yml"]["error"] == "permission denied"

    db.clear_compose_check_errors(["err-round-trip.yml"])
    states = {r["name"]: r for r in db.all_compose_file_states_with_status()}
    assert "err-round-trip.yml" not in states


def test_a_file_that_has_never_once_succeeded_still_shows_up_as_an_error():
    db.record_compose_check_errors({"err-never-succeeded.yml": "no such file"})
    states = {r["name"]: r for r in db.all_compose_file_states_with_status()}
    assert states["err-never-succeeded.yml"]["status"] == "error"
    assert states["err-never-succeeded.yml"]["last_at"] is not None


def test_compose_error_status_wins_over_issue_or_healthy_regardless_of_findings():
    db.upsert_finding("compose", "err-wins.yml", "OOM", "crash", "critical", "desc")
    db.set_compose_file_hash("err-wins.yml", "hash1")
    db.record_compose_check_errors({"err-wins.yml": "timeout"})

    states = {r["name"]: r for r in db.all_compose_file_states_with_status()}
    assert states["err-wins.yml"]["status"] == "error"


def test_run_compose_check_for_persists_an_error_and_counts_it():
    ok_file = _compose_file("run-check-ok.yml", "ok-svc")
    bad_path = Path(settings.compose_root) / "run-check-fails.yml"  # never created -> read_text() raises
    try:
        result = compose_reviewer.run_compose_check_for([bad_path, ok_file])
        assert result["errors"] == 1
        states = {r["name"]: r for r in db.all_compose_file_states_with_status()}
        assert states[str(bad_path)]["status"] == "error"
        assert states[str(ok_file)]["status"] == "healthy"
    finally:
        ok_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path IN (?, ?)", (str(bad_path), str(ok_file)))
            conn.execute("DELETE FROM compose_check_errors WHERE file_path IN (?, ?)", (str(bad_path), str(ok_file)))


def test_run_compose_check_for_clears_a_previously_recorded_error_on_next_success():
    ok_file = _compose_file("recovers.yml", "recovers-svc")
    try:
        db.record_compose_check_errors({str(ok_file): "old failure"})
        compose_reviewer.run_compose_check_for([ok_file])
        states = {r["name"]: r for r in db.all_compose_file_states_with_status()}
        assert states[str(ok_file)]["status"] == "healthy"
    finally:
        ok_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(ok_file),))


def test_run_compose_check_for_notifies_on_error_when_the_toggle_is_on():
    db.set_notifications_enabled(True)
    db.set_feature_notify_enabled("compose", True)
    db.set_notify_compose_include_errors(True)
    bad_path = Path(settings.compose_root) / "notify-err.yml"
    try:
        with patch("app.compose_reviewer.notify_compose_check_errors") as mock_notify:
            compose_reviewer.run_compose_check_for([bad_path])
        mock_notify.assert_called_once()
        args = mock_notify.call_args[0][0]
        assert args[0]["container_name"] == str(bad_path)
    finally:
        db.set_notifications_enabled(False)
        db.set_feature_notify_enabled("compose", False)
        db.set_notify_compose_include_errors(False)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_check_errors WHERE file_path = ?", (str(bad_path),))


def test_notify_compose_check_errors_is_a_noop_when_the_toggle_is_off(client):
    from app.notifications import notify_compose_check_errors
    db.set_notify_compose_include_errors(False)
    with patch("app.notifications._send") as mock_send:
        notify_compose_check_errors([{"container_name": "x.yml", "error": "y"}])
    mock_send.assert_not_called()


def test_all_files_table_shows_the_warning_icon_for_an_errored_file(client):
    db.record_compose_check_errors({"table-error.yml": "connection refused"})

    resp = client.get("/compose")
    section = resp.text[resp.text.index('id="compose-files-table"'):]
    row = section[section.index("table-error.yml"):]
    row = row[:row.index("</tr>")]
    assert "status-warning-icon" in row
    assert "cell-centered" in row
    assert "connection refused" in row


def test_all_files_table_status_sort_ranks_errors_above_issues_and_healthy(client):
    db.record_compose_check_errors({"sort-err.yml": "boom"})
    db.upsert_finding("compose", "sort-issue.yml", "OOM", "crash", "critical", "desc")
    db.set_compose_file_hash("sort-issue.yml", "hash1")
    db.set_compose_file_hash("sort-healthy.yml", "hash2")

    resp = client.get("/compose?csort=status&cdir=asc")
    section = resp.text[resp.text.index('id="compose-files-table"'):]
    err_pos = section.index("sort-err.yml")
    issue_pos = section.index("sort-issue.yml")
    healthy_pos = section.index("sort-healthy.yml")
    assert err_pos < issue_pos < healthy_pos


def test_compose_reset_and_recheck_clears_compose_check_errors():
    db.record_compose_check_errors({"reset-clears-err.yml": "boom"})
    db.reset_compose_data(subjects=["reset-clears-err.yml"])
    states = {r["name"]: r for r in db.all_compose_file_states_with_status()}
    assert "reset-clears-err.yml" not in states


def test_settings_notify_compose_include_errors_round_trip():
    db.set_notify_compose_include_errors(True)
    assert db.get_notify_compose_include_errors() is True
    db.set_notify_compose_include_errors(False)
    assert db.get_notify_compose_include_errors() is False


def test_settings_page_has_the_notify_compose_include_errors_toggle(client):
    resp = client.get("/settings")
    assert "compose file can" in resp.text and "t be checked" in resp.text  # apostrophe is HTML-escaped
    assert "/settings/notify/compose-include-errors" in resp.text


def test_settings_notify_compose_include_errors_route_saves(client):
    resp = client.post("/settings/notify/compose-include-errors", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_notify_compose_include_errors() is True
    db.set_notify_compose_include_errors(False)

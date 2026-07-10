"""An explicit ask: bring Logs to full action-button/silence parity with Updates -- a 3-button
row (Check now / Regenerate AI Response / Reset & re-check) at the global, stack, and service
(per-container) levels, service- and stack-level bulk Read/Unread and Silence/Unsilence, and a
new "Partially Silenced" badge state for when silencing cascades down from a stack/service but
some findings underneath it are still active. Also covers the log_watcher.py connection-batching
fix (mirroring the same regression test_persist.py already has for Updates) and the Issues
table's new sortable Stack column + the Severity-after-Last-seen column reorder."""

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app import check_state, compose_lookup, db, log_watcher, main, stacks
from app.config import settings

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
        conn.execute("DELETE FROM log_watch_state")
        conn.execute("DELETE FROM findings")
        conn.execute("DELETE FROM subject_summaries")
        conn.execute("DELETE FROM log_check_errors")
    db.set_cross_service_analysis_enabled("logs", False)
    yield
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
        conn.execute("DELETE FROM log_watch_state")
        conn.execute("DELETE FROM findings")
        conn.execute("DELETE FROM subject_summaries")
        conn.execute("DELETE FROM log_check_errors")
    db.set_cross_service_analysis_enabled("logs", False)


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _stack_id_for(container_name):
    return compose_lookup.match_container_to_stack(container_name, compose_lookup.build_stack_index())["stack_id"]


def _wait_until_not_running(feature: str = "logs"):
    for _ in range(50):
        if not check_state.get_state(feature)["running"]:
            return
        time.sleep(0.1)
    raise AssertionError(f"{feature} check never finished")


# ---------------------------------------------------------------------------
# db.py -- reset_logs_data, bulk silence/read, batched checkpoints
# ---------------------------------------------------------------------------

def test_reset_logs_data_global_wipes_findings_checkpoint_and_overview():
    db.set_log_watch_checkpoint("reset-global-a")
    db.upsert_finding("logs", "reset-global-a", "OOM", "crash", "critical", "desc")
    db.set_subject_summary("logs", "reset-global-a", "hash1", "An overview.")

    db.reset_logs_data()

    assert db.get_log_watch_checkpoint("reset-global-a") is None
    assert db.list_findings_for_subject("logs", "reset-global-a", include_silenced=True) == []
    assert db.get_subject_summary("logs", "reset-global-a") is None


def test_reset_logs_data_scoped_only_touches_the_given_subjects():
    db.set_log_watch_checkpoint("reset-scoped-a")
    db.set_log_watch_checkpoint("reset-scoped-b")
    db.upsert_finding("logs", "reset-scoped-a", "OOM", "crash", "critical", "desc")
    db.upsert_finding("logs", "reset-scoped-b", "Disk full", "resource", "warning", "desc")

    db.reset_logs_data(subjects=["reset-scoped-a"])

    assert db.get_log_watch_checkpoint("reset-scoped-a") is None
    assert db.list_findings_for_subject("logs", "reset-scoped-a", include_silenced=True) == []
    # untouched
    assert db.get_log_watch_checkpoint("reset-scoped-b") is not None
    assert len(db.list_findings_for_subject("logs", "reset-scoped-b", include_silenced=True)) == 1


def test_reset_logs_data_with_an_empty_subject_list_is_a_noop():
    db.set_log_watch_checkpoint("reset-empty-a")
    db.reset_logs_data(subjects=[])
    assert db.get_log_watch_checkpoint("reset-empty-a") is not None


def test_get_and_set_log_watch_checkpoints_are_batched():
    db.set_log_watch_checkpoints(["batch-cp-a", "batch-cp-b"])
    checkpoints = db.get_log_watch_checkpoints(["batch-cp-a", "batch-cp-b", "batch-cp-missing"])
    assert checkpoints["batch-cp-a"] is not None
    assert checkpoints["batch-cp-b"] is not None
    assert "batch-cp-missing" not in checkpoints


def test_silence_all_findings_for_subjects_only_touches_active_ones():
    fid1, _ = db.upsert_finding("logs", "bulk-silence-a", "OOM", "crash", "critical", "desc")
    fid2, _ = db.upsert_finding("logs", "bulk-silence-a", "Disk full", "resource", "warning", "desc2")
    db.set_finding_status(fid2, "silenced")

    db.silence_all_findings_for_subjects("logs", ["bulk-silence-a"])

    assert db.get_finding(fid1)["status"] == "silenced"
    assert db.get_finding(fid2)["status"] == "silenced"  # already silenced, untouched either way


def test_unsilence_all_findings_for_subjects_only_touches_silenced_ones():
    fid1, _ = db.upsert_finding("logs", "bulk-unsilence-a", "OOM", "crash", "critical", "desc")
    fid2, _ = db.upsert_finding("logs", "bulk-unsilence-a", "Disk full", "resource", "warning", "desc2")
    db.set_finding_status(fid1, "silenced")

    db.unsilence_all_findings_for_subjects("logs", ["bulk-unsilence-a"])

    assert db.get_finding(fid1)["status"] == "active"
    assert db.get_finding(fid2)["status"] == "active"


def test_set_findings_read_status_for_subject_only_touches_active_findings():
    fid1, _ = db.upsert_finding("logs", "bulk-read-a", "OOM", "crash", "critical", "desc")
    fid2, _ = db.upsert_finding("logs", "bulk-read-a", "Disk full", "resource", "warning", "desc2")
    db.set_finding_status(fid2, "silenced")
    db.set_finding_read_status(fid2, "unread")

    db.set_findings_read_status_for_subject("logs", "bulk-read-a", "read")

    assert db.get_finding(fid1)["read_status"] == "read"
    assert db.get_finding(fid2)["read_status"] == "unread"  # silenced -- left alone


# ---------------------------------------------------------------------------
# log_watcher.py -- connection batching + run_log_check_for + scoped claimed functions
# ---------------------------------------------------------------------------

def test_run_log_check_for_uses_a_fixed_number_of_connections_not_one_per_container():
    """Regression test mirroring test_persist.py's own connection-count guard: checkpoint
    read/write used to be one small sqlite3.connect() per container (get_log_watch_checkpoint /
    set_log_watch_checkpoint called inside the per-container loop). Batched now -- exactly 3
    connections (checkpoint read, checkpoint write, clearing any stale check-error rows for the
    containers that just succeeded) for the whole pass, regardless of container count. No
    connection for recording NEW errors here since none occur in this all-clean scenario --
    see the sibling test below for that path."""
    names = [f"conn-count-{i}" for i in range(15)]
    original_connect = sqlite3.connect
    connect_calls = []

    def counting_connect(*args, **kwargs):
        connect_calls.append(1)
        return original_connect(*args, **kwargs)

    with patch("app.log_watcher.get_container_logs_since", return_value=None), \
         patch("app.db.sqlite3.connect", side_effect=counting_connect):
        result = log_watcher.run_log_check_for(names)

    assert connect_calls == [1, 1, 1], f"expected a fixed 3-connection batch, got {len(connect_calls)}"
    assert result == {"checked": 15, "findings_found": 0, "errors": 0}


def test_run_log_check_for_only_stamps_the_checkpoint_for_containers_that_fetched_successfully():
    def _fake_logs(name, since, max_lines):
        if name == "checkpoint-fail":
            raise RuntimeError("docker socket down")
        return None

    with patch("app.log_watcher.get_container_logs_since", side_effect=_fake_logs):
        log_watcher.run_log_check_for(["checkpoint-ok", "checkpoint-fail"])

    assert db.get_log_watch_checkpoint("checkpoint-ok") is not None
    assert db.get_log_watch_checkpoint("checkpoint-fail") is None


def test_run_log_check_for_scoped_to_only_the_given_containers():
    with patch("app.log_watcher.get_container_logs_since", return_value=None):
        result = log_watcher.run_log_check_for(["scoped-a", "scoped-b"])
    assert result["checked"] == 2
    assert db.get_log_watch_checkpoint("scoped-a") is not None
    assert db.get_log_watch_checkpoint("scoped-b") is not None


def test_run_log_check_for_creates_findings_from_a_suspicious_excerpt():
    with patch("app.log_watcher.get_container_logs_since", return_value="ERROR: disk full"), \
         patch("app.log_watcher.extract_suspicious_excerpt", return_value="ERROR: disk full"), \
         patch("app.log_watcher.analyze_logs_batch", return_value=[
             {"container": "triage-a", "title": "Disk full", "category": "resource",
              "severity": "critical", "description": "desc"},
         ]), \
         patch("app.log_watcher.notify_finding") as mock_notify:
        result = log_watcher.run_log_check_for(["triage-a"])

    assert result["findings_found"] == 1
    findings = db.list_findings_for_subject("logs", "triage-a", include_silenced=True)
    assert len(findings) == 1
    assert findings[0]["title"] == "Disk full"
    mock_notify.assert_called_once()


def test_run_claimed_log_item_check_now_releases_the_mutex_on_completion():
    check_state.start_item("logitem:claim-a", "claim-a")
    with patch("app.log_watcher.run_log_check_for") as mock_check:
        log_watcher.run_claimed_log_item_check_now("logitem:claim-a", "claim-a")
    args, kwargs = mock_check.call_args
    assert args == (["claim-a"],)
    assert "on_progress" in kwargs  # real per-container progress, not a fake 0/1 bookend
    assert check_state.get_state("logs")["running"] is False
    assert check_state.get_item_state("logitem:claim-a")["running"] is False


def test_run_claimed_log_item_check_now_reports_real_progress_on_the_item_channel():
    check_state.start_item("logitem:progress-a", "progress-a")
    with patch("app.log_watcher.get_container_logs_since", return_value=None):
        log_watcher.run_claimed_log_item_check_now("logitem:progress-a", "progress-a")
    # finish_item() flips running False but leaves the last-reported stage/progress in place.
    item = check_state.get_item_state("logitem:progress-a")
    assert item["stage"] == "checking_logs"
    assert item["done"] == 1 and item["total"] == 1


def test_run_claimed_log_item_reset_and_recheck_wipes_then_rechecks():
    db.upsert_finding("logs", "claim-reset-a", "OOM", "crash", "critical", "desc")
    db.set_log_watch_checkpoint("claim-reset-a")

    with patch("app.log_watcher.get_container_logs_since", return_value=None):
        log_watcher.run_claimed_log_item_reset_and_recheck("logitem:claim-reset-a", "claim-reset-a")

    # The old finding is gone (wiped), and a fresh checkpoint was stamped by the re-check.
    assert db.list_findings_for_subject("logs", "claim-reset-a", include_silenced=True) == []
    assert db.get_log_watch_checkpoint("claim-reset-a") is not None
    assert check_state.get_state("logs")["running"] is False


def test_run_claimed_log_stack_check_now_checks_every_member():
    compose_file = _compose_file("claim-stack-check.yml", "claim-stack-svc-a", "claim-stack-svc-b")
    try:
        db.set_log_watch_checkpoint("claim-stack-svc-a")
        db.set_log_watch_checkpoint("claim-stack-svc-b")
        stack_id = _stack_id_for("claim-stack-svc-a")

        with patch("app.log_watcher.get_container_logs_since", return_value=None):
            log_watcher.run_claimed_log_stack_check_now("logstack:x", stack_id)

        assert check_state.get_state("logs")["running"] is False
    finally:
        compose_file.unlink()


def test_run_claimed_log_stack_reset_and_recheck_wipes_every_member():
    compose_file = _compose_file("claim-stack-reset.yml", "claim-reset-svc-a", "claim-reset-svc-b")
    try:
        db.upsert_finding("logs", "claim-reset-svc-a", "OOM", "crash", "critical", "desc")
        db.upsert_finding("logs", "claim-reset-svc-b", "Disk full", "resource", "warning", "desc2")
        db.set_log_watch_checkpoint("claim-reset-svc-a")
        db.set_log_watch_checkpoint("claim-reset-svc-b")
        stack_id = _stack_id_for("claim-reset-svc-a")

        with patch("app.log_watcher.get_container_logs_since", return_value=None):
            log_watcher.run_claimed_log_stack_reset_and_recheck("logstack:x", stack_id)

        assert db.list_findings_for_subject("logs", "claim-reset-svc-a", include_silenced=True) == []
        assert db.list_findings_for_subject("logs", "claim-reset-svc-b", include_silenced=True) == []
    finally:
        compose_file.unlink()


# ---------------------------------------------------------------------------
# main.py -- the 3-state silence model
# ---------------------------------------------------------------------------

def test_silence_state_is_none_when_nothing_is_silenced():
    assert main._silence_state(active_count=3, silenced_count=0) is None


def test_silence_state_is_none_when_there_are_no_findings_at_all():
    assert main._silence_state(active_count=0, silenced_count=0) is None


def test_silence_state_is_partially_silenced_when_some_but_not_all_are():
    assert main._silence_state(active_count=1, silenced_count=1) == "partially_silenced"


def test_silence_state_is_silenced_when_every_finding_is():
    assert main._silence_state(active_count=0, silenced_count=2) == "silenced"


# ---------------------------------------------------------------------------
# Global (main Logs page) -- Regenerate AI Response + Reset & re-check
# ---------------------------------------------------------------------------

def test_logs_page_has_all_three_buttons(client):
    resp = client.get("/logs")
    assert 'hx-post="/logs/check-now"' in resp.text
    assert 'action="/logs/regenerate-all"' in resp.text
    assert 'action="/logs/reset-and-recheck"' in resp.text


def test_updates_bulk_buttons_do_not_leak_onto_the_logs_page(client):
    resp = client.get("/logs")
    assert 'action="/updates/regenerate-all"' not in resp.text
    assert 'action="/updates/reset-and-recheck"' not in resp.text


def test_logs_reset_and_recheck_wipes_data_and_redirects(client):
    db.upsert_finding("logs", "global-reset-a", "OOM", "crash", "critical", "desc")
    db.set_log_watch_checkpoint("global-reset-a")

    try:
        with patch("app.scheduler.run_log_check"):
            resp = client.post("/logs/reset-and-recheck", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/logs"
        assert db.list_findings_for_subject("logs", "global-reset-a", include_silenced=True) == []
    finally:
        # The route's set_running("logs") is never cleared by the mocked-out check function --
        # release it so it doesn't leak into later tests' mutex-guarded routes.
        check_state.release_running("logs")


def test_logs_bulk_regenerate_force_regenerates_subject_overviews_and_stack_blurb(client):
    db.upsert_finding("logs", "bulk-regen-x", "OOM", "crash", "critical", "desc1")
    db.upsert_finding("logs", "bulk-regen-x", "Disk full", "resource", "warning", "desc2")
    db.set_log_watch_checkpoint("bulk-regen-x")

    with patch("app.main.summarize_findings_overview", return_value="A fresh overview.") as mock_overview:
        resp = client.post("/logs/regenerate-all", follow_redirects=False)
        assert resp.status_code == 303
        _wait_until_not_running("logs")

    mock_overview.assert_called()
    assert db.get_subject_summary("logs", "bulk-regen-x")["summary_markdown"] == "A fresh overview."


def test_logs_bulk_regenerate_refuses_to_start_while_a_check_is_already_running(client):
    check_state.set_running("logs")
    try:
        with patch("app.main._run_claimed_logs_bulk_regenerate") as mocked:
            resp = client.post("/logs/regenerate-all", follow_redirects=False)
        mocked.assert_not_called()
        assert resp.status_code == 303
    finally:
        check_state.release_running("logs")


# ---------------------------------------------------------------------------
# Stack level -- Check now / Reset & re-check / Silence-Unsilence + partial badge
# ---------------------------------------------------------------------------

def test_logs_stack_page_has_all_three_buttons_plus_silence_toggle(client):
    compose_file = _compose_file("parity-stack-buttons.yml", "parity-svc-a", "parity-svc-b")
    try:
        db.set_log_watch_checkpoint("parity-svc-a")
        db.set_log_watch_checkpoint("parity-svc-b")
        stack_id = _stack_id_for("parity-svc-a")

        resp = client.get(f"/logs/stack?id={stack_id}")
        assert f'/logs/stack/check-now?stack_id={stack_id}' in resp.text
        assert f'/logs/stack/reset-and-recheck?stack_id={stack_id}' in resp.text
        assert f'/logs/stack/silence?stack_id={stack_id}' in resp.text
    finally:
        compose_file.unlink()


def test_logs_stack_check_now_route_works(client):
    compose_file = _compose_file("stack-checknow.yml", "checknow-svc-a", "checknow-svc-b")
    try:
        db.set_log_watch_checkpoint("checknow-svc-a")
        db.set_log_watch_checkpoint("checknow-svc-b")
        stack_id = _stack_id_for("checknow-svc-a")

        with patch("app.log_watcher.get_container_logs_since", return_value=None):
            resp = client.post("/logs/stack/check-now", params={"stack_id": stack_id})
            assert resp.status_code == 200
            assert 'class="spinner"' in resp.text
            _wait_until_not_running()
    finally:
        compose_file.unlink()


def test_logs_stack_reset_and_recheck_route_wipes_and_rechecks(client):
    compose_file = _compose_file("stack-reset.yml", "reset-svc-a", "reset-svc-b")
    try:
        db.upsert_finding("logs", "reset-svc-a", "OOM", "crash", "critical", "desc")
        db.set_log_watch_checkpoint("reset-svc-a")
        db.set_log_watch_checkpoint("reset-svc-b")
        stack_id = _stack_id_for("reset-svc-a")

        with patch("app.log_watcher.get_container_logs_since", return_value=None):
            resp = client.post("/logs/stack/reset-and-recheck", params={"stack_id": stack_id})
            assert resp.status_code == 200
            _wait_until_not_running()

        assert db.list_findings_for_subject("logs", "reset-svc-a", include_silenced=True) == []
    finally:
        compose_file.unlink()


def test_logs_stack_silence_and_unsilence_cascade_to_every_member(client):
    compose_file = _compose_file("stack-silence.yml", "silence-svc-a", "silence-svc-b")
    try:
        fid1, _ = db.upsert_finding("logs", "silence-svc-a", "OOM", "crash", "critical", "desc")
        fid2, _ = db.upsert_finding("logs", "silence-svc-b", "Disk full", "resource", "warning", "desc2")
        db.set_log_watch_checkpoint("silence-svc-a")
        db.set_log_watch_checkpoint("silence-svc-b")
        stack_id = _stack_id_for("silence-svc-a")

        resp = client.post("/logs/stack/silence", params={"stack_id": stack_id})
        assert resp.status_code == 200
        assert "badge-silenced\">Silenced</span>" in resp.text
        assert db.get_finding(fid1)["status"] == "silenced"
        assert db.get_finding(fid2)["status"] == "silenced"

        resp = client.post("/logs/stack/unsilence", params={"stack_id": stack_id})
        assert db.get_finding(fid1)["status"] == "active"
        assert db.get_finding(fid2)["status"] == "active"
    finally:
        compose_file.unlink()


def test_logs_stack_shows_partially_silenced_when_only_some_members_are_fully_silenced(client):
    compose_file = _compose_file("stack-partial.yml", "partial-svc-a", "partial-svc-b")
    try:
        fid1, _ = db.upsert_finding("logs", "partial-svc-a", "OOM", "crash", "critical", "desc")
        db.upsert_finding("logs", "partial-svc-b", "Disk full", "resource", "warning", "desc2")
        db.set_finding_status(fid1, "silenced")
        db.set_log_watch_checkpoint("partial-svc-a")
        db.set_log_watch_checkpoint("partial-svc-b")
        stack_id = _stack_id_for("partial-svc-a")

        resp = client.get(f"/logs/stack?id={stack_id}")
        assert "badge-partially-silenced\">Partially Silenced</span>" in resp.text
    finally:
        compose_file.unlink()


def test_logs_stack_reset_and_recheck_with_missing_stack_id_is_rejected(client):
    resp = client.post("/logs/stack/reset-and-recheck")
    assert resp.status_code == 400


def test_logs_stack_check_now_with_missing_stack_id_is_rejected(client):
    resp = client.post("/logs/stack/check-now")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Service level -- Check now / Regenerate / Reset & re-check + Read/Unread + Silence/Unsilence
# ---------------------------------------------------------------------------

def test_service_page_has_the_full_action_row_for_logs(client):
    db.upsert_finding("logs", "service-parity-a", "OOM", "crash", "critical", "desc1")
    db.upsert_finding("logs", "service-parity-a", "Disk full", "resource", "warning", "desc2")

    resp = client.get("/logs/container/service-parity-a")
    assert 'hx-post="/logs/container/service-parity-a/check-now"' in resp.text
    assert 'hx-post="/logs/container/service-parity-a/regenerate"' in resp.text
    assert 'hx-post="/logs/container/service-parity-a/reset-and-recheck"' in resp.text
    assert 'hx-post="/logs/container/service-parity-a/read"' in resp.text or \
           'hx-post="/logs/container/service-parity-a/unread"' in resp.text
    assert 'hx-post="/logs/container/service-parity-a/silence"' in resp.text or \
           'hx-post="/logs/container/service-parity-a/unsilence"' in resp.text


def test_service_page_action_row_does_not_appear_for_compose(client):
    db.set_compose_file_hash("service-parity-compose.yml", "hash1")
    db.upsert_finding("compose", "service-parity-compose.yml", "Missing restart policy", "reliability", "critical", "d1")
    db.upsert_finding("compose", "service-parity-compose.yml", "No healthcheck", "reliability", "warning", "d2")

    resp = client.get("/compose/file?path=service-parity-compose.yml")
    assert "/check-now" not in resp.text
    assert "/regenerate" not in resp.text


def test_service_check_now_route_works(client):
    with patch("app.log_watcher.get_container_logs_since", return_value=None):
        resp = client.post("/logs/container/service-checknow-a/check-now")
        assert resp.status_code == 200
        assert 'class="spinner"' in resp.text
        _wait_until_not_running()
    assert db.get_log_watch_checkpoint("service-checknow-a") is not None


def test_service_reset_and_recheck_wipes_then_rechecks(client):
    db.upsert_finding("logs", "service-reset-a", "OOM", "crash", "critical", "desc")
    db.set_log_watch_checkpoint("service-reset-a")

    with patch("app.log_watcher.get_container_logs_since", return_value=None):
        resp = client.post("/logs/container/service-reset-a/reset-and-recheck")
        assert resp.status_code == 200
        _wait_until_not_running()

    assert db.list_findings_for_subject("logs", "service-reset-a", include_silenced=True) == []


def test_service_regenerate_force_regenerates_the_overview(client):
    db.upsert_finding("logs", "service-regen-a", "OOM", "crash", "critical", "desc1")
    db.upsert_finding("logs", "service-regen-a", "Disk full", "resource", "warning", "desc2")

    with patch("app.main.summarize_findings_overview", return_value="Fresh take.") as mock_overview:
        resp = client.post("/logs/container/service-regen-a/regenerate")
        assert resp.status_code == 200
        _wait_until_not_running()

    mock_overview.assert_called_once()
    assert db.get_subject_summary("logs", "service-regen-a")["summary_markdown"] == "Fresh take."


def test_service_page_still_offers_check_now_for_a_subject_with_no_findings(client):
    """A container with clean logs (or never checked at all) must still be actionable from its
    own page -- Check Now/Reset & re-check don't depend on any findings existing, only
    Regenerate/Read/Silence (which need something to act on) do."""
    resp = client.get("/logs/container/service-regen-empty-subject")
    assert "/check-now" in resp.text
    assert "/reset-and-recheck" in resp.text
    assert "/regenerate" not in resp.text
    assert "/read" not in resp.text and "/unread" not in resp.text
    assert "/silence" not in resp.text and "/unsilence" not in resp.text
    assert "Not checked yet" in resp.text


def test_service_page_shows_last_checked_time_when_clean(client):
    db.set_log_watch_checkpoint("service-clean-checked")
    resp = client.get("/logs/container/service-clean-checked")
    assert "Last checked" in resp.text
    assert "logs were clean" in resp.text
    assert "/check-now" in resp.text


def test_service_page_with_exactly_one_finding_redirects_straight_to_the_finding(client):
    fid, _ = db.upsert_finding("logs", "service-regen-single", "OOM", "crash", "critical", "desc")
    resp = client.get("/logs/container/service-regen-single", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/findings/{fid}"


def test_service_read_and_unread_toggle_every_active_finding(client):
    fid1, _ = db.upsert_finding("logs", "service-read-a", "OOM", "crash", "critical", "desc")
    fid2, _ = db.upsert_finding("logs", "service-read-a", "Disk full", "resource", "warning", "desc2")
    db.set_finding_read_status(fid1, "unread")
    db.set_finding_read_status(fid2, "unread")

    resp = client.post("/logs/container/service-read-a/read")
    assert resp.status_code == 200
    assert db.get_finding(fid1)["read_status"] == "read"
    assert db.get_finding(fid2)["read_status"] == "read"

    resp = client.post("/logs/container/service-read-a/unread")
    assert db.get_finding(fid1)["read_status"] == "unread"
    assert db.get_finding(fid2)["read_status"] == "unread"


def test_service_silence_and_unsilence_toggle_every_active_finding(client):
    fid1, _ = db.upsert_finding("logs", "service-silence-a", "OOM", "crash", "critical", "desc")
    fid2, _ = db.upsert_finding("logs", "service-silence-a", "Disk full", "resource", "warning", "desc2")

    resp = client.post("/logs/container/service-silence-a/silence")
    assert resp.status_code == 200
    assert "badge-silenced\">Silenced</span>" in resp.text
    assert db.get_finding(fid1)["status"] == "silenced"
    assert db.get_finding(fid2)["status"] == "silenced"

    resp = client.post("/logs/container/service-silence-a/unsilence")
    assert db.get_finding(fid1)["status"] == "active"
    assert db.get_finding(fid2)["status"] == "active"


def test_service_shows_partially_silenced_when_only_some_findings_are_silenced(client):
    fid1, _ = db.upsert_finding("logs", "service-partial-a", "OOM", "crash", "critical", "desc")
    db.upsert_finding("logs", "service-partial-a", "Disk full", "resource", "warning", "desc2")
    db.set_finding_status(fid1, "silenced")

    resp = client.get("/logs/container/service-partial-a")
    assert "badge-partially-silenced\">Partially Silenced</span>" in resp.text

    toggle_resp = client.post("/logs/container/service-partial-a/silence")
    assert "badge-silenced\">Silenced</span>" in toggle_resp.text


# ---------------------------------------------------------------------------
# Issues table -- new sortable Stack column + Severity moved after Last seen
# ---------------------------------------------------------------------------

def test_issues_table_has_a_sortable_stack_column_right_after_container(client):
    compose_file = _compose_file("issues-stack-col.yml", "issues-stack-svc-a", "issues-stack-svc-b")
    try:
        db.upsert_finding("logs", "issues-stack-svc-a", "OOM", "crash", "critical", "desc")
        db.set_log_watch_checkpoint("issues-stack-svc-a")
        db.set_log_watch_checkpoint("issues-stack-svc-b")
        stack_id = _stack_id_for("issues-stack-svc-a")

        resp = client.get("/logs")
        table = resp.text[resp.text.index('id="logs-issues-table"'):]
        header = table[:table.index("<tbody>")]
        # Sort-linked headers (see _sort_header.html's macro) wrap their label text across
        # lines ("<a ...>\n  Stack\n</a>"), so plain substrings are checked rather than a
        # tight ">Label<" match.
        assert "sort-link" in header and "Stack" in header
        assert "sort=stack" in header
        # Column order: Container header comes before Stack header comes before Findings header.
        assert header.index("Container") < header.index("Stack") < header.index("Findings")
        assert f'/logs/stack?id={stack_id}' in table
    finally:
        compose_file.unlink()


def test_issues_table_stack_column_is_not_shown_for_compose(client):
    db.set_compose_file_hash("issues-no-stack-col.yml", "hash1")
    db.upsert_finding("compose", "issues-no-stack-col.yml", "Missing restart policy", "reliability", "critical", "d1")

    resp = client.get("/compose")
    table = resp.text[resp.text.index('id="compose-issues-table"'):]
    header = table[:table.index("<tbody>")]
    assert "Stack" not in header


def test_issues_table_severity_column_is_right_of_last_seen(client):
    db.upsert_finding("logs", "issues-sev-order", "OOM", "crash", "critical", "desc")
    db.set_log_watch_checkpoint("issues-sev-order")

    resp = client.get("/logs")
    table = resp.text[resp.text.index('id="logs-issues-table"'):]
    header = table[:table.index("<tbody>")]
    # "Last seen" was renamed to "Detected" to match Updates' own column naming.
    assert header.index("Detected") < header.index("Severity")


def test_logs_stack_detail_severity_column_is_right_of_last_seen(client):
    compose_file = _compose_file("stack-sev-order.yml", "sev-order-svc-a", "sev-order-svc-b")
    try:
        db.set_log_watch_checkpoint("sev-order-svc-a")
        db.set_log_watch_checkpoint("sev-order-svc-b")
        stack_id = _stack_id_for("sev-order-svc-a")

        resp = client.get(f"/logs/stack?id={stack_id}")
        table = resp.text[resp.text.index("findings-table"):]
        header = table[:table.index("<tbody>")]
        # "Last seen" was renamed to "Detected" to match Updates' own column naming; headers
        # are now sort links (see _sort_header.html), not bare <th> text.
        assert header.index("sort=detected") < header.index("sort=severity")
    finally:
        compose_file.unlink()


def test_service_findings_table_severity_column_is_right_of_seen(client):
    db.upsert_finding("logs", "service-sev-order", "OOM", "crash", "critical", "desc1")
    db.upsert_finding("logs", "service-sev-order", "Disk full", "resource", "warning", "desc2")

    resp = client.get("/logs/container/service-sev-order")
    table = resp.text[resp.text.index("findings-table"):]
    header = table[:table.index("<tbody>")]
    # Headers are now sort links (see _sort_header.html), not bare <th> text.
    assert header.index("sort=seen") < header.index("sort=severity")


# ---------------------------------------------------------------------------
# The bottom "All containers" table's Silenced column becomes 3-state
# ---------------------------------------------------------------------------

def test_all_containers_table_shows_partially_silenced_badge(client):
    fid1, _ = db.upsert_finding("logs", "table-partial-a", "OOM", "crash", "critical", "desc")
    db.upsert_finding("logs", "table-partial-a", "Disk full", "resource", "warning", "desc2")
    db.set_finding_status(fid1, "silenced")
    db.set_log_watch_checkpoint("table-partial-a")

    resp = client.get("/logs")
    section = resp.text[resp.text.index('id="logs-containers-table"'):]
    row = section[section.index("table-partial-a"):]
    row = row[:row.index("</tr>")]
    assert "badge-partially-silenced\">Partially Silenced</span>" in row


# ---------------------------------------------------------------------------
# Finding-level page: Back to Stack link + Check Now/Regenerate/Reset & re-check buttons
# (mirroring detail.html's update-level action row), and an "Issue" heading above the
# description body to match the "Suggested fix" heading's treatment.
# ---------------------------------------------------------------------------

def test_finding_page_shows_an_issue_heading_above_the_description(client):
    fid, _ = db.upsert_finding("logs", "finding-heading-a", "OOM", "crash", "critical", "desc text")
    resp = client.get(f"/findings/{fid}")
    body = resp.text[resp.text.index('class="detail-body"'):]
    assert "<h3>Issue</h3>" in body
    assert body.index("<h3>Issue</h3>") < body.index("desc text")


def test_finding_page_has_check_now_and_reset_buttons_scoped_to_its_subject(client):
    fid, _ = db.upsert_finding("logs", "finding-buttons-a", "OOM", "crash", "critical", "desc")
    db.upsert_finding("logs", "finding-buttons-a", "Disk full", "resource", "warning", "desc2")

    resp = client.get(f"/findings/{fid}")
    assert 'hx-post="/logs/container/finding-buttons-a/check-now"' in resp.text
    assert 'hx-post="/logs/container/finding-buttons-a/reset-and-recheck"' in resp.text
    assert 'hx-post="/logs/container/finding-buttons-a/regenerate"' in resp.text


def test_finding_page_disables_regenerate_when_its_subject_has_fewer_than_two_findings(client):
    """This is actually the common case reachable here (a subject with exactly one finding
    redirects straight to this page), so Regenerate is disabled with an explanatory tooltip --
    same "always show all 3 buttons" shape as Updates' detail.html -- rather than hidden the
    way subject_findings.html's genuinely-unreachable <2 case is."""
    fid, _ = db.upsert_finding("logs", "finding-single-a", "OOM", "crash", "critical", "desc")
    resp = client.get(f"/findings/{fid}")
    assert 'hx-post="/logs/container/finding-single-a/check-now"' in resp.text
    assert 'hx-post="/logs/container/finding-single-a/regenerate"' not in resp.text
    assert "Regenerate AI Response" in resp.text
    assert 'button-warn" disabled' in resp.text


def test_finding_page_action_buttons_do_not_appear_for_compose(client):
    fid, _ = db.upsert_finding("compose", "finding-compose.yml", "Missing restart policy", "reliability", "critical", "d1")
    db.upsert_finding("compose", "finding-compose.yml", "No healthcheck", "reliability", "warning", "d2")
    resp = client.get(f"/findings/{fid}")
    assert "/check-now" not in resp.text
    assert "/reset-and-recheck" not in resp.text
    assert "/regenerate" not in resp.text


# ---------------------------------------------------------------------------
# Finding-level page: "Back to {service/file}" -- the 4th (Logs) / 3rd (Compose) hierarchy
# level was being skipped entirely on the way back up from an individual finding, landing
# straight on the top-level Logs/Compose page instead of the subject's own findings list.
# ---------------------------------------------------------------------------

def test_finding_page_shows_back_to_service_link_when_its_subject_has_multiple_findings(client):
    fid, _ = db.upsert_finding("logs", "finding-back-svc-a", "OOM", "crash", "critical", "desc")
    db.upsert_finding("logs", "finding-back-svc-a", "Disk full", "resource", "warning", "desc2")

    resp = client.get(f"/findings/{fid}")
    assert '/logs/container/finding-back-svc-a"' in resp.text
    assert "Back to finding-back-svc-a" in resp.text


def test_finding_page_hides_back_to_service_link_when_its_subject_has_only_this_one_finding(client):
    fid, _ = db.upsert_finding("logs", "finding-back-svc-solo", "OOM", "crash", "critical", "desc")
    resp = client.get(f"/findings/{fid}")
    back_link_row = resp.text[resp.text.index('class="back-link-row"'):resp.text.index("</div>")]
    assert "Back to finding-back-svc-solo" not in back_link_row


def test_finding_page_shows_back_to_file_link_for_compose_when_its_subject_has_multiple_findings(client):
    fid, _ = db.upsert_finding("compose", "back-to-file.yml", "Missing restart policy", "reliability", "critical", "d1")
    db.upsert_finding("compose", "back-to-file.yml", "No healthcheck", "reliability", "warning", "d2")

    resp = client.get(f"/findings/{fid}")
    assert "/compose/file?path=back-to-file.yml" in resp.text
    assert "Back to" in resp.text


def test_finding_page_shows_back_to_stack_link_for_a_stack_member(client):
    compose_file = _compose_file("finding-stack-link.yml", "finding-stack-svc-a", "finding-stack-svc-b")
    try:
        fid, _ = db.upsert_finding("logs", "finding-stack-svc-a", "OOM", "crash", "critical", "desc")
        db.upsert_finding("logs", "finding-stack-svc-a", "Disk full", "resource", "warning", "desc2")
        stack_id = _stack_id_for("finding-stack-svc-a")

        resp = client.get(f"/findings/{fid}")
        assert f'/logs/stack?id={stack_id}' in resp.text
        assert "Back to Stack" in resp.text
    finally:
        compose_file.unlink()


def test_finding_page_has_no_back_to_stack_link_for_an_ungrouped_container(client):
    fid, _ = db.upsert_finding("logs", "finding-ungrouped-a", "OOM", "crash", "critical", "desc")
    resp = client.get(f"/findings/{fid}")
    assert "Back to Stack" not in resp.text


def test_service_page_shows_back_to_stack_link_for_a_stack_member(client):
    compose_file = _compose_file("service-stack-link.yml", "service-stack-svc-a", "service-stack-svc-b")
    try:
        db.upsert_finding("logs", "service-stack-svc-a", "OOM", "crash", "critical", "desc")
        db.upsert_finding("logs", "service-stack-svc-a", "Disk full", "resource", "warning", "desc2")
        stack_id = _stack_id_for("service-stack-svc-a")

        resp = client.get("/logs/container/service-stack-svc-a")
        assert f'/logs/stack?id={stack_id}' in resp.text
        assert "Back to Stack" in resp.text
    finally:
        compose_file.unlink()


def test_service_page_has_no_back_to_stack_link_for_an_ungrouped_container(client):
    db.upsert_finding("logs", "service-ungrouped-a", "OOM", "crash", "critical", "desc")
    db.upsert_finding("logs", "service-ungrouped-a", "Disk full", "resource", "warning", "desc2")
    resp = client.get("/logs/container/service-ungrouped-a")
    assert "Back to Stack" not in resp.text


# ---------------------------------------------------------------------------
# Issues table row highlight -- matches the subtle green "row-unread" background the Updates
# page's pending-update rows already have.
# ---------------------------------------------------------------------------

def test_issues_table_rows_get_the_same_green_highlight_as_updates_pending_rows(client):
    db.upsert_finding("logs", "highlight-active-a", "OOM", "crash", "critical", "desc")
    db.set_log_watch_checkpoint("highlight-active-a")

    resp = client.get("/logs")
    section = resp.text[resp.text.index('id="logs-issues-table"'):]
    tr_start = section.rindex("<tr", 0, section.index("highlight-active-a"))
    tr_row = section[tr_start:section.index("</tr>", tr_start)]
    assert "row-unread" in tr_row


def test_issues_table_silenced_rows_do_not_get_the_green_highlight(client):
    fid, _ = db.upsert_finding("logs", "highlight-silenced-a", "OOM", "crash", "critical", "desc")
    db.set_finding_status(fid, "silenced")
    db.set_log_watch_checkpoint("highlight-silenced-a")

    resp = client.get("/logs?show_silenced=1")
    section = resp.text[resp.text.index('id="logs-issues-table"'):]
    tr_start = section.rindex("<tr", 0, section.index("highlight-silenced-a"))
    tr_row = section[tr_start:section.index("</tr>", tr_start)]
    assert "row-unread" not in tr_row


# ---------------------------------------------------------------------------
# Check-failure surfacing -- a real-world report: a container whose logs couldn't be fetched
# was silently invisible everywhere (no error count, no indicator, no way to notice it had
# stopped being checked at all), unlike Updates' registry-check failures which get a visible
# warning icon, a dedicated error status, and an opt-in notification.
# ---------------------------------------------------------------------------

def test_record_and_clear_log_check_errors_round_trip():
    db.record_log_check_errors({"err-round-trip-a": "connection refused"})
    states = {r["name"]: r for r in db.all_log_watch_states_with_status()}
    assert states["err-round-trip-a"]["status"] == "error"
    assert states["err-round-trip-a"]["error"] == "connection refused"

    db.clear_log_check_errors(["err-round-trip-a"])
    states = {r["name"]: r for r in db.all_log_watch_states_with_status()}
    assert "err-round-trip-a" not in states


def test_a_container_that_has_never_once_succeeded_still_shows_up_as_an_error():
    """The core of the bug: a container with zero log_watch_state rows (never successfully
    checked) used to be completely invisible, no matter how many times its check failed."""
    db.record_log_check_errors({"err-never-succeeded": "no such container"})
    states = {r["name"]: r for r in db.all_log_watch_states_with_status()}
    assert states["err-never-succeeded"]["status"] == "error"
    assert states["err-never-succeeded"]["last_at"] is not None  # last_error_at fallback


def test_error_status_wins_over_issue_or_healthy_regardless_of_findings():
    db.upsert_finding("logs", "err-wins-a", "OOM", "crash", "critical", "desc")
    db.set_log_watch_checkpoint("err-wins-a")
    db.record_log_check_errors({"err-wins-a": "timeout"})

    states = {r["name"]: r for r in db.all_log_watch_states_with_status()}
    assert states["err-wins-a"]["status"] == "error"


def test_run_log_check_for_persists_an_error_and_counts_it():
    def _fake_logs(name, since, max_lines):
        if name == "run-check-fails":
            raise RuntimeError("boom")
        return None

    with patch("app.log_watcher.get_container_logs_since", side_effect=_fake_logs):
        result = log_watcher.run_log_check_for(["run-check-fails", "run-check-ok"])

    assert result["errors"] == 1
    states = {r["name"]: r for r in db.all_log_watch_states_with_status()}
    assert states["run-check-fails"]["status"] == "error"
    assert "boom" in states["run-check-fails"]["error"]
    assert states["run-check-ok"]["status"] == "healthy"


def test_run_log_check_for_clears_a_previously_recorded_error_on_next_success():
    db.record_log_check_errors({"recovers-a": "old failure"})

    with patch("app.log_watcher.get_container_logs_since", return_value=None):
        log_watcher.run_log_check_for(["recovers-a"])

    states = {r["name"]: r for r in db.all_log_watch_states_with_status()}
    assert states["recovers-a"]["status"] == "healthy"


def test_run_log_check_for_notifies_on_error_when_the_toggle_is_on():
    db.set_notifications_enabled(True)
    db.set_feature_notify_enabled("logs", True)
    db.set_notify_logs_include_errors(True)
    try:
        with patch("app.log_watcher.get_container_logs_since", side_effect=RuntimeError("nope")), \
             patch("app.log_watcher.notify_logs_check_errors") as mock_notify:
            log_watcher.run_log_check_for(["notify-err-a"])
        mock_notify.assert_called_once()
        args = mock_notify.call_args[0][0]
        assert args[0]["container_name"] == "notify-err-a"
    finally:
        db.set_notifications_enabled(False)
        db.set_feature_notify_enabled("logs", False)
        db.set_notify_logs_include_errors(False)


def test_notify_logs_check_errors_is_a_noop_when_the_toggle_is_off(client):
    from app.notifications import notify_logs_check_errors
    db.set_notify_logs_include_errors(False)
    with patch("app.notifications._send") as mock_send:
        notify_logs_check_errors([{"container_name": "x", "error": "y"}])
    mock_send.assert_not_called()


def test_all_containers_table_shows_the_warning_icon_for_an_errored_container(client):
    db.record_log_check_errors({"table-error-a": "connection refused"})

    resp = client.get("/logs")
    section = resp.text[resp.text.index('id="logs-containers-table"'):]
    row = section[section.index("table-error-a"):]
    row = row[:row.index("</tr>")]
    assert "status-warning-icon" in row
    assert "cell-centered" in row
    assert "connection refused" in row


def test_all_containers_table_status_sort_ranks_errors_above_issues_and_healthy(client):
    db.record_log_check_errors({"sort-err-a": "boom"})
    db.upsert_finding("logs", "sort-issue-a", "OOM", "crash", "critical", "desc")
    db.set_log_watch_checkpoint("sort-issue-a")
    db.set_log_watch_checkpoint("sort-healthy-a")

    resp = client.get("/logs?csort=status&cdir=asc")
    section = resp.text[resp.text.index('id="logs-containers-table"'):]
    err_pos = section.index("sort-err-a")
    issue_pos = section.index("sort-issue-a")
    healthy_pos = section.index("sort-healthy-a")
    assert err_pos < issue_pos < healthy_pos


def test_reset_and_recheck_clears_log_check_errors():
    db.record_log_check_errors({"reset-clears-err-a": "boom"})
    db.reset_logs_data(subjects=["reset-clears-err-a"])
    states = {r["name"]: r for r in db.all_log_watch_states_with_status()}
    assert "reset-clears-err-a" not in states


def test_settings_notify_logs_include_errors_round_trip():
    db.set_notify_logs_include_errors(True)
    assert db.get_notify_logs_include_errors() is True
    db.set_notify_logs_include_errors(False)
    assert db.get_notify_logs_include_errors() is False


def test_settings_page_has_the_notify_logs_include_errors_toggle(client):
    resp = client.get("/settings")
    assert "logs can" in resp.text and "t be fetched" in resp.text  # apostrophe is HTML-escaped
    assert "/settings/notify/logs-include-errors" in resp.text


def test_settings_notify_logs_include_errors_route_saves(client):
    resp = client.post("/settings/notify/logs-include-errors", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_notify_logs_include_errors() is True
    db.set_notify_logs_include_errors(False)


# ---------------------------------------------------------------------------
# Mutex-busy ("a check just started elsewhere") coverage for every scoped Logs action route --
# only the global bulk-regenerate route had this before.
# ---------------------------------------------------------------------------

def test_logs_stack_check_now_is_a_noop_when_the_mutex_is_already_held(client):
    compose_file = _compose_file("busy-stack-checknow.yml", "busy-checknow-a", "busy-checknow-b")
    try:
        db.set_log_watch_checkpoint("busy-checknow-a")
        db.set_log_watch_checkpoint("busy-checknow-b")
        stack_id = _stack_id_for("busy-checknow-a")

        check_state.set_running("logs")
        try:
            with patch("app.log_watcher.run_log_check_for") as mock_check:
                resp = client.post("/logs/stack/check-now", params={"stack_id": stack_id})
            mock_check.assert_not_called()
            assert resp.status_code == 200
            assert "started elsewhere" in resp.text
        finally:
            check_state.release_running("logs")
    finally:
        compose_file.unlink()


def test_logs_stack_reset_and_recheck_is_a_noop_when_the_mutex_is_already_held(client):
    compose_file = _compose_file("busy-stack-reset.yml", "busy-reset-a", "busy-reset-b")
    try:
        db.set_log_watch_checkpoint("busy-reset-a")
        db.set_log_watch_checkpoint("busy-reset-b")
        stack_id = _stack_id_for("busy-reset-a")

        check_state.set_running("logs")
        try:
            with patch("app.log_watcher.run_log_check_for") as mock_check:
                resp = client.post("/logs/stack/reset-and-recheck", params={"stack_id": stack_id})
            mock_check.assert_not_called()
            assert resp.status_code == 200
            assert "started elsewhere" in resp.text
        finally:
            check_state.release_running("logs")
    finally:
        compose_file.unlink()


def test_logs_stack_retry_is_a_noop_when_the_mutex_is_already_held(client):
    compose_file = _compose_file("busy-stack-retry.yml", "busy-retry-a", "busy-retry-b")
    try:
        db.set_log_watch_checkpoint("busy-retry-a")
        db.set_log_watch_checkpoint("busy-retry-b")
        stack_id = _stack_id_for("busy-retry-a")

        check_state.set_running("logs")
        try:
            with patch("app.stacks.regenerate_log_stack_analysis") as mock_regen:
                resp = client.post("/logs/stack/retry", params={"stack_id": stack_id})
            mock_regen.assert_not_called()
            assert resp.status_code == 200
            assert "started elsewhere" in resp.text
        finally:
            check_state.release_running("logs")
    finally:
        compose_file.unlink()


def test_service_check_now_is_a_noop_when_the_mutex_is_already_held(client):
    check_state.set_running("logs")
    try:
        with patch("app.log_watcher.run_log_check_for") as mock_check:
            resp = client.post("/logs/container/busy-service-checknow/check-now")
        mock_check.assert_not_called()
        assert resp.status_code == 200
        assert "started elsewhere" in resp.text
    finally:
        check_state.release_running("logs")


def test_service_regenerate_is_a_noop_when_the_mutex_is_already_held(client):
    db.upsert_finding("logs", "busy-service-regen", "OOM", "crash", "critical", "desc1")
    db.upsert_finding("logs", "busy-service-regen", "Disk full", "resource", "warning", "desc2")

    check_state.set_running("logs")
    try:
        with patch("app.main.summarize_findings_overview") as mock_overview:
            resp = client.post("/logs/container/busy-service-regen/regenerate")
        mock_overview.assert_not_called()
        assert resp.status_code == 200
        assert "started elsewhere" in resp.text
    finally:
        check_state.release_running("logs")


def test_service_reset_and_recheck_is_a_noop_when_the_mutex_is_already_held(client):
    check_state.set_running("logs")
    try:
        with patch("app.log_watcher.run_log_check_for") as mock_check:
            resp = client.post("/logs/container/busy-service-reset/reset-and-recheck")
        mock_check.assert_not_called()
        assert resp.status_code == 200
        assert "started elsewhere" in resp.text
    finally:
        check_state.release_running("logs")


# ---------------------------------------------------------------------------
# run_log_check() -- the full-check wrapper itself, not just run_log_check_for. Every other
# connection-count/progress test in this file exercises run_log_check_for directly; this is the
# function real scheduled/manual full checks actually call.
# ---------------------------------------------------------------------------

def test_run_log_check_reports_progress_on_the_logs_feature_channel():
    with patch("app.log_watcher.list_running_containers_for_logs", return_value=[]):
        result = log_watcher.run_log_check()
    assert result == {"checked": 0, "findings_found": 0, "errors": 0}
    assert check_state.get_state("logs")["running"] is False


def test_run_log_check_uses_a_fixed_number_of_connections_end_to_end():
    """Same guarantee as run_log_check_for's own connection-count test, but through the real
    entry point (run_log_check), which also calls list_running_containers_for_logs (mocked
    here -- a real Docker call), _run_log_stack_analysis_pass_safely, and set_finished (its own
    connection to persist last_check_result) -- proving the full path stays cheap, not just the
    inner helper."""
    from app.docker_client import TrackedContainer

    containers = [
        TrackedContainer(name=f"full-check-{i}", image_repo=f"owner/x{i}", tag="latest", current_digest=None, labels={})
        for i in range(10)
    ]
    original_connect = sqlite3.connect
    connect_calls = []

    def counting_connect(*args, **kwargs):
        connect_calls.append(1)
        return original_connect(*args, **kwargs)

    with patch("app.log_watcher.list_running_containers_for_logs", return_value=containers), \
         patch("app.log_watcher.get_container_logs_since", return_value=None), \
         patch("app.db.sqlite3.connect", side_effect=counting_connect):
        result = log_watcher.run_log_check()

    # 3 for run_log_check_for's own batch (checkpoint read/write + error-clear) + 1 for
    # get_cross_service_analysis_enabled (the stack-analysis-pass gate) + 1 for set_finished's
    # persisted last_check_result -- a small fixed number regardless of container count, not
    # one connection per container.
    assert len(connect_calls) <= 6, f"expected a small fixed connection count, got {len(connect_calls)}"
    assert result["checked"] == 10


# ---------------------------------------------------------------------------
# Silenced -> Partially Silenced demotion when a new finding appears -- the core invariant the
# whole cascading-silence design depends on, self-tested live via a dev server earlier; this
# locks it in as a real regression test.
# ---------------------------------------------------------------------------

def test_service_badge_demotes_from_silenced_to_partially_silenced_when_a_new_finding_appears(client):
    fid1, _ = db.upsert_finding("logs", "demote-service-a", "OOM", "crash", "critical", "desc")
    db.set_finding_status(fid1, "silenced")
    fid2, _ = db.upsert_finding("logs", "demote-service-a", "Disk full", "resource", "warning", "desc2")
    db.set_finding_status(fid2, "silenced")

    resp = client.get("/logs/container/demote-service-a")
    assert "badge-lg badge-silenced\">Silenced</span>" in resp.text

    db.upsert_finding("logs", "demote-service-a", "New issue", "reliability", "warning", "desc3")

    resp = client.get("/logs/container/demote-service-a")
    assert "badge-lg badge-partially-silenced\">Partially Silenced</span>" in resp.text


def test_stack_badge_demotes_from_silenced_to_partially_silenced_when_a_new_finding_appears(client):
    compose_file = _compose_file("demote-stack.yml", "demote-stack-svc-a", "demote-stack-svc-b")
    try:
        fid1, _ = db.upsert_finding("logs", "demote-stack-svc-a", "OOM", "crash", "critical", "desc")
        db.set_finding_status(fid1, "silenced")
        db.set_log_watch_checkpoint("demote-stack-svc-a")
        db.set_log_watch_checkpoint("demote-stack-svc-b")
        stack_id = _stack_id_for("demote-stack-svc-a")

        resp = client.get(f"/logs/stack?id={stack_id}")
        assert "badge-lg badge-silenced\">Silenced</span>" in resp.text

        db.upsert_finding("logs", "demote-stack-svc-a", "New issue", "reliability", "warning", "desc2")

        resp = client.get(f"/logs/stack?id={stack_id}")
        assert "badge-lg badge-partially-silenced\">Partially Silenced</span>" in resp.text
    finally:
        compose_file.unlink()


# ---------------------------------------------------------------------------
# Compose's bottom "All files" table also got the 3-state silence badge (all_compose_file_
# states_with_status), tested for Logs' equivalent table but never for Compose's own.
# ---------------------------------------------------------------------------

def test_compose_all_files_table_shows_partially_silenced_badge(client):
    fid1, _ = db.upsert_finding("compose", "compose-partial-table.yml", "Missing restart policy", "reliability", "critical", "d1")
    db.upsert_finding("compose", "compose-partial-table.yml", "No healthcheck", "reliability", "warning", "d2")
    db.set_finding_status(fid1, "silenced")
    db.set_compose_file_hash("compose-partial-table.yml", "hash1")

    try:
        resp = client.get("/compose")
        section = resp.text[resp.text.index('id="compose-files-table"'):]
        row = section[section.index("compose-partial-table.yml"):]
        row = row[:row.index("</tr>")]
        assert "badge-partially-silenced\">Partially Silenced</span>" in row
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = 'compose-partial-table.yml'")


# ---------------------------------------------------------------------------
# A dedicated, sortable Silenced column on the per-subject findings table
# (subject_findings.html), replacing the old inline badge next to the title
# that wrapped onto a second line on narrow viewports.
# ---------------------------------------------------------------------------

def test_subject_findings_table_has_a_dedicated_silenced_column(client):
    fid1, _ = db.upsert_finding("logs", "silenced-col-test", "OOM", "crash", "critical", "d1")
    db.upsert_finding("logs", "silenced-col-test", "Disk full", "resource", "warning", "d2")
    db.set_finding_status(fid1, "silenced")

    resp = client.get("/logs/container/silenced-col-test")
    table = resp.text[resp.text.index("findings-table"):]
    header = table[:table.index("<tbody>")]
    body = table[table.index("<tbody>"):]

    # Header is a real sort link, not a bare <th>Silenced</th>.
    assert "sort=silenced" in header

    # The title cell no longer carries an inline Silenced badge next to it (the old
    # two-row-wrap bug) -- the badge now only appears in its own column.
    title_row = body[body.index("OOM"):]
    title_cell = title_row[:title_row.index("</td>")]
    assert "badge-silenced" not in title_cell

    assert body.count("badge-silenced\">Silenced</span>") == 1
    assert "<span class=\"meta\">—</span>" in body


def test_subject_findings_table_sorts_by_silenced_column(client):
    fid1, _ = db.upsert_finding("logs", "silenced-col-sort", "Active issue", "crash", "critical", "d1")
    fid2, _ = db.upsert_finding("logs", "silenced-col-sort", "Silenced issue", "resource", "warning", "d2")
    db.set_finding_status(fid2, "silenced")

    resp = client.get("/logs/container/silenced-col-sort?sort=silenced&dir=asc")
    body = resp.text[resp.text.index("<tbody>"):]
    assert body.index("Silenced issue") < body.index("Active issue")

    resp = client.get("/logs/container/silenced-col-sort?sort=silenced&dir=desc")
    body = resp.text[resp.text.index("<tbody>"):]
    assert body.index("Active issue") < body.index("Silenced issue")


def test_compose_file_findings_table_sort_links_preserve_the_path_query_param(client):
    fid1, _ = db.upsert_finding("compose", "silenced-sort-compose.yml", "Missing restart policy", "reliability", "critical", "d1")
    db.upsert_finding("compose", "silenced-sort-compose.yml", "No healthcheck", "reliability", "warning", "d2")
    db.set_finding_status(fid1, "silenced")

    try:
        resp = client.get("/compose/file?path=silenced-sort-compose.yml")
        assert "sort=silenced" in resp.text
        assert "path=silenced-sort-compose.yml" in resp.text

        resp = client.get("/compose/file?path=silenced-sort-compose.yml&sort=silenced&dir=asc")
        body = resp.text[resp.text.index("<tbody>"):]
        assert body.index("Missing restart policy") < body.index("No healthcheck")
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = 'silenced-sort-compose.yml'")


# ---------------------------------------------------------------------------
# The Logs stack detail members table is sortable too, same as every other findings table.
# ---------------------------------------------------------------------------

def test_logs_stack_detail_members_table_is_sortable_and_defaults_to_severity(client):
    compose_file = _compose_file("stack-sortable.yml", "sortable-svc-a", "sortable-svc-b")
    try:
        db.set_log_watch_checkpoint("sortable-svc-a")
        db.set_log_watch_checkpoint("sortable-svc-b")
        db.upsert_finding("logs", "sortable-svc-a", "minor issue", "reliability", "warning", "d1")
        db.upsert_finding("logs", "sortable-svc-b", "big crash", "crash", "critical", "d2")
        stack_id = _stack_id_for("sortable-svc-a")

        resp = client.get(f"/logs/stack?id={stack_id}")
        table = resp.text[resp.text.index("findings-table"):]
        header = table[:table.index("<tbody>")]
        assert "sort-link" in header
        # Default (no sort params) is severity-first, most severe on top -- same convention as
        # the Issues table and every other findings table in the app.
        body = table[table.index("<tbody>"):]
        assert body.index("sortable-svc-b") < body.index("sortable-svc-a")

        # Explicit sort by container name works too.
        resp = client.get(f"/logs/stack?id={stack_id}&sort=container&dir=asc")
        body = resp.text[resp.text.index("<tbody>"):]
        assert body.index("sortable-svc-a") < body.index("sortable-svc-b")
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE subject IN ('sortable-svc-a', 'sortable-svc-b')")
            conn.execute("DELETE FROM log_watch_state WHERE container_name IN ('sortable-svc-a', 'sortable-svc-b')")


def test_logs_stack_detail_sort_links_preserve_the_id_query_param(client):
    compose_file = _compose_file("stack-sort-qs.yml", "sort-qs-svc-a", "sort-qs-svc-b")
    try:
        db.set_log_watch_checkpoint("sort-qs-svc-a")
        db.set_log_watch_checkpoint("sort-qs-svc-b")
        stack_id = _stack_id_for("sort-qs-svc-a")

        resp = client.get(f"/logs/stack?id={stack_id}")
        assert f"id={stack_id}" in resp.text
        assert "sort=container" in resp.text
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM log_watch_state WHERE container_name IN ('sort-qs-svc-a', 'sort-qs-svc-b')")

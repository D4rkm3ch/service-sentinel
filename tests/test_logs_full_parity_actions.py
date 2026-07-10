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
    db.set_cross_service_analysis_enabled("logs", False)
    yield
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
        conn.execute("DELETE FROM log_watch_state")
        conn.execute("DELETE FROM findings")
        conn.execute("DELETE FROM subject_summaries")
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
    set_log_watch_checkpoint called inside the per-container loop). Batched now -- exactly 2
    connections (one read, one write) for the whole pass, regardless of container count."""
    names = [f"conn-count-{i}" for i in range(15)]
    original_connect = sqlite3.connect
    connect_calls = []

    def counting_connect(*args, **kwargs):
        connect_calls.append(1)
        return original_connect(*args, **kwargs)

    with patch("app.log_watcher.get_container_logs_since", return_value=None), \
         patch("app.db.sqlite3.connect", side_effect=counting_connect):
        result = log_watcher.run_log_check_for(names)

    assert connect_calls == [1, 1], f"expected a fixed 2-connection batch, got {len(connect_calls)}"
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
    assert header.index("Last seen") < header.index("Severity")


def test_logs_stack_detail_severity_column_is_right_of_last_seen(client):
    compose_file = _compose_file("stack-sev-order.yml", "sev-order-svc-a", "sev-order-svc-b")
    try:
        db.set_log_watch_checkpoint("sev-order-svc-a")
        db.set_log_watch_checkpoint("sev-order-svc-b")
        stack_id = _stack_id_for("sev-order-svc-a")

        resp = client.get(f"/logs/stack?id={stack_id}")
        table = resp.text[resp.text.index("findings-table"):]
        header = table[:table.index("<tbody>")]
        assert header.index(">Last seen<") < header.index(">Severity<")
    finally:
        compose_file.unlink()


def test_service_findings_table_severity_column_is_right_of_seen(client):
    db.upsert_finding("logs", "service-sev-order", "OOM", "crash", "critical", "desc1")
    db.upsert_finding("logs", "service-sev-order", "Disk full", "resource", "warning", "desc2")

    resp = client.get("/logs/container/service-sev-order")
    table = resp.text[resp.text.index("findings-table"):]
    header = table[:table.index("<tbody>")]
    assert header.index(">Seen<") < header.index(">Severity<")


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

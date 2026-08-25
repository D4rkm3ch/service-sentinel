"""Staleness (Settings -> Timing & Delivery): a finding whose last_seen_at hasn't moved in the
configured number of days gets flagged "Stale" -- last_seen_at already means "the last time this
was actually re-confirmed" for both sources (Logs only bumps it on fresh AI-confirmed evidence,
Compose only bumps it when the file is actually re-reviewed at all), so this needed no new
tracking, just a threshold and a render check. Covers the db layer (get/set/cutoff/count/
silence), the is_stale Jinja filter and its wiring into the badge on three pages, the Settings
control, and the Issues table's own "Silence N Stale" bulk action."""

from datetime import datetime, timedelta, timezone

from app import db

db.init_db()


def _backdate(finding_id: int, days: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.get_conn() as conn:
        conn.execute("UPDATE findings SET last_seen_at = ? WHERE id = ?", (ts, finding_id))


def setup_function(_):
    db.set_stale_after("14")


def teardown_function(_):
    db.set_stale_after("14")


# ---------------------------------------------------------------------------
# db layer
# ---------------------------------------------------------------------------

def test_stale_after_defaults_to_14_days():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'stale_after_days'")
    assert db.get_stale_after() == "14"
    assert db.get_stale_after_days() == 14


def test_set_and_get_stale_after():
    db.set_stale_after("30")
    assert db.get_stale_after() == "30"
    assert db.get_stale_after_days() == 30


def test_off_maps_to_no_threshold():
    db.set_stale_after("off")
    assert db.get_stale_after_days() is None


def test_cutoff_is_none_when_off():
    db.set_stale_after("off")
    assert db.get_stale_cutoff_iso() is None


def test_cutoff_is_a_timestamp_roughly_n_days_ago():
    db.set_stale_after("7")
    cutoff = db.get_stale_cutoff_iso()
    assert cutoff is not None
    parsed = datetime.fromisoformat(cutoff)
    expected = datetime.now(timezone.utc) - timedelta(days=7)
    assert abs((parsed - expected).total_seconds()) < 5


def test_count_and_silence_stale_findings():
    db.set_stale_after("7")
    fresh_id, _ = db.upsert_finding("logs", "stale-db-test", "fresh one", "error", "warning", "d1")
    old_id, _ = db.upsert_finding("logs", "stale-db-test", "old one", "error", "warning", "d2")
    already_silenced_id, _ = db.upsert_finding("logs", "stale-db-test", "old silenced", "error", "warning", "d3")
    db.set_finding_status(already_silenced_id, "silenced")
    _backdate(old_id, 30)
    _backdate(already_silenced_id, 30)
    try:
        # Only the old ACTIVE one counts -- the fresh active one and the old silenced one don't.
        assert db.count_stale_findings("logs") == 1

        silenced_count = db.silence_stale_findings("logs")
        assert silenced_count == 1
        assert db.get_finding(old_id)["status"] == "silenced"
        assert db.get_finding(fresh_id)["status"] == "active"
        assert db.count_stale_findings("logs") == 0
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE subject = 'stale-db-test'")


def test_count_and_silence_are_scoped_by_source():
    db.set_stale_after("7")
    logs_id, _ = db.upsert_finding("logs", "stale-scope-test", "logs one", "error", "warning", "d1")
    compose_id, _ = db.upsert_finding("compose", "stale-scope-test.yml", "compose one", "reliability", "warning", "d2")
    _backdate(logs_id, 30)
    _backdate(compose_id, 30)
    try:
        assert db.count_stale_findings("logs") == 1
        assert db.count_stale_findings("compose") == 1
        db.silence_stale_findings("logs")
        assert db.get_finding(logs_id)["status"] == "silenced"
        assert db.get_finding(compose_id)["status"] == "active"
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE subject IN ('stale-scope-test', 'stale-scope-test.yml')")


def test_count_and_silence_return_zero_when_staleness_is_off():
    db.set_stale_after("7")
    old_id, _ = db.upsert_finding("logs", "stale-off-test", "old one", "error", "warning", "d1")
    _backdate(old_id, 30)
    try:
        db.set_stale_after("off")
        assert db.count_stale_findings("logs") == 0
        assert db.silence_stale_findings("logs") == 0
        assert db.get_finding(old_id)["status"] == "active"
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE subject = 'stale-off-test'")


# ---------------------------------------------------------------------------
# is_stale Jinja filter, wired through real pages
# ---------------------------------------------------------------------------

def test_finding_detail_page_shows_the_stale_badge_for_an_old_finding(client):
    db.set_stale_after("7")
    fid, _ = db.upsert_finding("logs", "stale-badge-detail-test", "old finding", "error", "warning", "d")
    _backdate(fid, 30)
    try:
        resp = client.get(f"/findings/{fid}")
        assert "badge-stale" in resp.text
        assert "Stale" in resp.text
    finally:
        db.set_finding_status(fid, "silenced")


def test_finding_detail_page_omits_the_stale_badge_for_a_fresh_finding(client):
    db.set_stale_after("7")
    fid, _ = db.upsert_finding("logs", "stale-badge-fresh-test", "fresh finding", "error", "warning", "d")
    try:
        resp = client.get(f"/findings/{fid}")
        assert "badge-stale" not in resp.text
    finally:
        db.set_finding_status(fid, "silenced")


def test_finding_detail_page_omits_the_stale_badge_when_staleness_is_off(client):
    fid, _ = db.upsert_finding("logs", "stale-badge-off-test", "old finding", "error", "warning", "d")
    _backdate(fid, 30)
    try:
        db.set_stale_after("off")
        resp = client.get(f"/findings/{fid}")
        assert "badge-stale" not in resp.text
    finally:
        db.set_finding_status(fid, "silenced")


def test_issues_table_shows_the_stale_badge_for_an_old_active_finding(client):
    db.set_stale_after("7")
    fid, _ = db.upsert_finding("logs", "stale-badge-table-test", "old finding", "error", "warning", "d")
    _backdate(fid, 30)
    try:
        resp = client.get("/logs")
        assert "badge-stale" in resp.text
    finally:
        db.set_finding_status(fid, "silenced")


def test_subject_findings_page_shows_the_stale_badge(client):
    db.set_stale_after("7")
    fid1, _ = db.upsert_finding("logs", "stale-badge-subject-test", "old finding", "error", "warning", "d1")
    fid2, _ = db.upsert_finding("logs", "stale-badge-subject-test", "fresh finding", "error", "warning", "d2")
    _backdate(fid1, 30)
    try:
        resp = client.get("/logs/container/stale-badge-subject-test")
        assert "badge-stale" in resp.text
    finally:
        db.set_finding_status(fid1, "silenced")
        db.set_finding_status(fid2, "silenced")


# ---------------------------------------------------------------------------
# Settings control
# ---------------------------------------------------------------------------

def test_settings_page_reflects_the_current_stale_after_value(client):
    db.set_stale_after("30")
    resp = client.get("/settings")
    assert '<option value="30" selected>' in resp.text


def test_save_stale_after_route_updates_the_setting(client):
    resp = client.post("/settings/stale-after", data={"stale_after_days": "30"})
    assert resp.status_code == 200
    assert db.get_stale_after() == "30"


def test_save_stale_after_route_rejects_an_unknown_value(client):
    resp = client.post("/settings/stale-after", data={"stale_after_days": "not-a-real-choice"})
    assert resp.status_code == 400
    assert db.get_stale_after() != "not-a-real-choice"


# ---------------------------------------------------------------------------
# Bulk "Silence N Stale" button
# ---------------------------------------------------------------------------

def test_logs_page_shows_the_silence_stale_button_when_something_is_stale(client):
    db.set_stale_after("7")
    fid, _ = db.upsert_finding("logs", "stale-bulk-btn-test", "old finding", "error", "warning", "d")
    _backdate(fid, 30)
    try:
        resp = client.get("/logs")
        assert "Silence 1 Stale" in resp.text
    finally:
        db.set_finding_status(fid, "silenced")


def test_logs_page_omits_the_button_when_nothing_is_stale(client):
    db.set_stale_after("7")
    resp = client.get("/logs")
    assert "Stale</button>" not in resp.text


def test_silence_stale_route_silences_and_returns_the_updated_table_and_button(client):
    db.set_stale_after("7")
    fid, _ = db.upsert_finding("logs", "stale-bulk-route-test", "old finding", "error", "warning", "d")
    _backdate(fid, 30)
    try:
        resp = client.post("/logs/silence-stale")
        assert resp.status_code == 200
        assert db.get_finding(fid)["status"] == "silenced"
        # The oob button companion should now report nothing left to silence.
        assert "Silence 1 Stale" not in resp.text
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE subject = 'stale-bulk-route-test'")


def test_silence_stale_route_is_scoped_to_its_own_source(client):
    db.set_stale_after("7")
    logs_id, _ = db.upsert_finding("logs", "stale-bulk-scope-test", "logs one", "error", "warning", "d1")
    compose_id, _ = db.upsert_finding("compose", "stale-bulk-scope-test.yml", "compose one", "reliability", "warning", "d2")
    _backdate(logs_id, 30)
    _backdate(compose_id, 30)
    try:
        client.post("/compose/silence-stale")
        assert db.get_finding(compose_id)["status"] == "silenced"
        assert db.get_finding(logs_id)["status"] == "active"
    finally:
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM findings WHERE subject IN ('stale-bulk-scope-test', 'stale-bulk-scope-test.yml')"
            )

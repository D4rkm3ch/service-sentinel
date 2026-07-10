"""Stage 5c: moves TZ from an env-var-only setting into the Settings UI (db.get_timezone() /
db.set_timezone()), seeded from the TZ env var on first boot so existing deployments keep
behaving the same until someone changes it from the page. Covers the DB round-trip, the
settings route (including rejecting an unrecognized zone name), that a save re-applies the
schedule immediately so a running job's times reinterpret in the new zone without a restart,
and that the footer's displayed timezone updates too (it's a Jinja global registered as a
callable specifically so it doesn't go stale after a change -- see main.py)."""

from unittest.mock import patch

import pytest

from app import db

db.init_db()


def _clear_stored_timezone():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'timezone'")


@pytest.fixture(autouse=True)
def clean_timezone():
    """All test files in this suite share one physical SQLite database (see conftest.py), so
    a "no timezone stored yet" test must not depend on execution order -- this guarantees a
    clean slate before and after every test in this file regardless of what ran before it."""
    _clear_stored_timezone()
    yield
    _clear_stored_timezone()


@pytest.fixture(autouse=True)
def updates_feature_enabled():
    """apply_schedules() only registers a feature's periodic job when its toggle is on (see
    scheduler.py) -- these tests are about timezone re-application, not that gate, so keep
    "updates" enabled regardless of what other test files leave it as. Logs/Compose are
    explicitly disabled too: 2+ enabled features sharing the master schedule now get grouped
    into one combined sequential job (see scheduler.py's apply_schedules), which would hide
    "updates" own periodic_updates_check job id that these tests assert on directly."""
    db.set_feature_enabled("updates", True)
    db.set_feature_enabled("logs", False)
    db.set_feature_enabled("compose", False)
    yield
    db.set_feature_enabled("updates", True)
    db.set_feature_enabled("logs", False)
    db.set_feature_enabled("compose", False)


def test_get_timezone_defaults_to_the_env_var_seed():
    with patch("app.db.settings.tz", "Australia/Sydney"):
        assert db.get_timezone() == "Australia/Sydney"


def test_set_and_get_timezone_round_trip():
    db.set_timezone("America/New_York")
    assert db.get_timezone() == "America/New_York"


def test_save_timezone_route_persists_and_reapplies_schedule(client):
    from app import scheduler

    resp = client.post("/settings/timezone", data={"timezone": "Australia/Sydney"})
    assert resp.status_code == 200
    assert db.get_timezone() == "Australia/Sydney"

    job = scheduler._scheduler.get_job("periodic_updates_check")
    assert str(job.trigger.timezone) == "Australia/Sydney"


def test_save_timezone_rejects_unrecognized_zone_name(client):
    resp = client.post("/settings/timezone", data={"timezone": "Not/A_Real_Zone"})
    assert resp.status_code == 400
    assert db.get_timezone() != "Not/A_Real_Zone"


def test_settings_page_renders_the_configured_timezone_as_selected(client):
    db.set_timezone("Europe/London")
    resp = client.get("/settings")
    assert 'value="Europe/London" selected' in resp.text


def test_footer_reflects_the_current_timezone_without_restart(client):
    db.set_timezone("Asia/Tokyo")
    resp = client.get("/")
    assert "Asia/Tokyo" in resp.text


# ---------------------------------------------------------------------------
# local_dt -- the Jinja filter every table/timestamp in the app routes through (see main.py).
# Regression coverage for a real-world report: every table's timestamps (Detected, Last
# checked, Last seen, Seen, ...) were displaying the raw stored UTC value untouched -- only
# check_state.format_summary's own separately hand-rolled "Last checked: ..." status line and
# the schedule actually respected the configured TZ.
# ---------------------------------------------------------------------------

def test_local_dt_converts_utc_to_a_fixed_offset_zone():
    from app.main import local_dt

    db.set_timezone("Asia/Tokyo")  # UTC+9, no DST -- deterministic year-round
    assert local_dt("2026-01-01T12:00:00+00:00") == "2026-01-01 21:00"


def test_local_dt_handles_a_missing_value():
    from app.main import local_dt

    assert local_dt(None) == "—"
    assert local_dt("") == "—"


def test_local_dt_falls_back_to_utc_for_an_unrecognized_zone():
    from app.main import local_dt

    db.set_timezone("Not/A_Real_Zone")
    assert local_dt("2026-01-01T12:00:00+00:00") == "2026-01-01 12:00"


def test_a_real_table_renders_timestamps_in_the_configured_timezone(client):
    """Integration-level check (not just the filter in isolation): the Updates 'Tracked
    containers' table's Last checked column must reflect the conversion, proving local_dt is
    actually wired into the template, not just defined."""
    from unittest.mock import patch

    db.set_timezone("Asia/Tokyo")
    try:
        with patch("app.db.now_iso", return_value="2026-03-01T03:00:00+00:00"):
            db.upsert_container_state("tz-table-check", "owner/tz-table-check", "latest", "sha256:old")

        resp = client.get("/updates")
        section = resp.text[resp.text.index('id="containers-table"'):]
        row = section[section.index("tz-table-check"):]
        row = row[:row.index("</tr>")]
        assert "2026-03-01 12:00" in row  # 03:00 UTC + 9h = 12:00 JST
        assert "03:00" not in row  # the raw UTC value must not leak through
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM container_state WHERE container_name = 'tz-table-check'")

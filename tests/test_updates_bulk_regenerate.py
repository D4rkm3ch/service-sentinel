"""An explicit ask: the per-item "Regenerate AI Response" button (see test_regenerate_route_
regenerates_the_summary_in_place_without_changing_the_id in test_updates_e2e.py) already
exists -- this puts the same action on the main Updates page, affecting every pending update
at once rather than just one, reusing the same claimed-mutex + fan-out pattern as the real
check itself (persist.run_claimed_bulk_regenerate)."""

import time
from unittest.mock import patch

from app import check_state, db


def _seed_pending_update(container_name: str, release_notes_raw: str | None = "Some raw notes"):
    db.upsert_container_state(container_name, f"owner/{container_name}", "latest", "sha256:new")
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name=container_name, image_repo=f"owner/{container_name}", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown="## Old\nStale.", source_url=None, release_notes_raw=release_notes_raw,
        )


def _cleanup(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))


def _wait_for_updates_check_to_finish():
    for _ in range(50):
        if not check_state.get_state("updates")["running"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("bulk regenerate never finished")


def test_button_appears_on_the_updates_page(client):
    resp = client.get("/updates")
    assert 'action="/updates/regenerate-all"' in resp.text
    assert "Regenerate AI Response" in resp.text


def test_button_does_not_appear_on_logs_or_compose_pages(client):
    logs = client.get("/logs")
    assert 'action="/updates/regenerate-all"' not in logs.text
    compose = client.get("/compose")
    assert 'action="/updates/regenerate-all"' not in compose.text


def test_bulk_regenerate_regenerates_every_pending_update_with_notes(client):
    _seed_pending_update("bulk-regen-a")
    _seed_pending_update("bulk-regen-b")
    _seed_pending_update("bulk-regen-none", release_notes_raw=None)  # nothing to regenerate from

    with patch("app.persist.summarize_update", return_value=("## Bug Fixes\nRegenerated.", "bugfix")):
        resp = client.post("/updates/regenerate-all")
        assert resp.status_code in (200, 303)
        _wait_for_updates_check_to_finish()

    a = db.get_latest_update_for_container("bulk-regen-a")
    b = db.get_latest_update_for_container("bulk-regen-b")
    none = db.get_latest_update_for_container("bulk-regen-none")
    assert a["summary_markdown"] == "## Bug Fixes\nRegenerated."
    assert b["summary_markdown"] == "## Bug Fixes\nRegenerated."
    assert none["summary_markdown"] == "## Old\nStale."  # untouched, no notes to regenerate from

    _cleanup("bulk-regen-a")
    _cleanup("bulk-regen-b")
    _cleanup("bulk-regen-none")


def test_bulk_regenerate_does_not_overwrite_the_last_checked_summary(client):
    """release_running (not set_finished) on completion -- this isn't itself a check, so the
    status badge's "Last checked" line must be left exactly as the last real check left it."""
    _seed_pending_update("bulk-regen-status")
    before = check_state.get_state("updates").get("last_result")

    with patch("app.persist.summarize_update", return_value=("## Notes", "bugfix")):
        client.post("/updates/regenerate-all")
        _wait_for_updates_check_to_finish()

    assert check_state.get_state("updates").get("last_result") == before

    _cleanup("bulk-regen-status")


def test_bulk_regenerate_refuses_to_start_while_a_check_is_already_running(client):
    assert check_state.get_state("updates")["running"] is False
    check_state.set_running("updates")
    try:
        with patch("app.persist.run_claimed_bulk_regenerate") as mocked:
            resp = client.post("/updates/regenerate-all")
            assert resp.status_code in (200, 303)
            mocked.assert_not_called()
    finally:
        check_state.release_running("updates")

"""An explicit ask: the per-item "Regenerate AI Response" button (see test_regenerate_route_
regenerates_the_summary_in_place_without_changing_the_id in test_updates_e2e.py) already
exists -- this puts the same action on the main Updates page, affecting every pending update
at once rather than just one, reusing the same claimed-mutex + fan-out pattern as the real
check itself (persist.run_claimed_bulk_regenerate)."""

import time
from pathlib import Path
from unittest.mock import patch

from app import check_state, compose_lookup, db
from app.config import settings


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


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _stack_id_for(container_name):
    return compose_lookup.match_container_to_stack(container_name, compose_lookup.build_stack_index())["stack_id"]


def test_bulk_regenerate_also_force_refreshes_every_qualifying_stacks_cross_service_blurb(client):
    """A real-world audit found the global Regenerate AI Response only touched per-update
    summaries, unlike Logs' equivalent, which also force-refreshes every qualifying stack's
    Cross-Service Analysis blurb. This locks in parity: an explicit "regenerate everything"
    click should mean everything AI-written on the Updates pages."""
    compose_file = _compose_file("bulk-regen-stack.yml", "bulk-regen-stack-a", "bulk-regen-stack-b")
    db.set_cross_service_analysis_enabled("updates", True)
    try:
        _seed_pending_update("bulk-regen-stack-a")
        _seed_pending_update("bulk-regen-stack-b")
        stack_id = _stack_id_for("bulk-regen-stack-a")

        db.set_stack_analysis(stack_id, "stale-hash", "Stale blurb.", source="updates")

        with patch("app.persist.summarize_update", return_value=("## Notes", "bugfix")), \
             patch("app.stacks.analyze_stack_impact", return_value="Fresh cross-service blurb.") as mocked:
            resp = client.post("/updates/regenerate-all")
            assert resp.status_code in (200, 303)
            _wait_for_updates_check_to_finish()

        mocked.assert_called_once()
        analysis = db.get_stack_analysis(stack_id, source="updates")
        assert analysis["analysis_markdown"] == "Fresh cross-service blurb."
        assert analysis["content_hash"] != "stale-hash"
    finally:
        db.set_cross_service_analysis_enabled("updates", False)
        compose_file.unlink()
        _cleanup("bulk-regen-stack-a")
        _cleanup("bulk-regen-stack-b")
        with db.get_conn() as conn:
            conn.execute("DELETE FROM stack_analyses WHERE stack_id = ?", (stack_id,))


def test_bulk_regenerate_does_not_touch_stack_analysis_when_toggle_is_off(client):
    compose_file = _compose_file("bulk-regen-stack-off.yml", "bulk-regen-off-a", "bulk-regen-off-b")
    db.set_cross_service_analysis_enabled("updates", False)
    try:
        _seed_pending_update("bulk-regen-off-a")
        _seed_pending_update("bulk-regen-off-b")

        with patch("app.persist.summarize_update", return_value=("## Notes", "bugfix")), \
             patch("app.stacks.analyze_stack_impact") as mocked:
            resp = client.post("/updates/regenerate-all")
            assert resp.status_code in (200, 303)
            _wait_for_updates_check_to_finish()

        mocked.assert_not_called()
    finally:
        compose_file.unlink()
        _cleanup("bulk-regen-off-a")
        _cleanup("bulk-regen-off-b")


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

"""An explicit ask: Updates never had a Deep Analysis equivalent to Logs/Compose's per-finding
suggested fix -- this adds one ("upgrade guidance"), off by default, its own toggle on Settings,
completely separate from the pre-existing stack-wide Cross-Service Analysis toggle (which used
to share the same underlying deep_analysis_updates_enabled key -- see the migration tests
below for why that had to change)."""

from unittest.mock import patch

from app import db, persist


def _cleanup(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))


def test_upgrade_guidance_defaults_to_off():
    assert db.get_deep_analysis_enabled("updates") is False


def test_summarize_container_does_not_generate_guidance_when_toggle_is_off():
    db.set_deep_analysis_enabled("updates", False)
    container = {"container_name": "guidance-off-test", "image_repo": "owner/x", "current_digest": "a", "latest_digest": "b"}
    with patch("app.persist.summarize_update", return_value=("Summary.", "bugfix")), \
         patch("app.persist.generate_upgrade_guidance") as mock_guidance:
        result = persist._summarize_container(container, "raw notes")
    assert result == ("Summary.", "bugfix", None)
    mock_guidance.assert_not_called()


def test_summarize_container_generates_guidance_when_toggle_is_on():
    db.set_deep_analysis_enabled("updates", True)
    try:
        container = {"container_name": "guidance-on-test", "image_repo": "owner/x", "current_digest": "a", "latest_digest": "b"}
        with patch("app.persist.summarize_update", return_value=("Summary.", "breaking")), \
             patch("app.persist.generate_upgrade_guidance", return_value="- Back up your data first.") as mock_guidance:
            result = persist._summarize_container(container, "raw notes")
        assert result == ("Summary.", "breaking", "- Back up your data first.")
        mock_guidance.assert_called_once()
    finally:
        db.set_deep_analysis_enabled("updates", False)


def test_a_failed_guidance_call_never_invalidates_an_otherwise_successful_summary():
    db.set_deep_analysis_enabled("updates", True)
    try:
        container = {"container_name": "guidance-fail-test", "image_repo": "owner/x", "current_digest": "a", "latest_digest": "b"}
        with patch("app.persist.summarize_update", return_value=("Summary.", "feature")), \
             patch("app.persist.generate_upgrade_guidance", side_effect=RuntimeError("provider down")):
            result = persist._summarize_container(container, "raw notes")
        assert result == ("Summary.", "feature", None)
    finally:
        db.set_deep_analysis_enabled("updates", False)


def test_upgrade_guidance_is_persisted_and_rendered_on_the_detail_page(client):
    db.set_deep_analysis_enabled("updates", True)
    try:
        db.upsert_container_state("guidance-e2e-test", "owner/guidance-e2e-test", "latest", "sha256:new")
        with patch("app.persist.release_notes.get_release_notes", return_value=("raw notes", "https://example.com")), \
             patch("app.persist.ai_provider.is_configured", return_value=True), \
             patch("app.persist.summarize_update", return_value=("Summary text.", "action_needed")), \
             patch("app.persist.generate_upgrade_guidance", return_value="- Set the new FOO env var first."):
            persist.persist_check_outcome({
                "containers": [{
                    "container_name": "guidance-e2e-test", "image_repo": "owner/guidance-e2e-test", "tag": "latest",
                    "status": "update_available", "current_digest": "sha256:old", "latest_digest": "sha256:new",
                }],
                "errors": 0, "checked_at": "2026-01-01T00:00:00+00:00",
            })

        update = db.get_latest_update_for_container("guidance-e2e-test")
        assert update["upgrade_guidance"] == "- Set the new FOO env var first."

        resp = client.get(f"/updates/{update['id']}")
        assert "Upgrade Guidance" in resp.text
        assert "Set the new FOO env var first" in resp.text
    finally:
        db.set_deep_analysis_enabled("updates", False)
        _cleanup("guidance-e2e-test")


def test_detail_page_shows_no_upgrade_guidance_section_when_toggle_is_off():
    db.set_deep_analysis_enabled("updates", False)
    db.upsert_container_state("guidance-off-e2e", "owner/guidance-off-e2e", "latest", "sha256:new")
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name="guidance-off-e2e", image_repo="owner/guidance-off-e2e", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown="A summary.", source_url=None, release_notes_raw="notes",
        )
    update = db.get_latest_update_for_container("guidance-off-e2e")

    from app.main import app
    from fastapi.testclient import TestClient
    resp = TestClient(app).get(f"/updates/{update['id']}")
    assert "Upgrade Guidance" not in resp.text

    _cleanup("guidance-off-e2e")


# ---------------------------------------------------------------------------
# Migration: deep_analysis_updates_enabled used to mean "stack-wide cross-service analysis" --
# renamed to cross_service_analysis_updates_enabled so it stops colliding with the new,
# genuinely different per-item toggle that now owns the old key name.
# ---------------------------------------------------------------------------

def test_migration_preserves_an_existing_installs_cross_service_setting_under_the_new_key():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key IN "
                     "('deep_analysis_updates_enabled', 'cross_service_analysis_updates_enabled', "
                     "'migrated_cross_service_updates_rename')")
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('deep_analysis_updates_enabled', 'true')"
        )

    db.init_db()  # re-run migrations against this simulated pre-migration state

    assert db.get_cross_service_analysis_enabled("updates") is True
    # The old key is reset to false -- it now means the new, unrelated per-item feature, which
    # must not inherit whatever the old cross-service value happened to be.
    assert db.get_deep_analysis_enabled("updates") is False

    # Idempotent: running init_db() again must NOT reset a since-legitimately-enabled per-item
    # toggle back to false.
    db.set_deep_analysis_enabled("updates", True)
    db.init_db()
    assert db.get_deep_analysis_enabled("updates") is True

    db.set_deep_analysis_enabled("updates", False)
    db.set_cross_service_analysis_enabled("updates", False)

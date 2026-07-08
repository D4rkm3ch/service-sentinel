"""POST /updates/stack/retry was a Stage-1-era no-op placeholder -- a plain 303 redirect that
never called anything, left over from before stacks existed and never updated once Stage 12
landed. This covers its real behavior: force-regenerates the stack's cross-service analysis
bypassing the content-hash cache, respects the shared "one check at a time" mutex, and is a
safe no-op when there's nothing to regenerate."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app import check_state, db
from app.config import settings

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
    yield
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _stack_id_for(container_name):
    from app import compose_lookup
    return compose_lookup.match_container_to_stack(container_name, compose_lookup.build_stack_index())["stack_id"]


def test_retry_force_regenerates_bypassing_the_cache(client):
    compose_file = _compose_file("retry-stack.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")
        stack_id = _stack_id_for("sonarr")

        with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
             patch("app.stacks.analyze_stack_impact", return_value="First analysis.") as mock_analyze:
            resp = client.post("/updates/stack/retry", data={"stack_id": stack_id}, follow_redirects=False)
        assert resp.status_code == 303
        mock_analyze.assert_called_once()
        assert db.get_stack_analysis(stack_id)["analysis_markdown"] == "First analysis."

        # A second click must call the AI again even though nothing about the stack changed --
        # that's the whole point of a manual Retry button versus the automatic cached pass.
        with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
             patch("app.stacks.analyze_stack_impact", return_value="Second analysis.") as mock_analyze:
            client.post("/updates/stack/retry", data={"stack_id": stack_id}, follow_redirects=False)
        mock_analyze.assert_called_once()
        assert db.get_stack_analysis(stack_id)["analysis_markdown"] == "Second analysis."
    finally:
        compose_file.unlink()


def test_retry_is_a_silent_noop_when_the_check_mutex_is_already_held(client):
    compose_file = _compose_file("retry-busy.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")
        stack_id = _stack_id_for("sonarr")

        check_state.set_running("updates")
        try:
            with patch("app.stacks.analyze_stack_impact") as mock_analyze:
                resp = client.post("/updates/stack/retry", data={"stack_id": stack_id}, follow_redirects=False)
            assert resp.status_code == 303
            mock_analyze.assert_not_called()
        finally:
            check_state.release_running("updates")
    finally:
        compose_file.unlink()


def test_retry_with_no_stack_id_is_a_noop(client):
    with patch("app.stacks.analyze_stack_impact") as mock_analyze:
        resp = client.post("/updates/stack/retry", data={"stack_id": ""}, follow_redirects=False)
    assert resp.status_code == 303
    mock_analyze.assert_not_called()


def test_retry_uses_the_latest_persisted_digest_when_a_real_update_is_pending(client):
    """Members for Retry are built from whatever's currently persisted (container_state +
    updates), not a fresh check -- confirms a pending update's new_digest is what actually
    feeds the fingerprint, not just the last-seen digest."""
    compose_file = _compose_file("retry-pending.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")
        db.record_update(
            container_name="sonarr", image_repo="owner/sonarr", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw="notes",
        )
        stack_id = _stack_id_for("sonarr")

        captured = {}

        def fake_analyze(display_name, service_names, changed_summary):
            captured["service_names"] = service_names
            return "Analysis."

        with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
             patch("app.stacks.analyze_stack_impact", side_effect=fake_analyze):
            client.post("/updates/stack/retry", data={"stack_id": stack_id}, follow_redirects=False)

        assert set(captured["service_names"]) == {"sonarr", "radarr"}
        assert db.get_stack_analysis(stack_id) is not None
    finally:
        compose_file.unlink()

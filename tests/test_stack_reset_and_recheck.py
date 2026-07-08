"""The stack-level "Reset & re-check" button (POST /updates/stack/reset-and-recheck) was a
Stage-1-era no-op placeholder -- a plain 303 redirect that never touched anything. This covers
its real behavior: scoped to exactly the stack's own member containers (never the rest of the
fleet -- the scenario a "major issue" report this session worried had actually happened at the
per-item level, though it turned out that button was fine), wiping and re-fetching just those,
respecting the shared "one check at a time" mutex, and a safe no-op when there's nothing to do
or the mutex is already held."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app import check_state, db
from app.config import settings
from app.docker_client import TrackedContainer

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    yield
    db.reset_updates_data()


@pytest.fixture(autouse=True)
def no_real_release_notes_fetch():
    with patch("app.persist.release_notes.get_release_notes", return_value=("Fresh notes", "https://example.com")):
        yield


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _fake_containers():
    return [
        TrackedContainer(name="sonarr", image_repo="owner/sonarr", tag="latest", current_digest="sha256:old", labels={}),
        TrackedContainer(name="radarr", image_repo="owner/radarr", tag="latest", current_digest="sha256:old", labels={}),
        TrackedContainer(name="unrelated", image_repo="owner/unrelated", tag="latest", current_digest="sha256:old", labels={}),
    ]


def _fake_digest(repo, tag):
    return "sha256:new"  # every tracked container gets a fresh digest -> update_available


def test_stack_reset_and_recheck_only_touches_its_own_members(client):
    compose_file = _compose_file("radar-stack.yml", "sonarr", "radarr")
    try:
        for name in ("sonarr", "radarr", "unrelated"):
            db.upsert_container_state(name, f"owner/{name}", "latest", "sha256:old")

        index_resp = client.get("/updates")  # populate stack index lazily isn't needed, but sanity-check page loads
        assert index_resp.status_code == 200

        from app import compose_lookup
        stack_id = compose_lookup.match_container_to_stack("sonarr", compose_lookup.build_stack_index())["stack_id"]

        with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
             patch("app.reconcile.get_latest_digest", side_effect=_fake_digest):
            resp = client.post("/updates/stack/reset-and-recheck", data={"stack_id": stack_id}, follow_redirects=False)

        assert resp.status_code == 303
        rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
        assert rows["sonarr"]["status"] == "update_available"
        assert rows["radarr"]["status"] == "update_available"
        # The unrelated container was never in this stack -- must be untouched (still up_to_date/no row).
        assert rows["unrelated"]["status"] == "up_to_date"
    finally:
        compose_file.unlink()


def test_stack_reset_and_recheck_forces_a_fresh_fetch_even_on_unchanged_digest():
    compose_file = _compose_file("radar-stack2.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")
        db.record_update(
            container_name="sonarr", image_repo="owner/sonarr", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw="Stale notes",
        )
        first_id = db.get_latest_update_for_container("sonarr")["id"]

        from app import persist

        with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
             patch("app.reconcile.get_latest_digest", side_effect=_fake_digest), \
             patch("app.persist.release_notes.get_release_notes", return_value=("Brand new notes", "https://example.com")) as mock_fetch:
            persist.run_and_persist_many_reset_and_check(["sonarr", "radarr"])

        assert mock_fetch.call_count == 2  # both members re-fetched, digest change or not
        updated = db.get_latest_update_for_container("sonarr")
        assert updated["id"] != first_id
        assert updated["release_notes_raw"] == "Brand new notes"
    finally:
        compose_file.unlink()


def test_stack_reset_and_recheck_is_a_noop_when_the_mutex_is_already_held(client):
    compose_file = _compose_file("radar-stack3.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")

        from app import compose_lookup
        stack_id = compose_lookup.match_container_to_stack("sonarr", compose_lookup.build_stack_index())["stack_id"]

        check_state.set_running("updates")
        try:
            with patch("app.reconcile.list_tracked_containers") as mock_list:
                resp = client.post("/updates/stack/reset-and-recheck", data={"stack_id": stack_id}, follow_redirects=False)
            mock_list.assert_not_called()
            assert resp.status_code == 303
        finally:
            check_state.release_running("updates")
    finally:
        compose_file.unlink()


def test_stack_reset_and_recheck_with_unknown_stack_id_is_a_safe_noop(client):
    resp = client.post("/updates/stack/reset-and-recheck", data={"stack_id": "nonexistent"}, follow_redirects=False)
    assert resp.status_code == 303
    assert not check_state.get_state("updates")["running"]

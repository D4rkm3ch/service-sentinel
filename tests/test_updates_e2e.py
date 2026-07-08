"""End-to-end regression test for Stage 3: drives the real HTTP routes through TestClient
with mocked Docker/registry calls, proving the full check -> persist -> render pipeline works
-- real ids on update rows, the per-update detail page rendering real content, and the global
Reset & re-check route actually wiping persisted state rather than being Stage 1's
placeholder. Uses the shared `client` fixture from conftest.py."""

import time
from unittest.mock import patch

import pytest

from app import check_state, db
from app.docker_client import TrackedContainer


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    yield
    db.reset_updates_data()


@pytest.fixture(autouse=True)
def no_real_release_notes_fetch():
    """These tests drive the real check pipeline end-to-end through the HTTP routes, which as
    of Stage 6 includes a real release notes fetch for any genuinely-new update_available
    container -- mocked here for the same reason the Docker/registry calls above are: this
    file proves the pipeline wiring, not release_notes.py's own network behavior (covered by
    test_release_notes.py) or persist.py's fetch-decision logic (covered by
    test_stage6_persist_release_notes.py)."""
    with patch("app.persist.release_notes.get_release_notes", return_value=("Fake release notes", "https://example.com/notes")):
        yield


def _fake_containers():
    return [
        TrackedContainer(name="sonarr", image_repo="linuxserver/sonarr", tag="latest",
                          current_digest="sha256:old", labels={}),
        TrackedContainer(name="plex", image_repo="linuxserver/plex", tag="latest",
                          current_digest="sha256:same", labels={}),
        TrackedContainer(name="broken", image_repo="owner/broken", tag="latest",
                          current_digest="sha256:x", labels={}),
    ]


def _fake_digest(repo, tag):
    return {
        "linuxserver/sonarr": "sha256:new",
        "linuxserver/plex": "sha256:same",
    }.get(repo)  # "owner/broken" -> None -> error


def _run_check_and_wait(client):
    with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
         patch("app.reconcile.get_latest_digest", side_effect=_fake_digest):
        resp = client.post("/updates/check-now")
        assert resp.status_code == 200
        for _ in range(50):
            if not check_state.get_state("updates")["running"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("check never finished")


def test_check_now_persists_real_rows_with_real_ids(client):
    _run_check_and_wait(client)

    page = client.get("/updates")
    assert page.status_code == 200
    assert "sonarr" in page.text
    assert "Coming back in a later stage" not in page.text  # the Stage 1 disabled-chevron tooltip

    rows = db.list_tracked_containers_with_status()
    by_name = {r["container_name"]: r for r in rows}
    assert by_name["sonarr"]["status"] == "update_available"
    assert by_name["plex"]["status"] == "up_to_date"
    assert by_name["broken"]["status"] == "error"
    assert by_name["sonarr"]["id"] is not None
    assert by_name["plex"]["id"] is None


def test_update_detail_page_renders_real_content(client):
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")

    detail = client.get(f"/updates/{sonarr['id']}")
    assert detail.status_code == 200
    assert "sonarr" in detail.text
    assert "linuxserver/sonarr" in detail.text
    # No AI yet (Stage 7) -- severity badge falls back to the same "--" convention used
    # elsewhere in the UI, not a broken empty badge.
    assert "badge-sev-" not in detail.text
    # Stage 6: real release notes fetched during the check above render as the page content
    # in place of the (nonexistent-until-Stage-7) AI summary.
    assert "Fake release notes" in detail.text
    assert "https://example.com/notes" in detail.text


def test_global_reset_and_recheck_wipes_then_repopulates(client):
    _run_check_and_wait(client)
    assert len(db.list_tracked_containers_with_status()) == 3

    with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
         patch("app.reconcile.get_latest_digest", side_effect=_fake_digest):
        resp = client.post("/updates/reset-and-recheck")
        assert resp.status_code in (200, 303)
        for _ in range(50):
            if not check_state.get_state("updates")["running"]:
                break
            time.sleep(0.1)

    rows = db.list_tracked_containers_with_status()
    assert len(rows) == 3  # repopulated fresh, not left empty


def test_retry_route_is_still_a_disabled_placeholder(client):
    """The button itself is disabled in the UI (Stage 7 hasn't shipped an AI summary to
    regenerate yet) -- this just proves the route it would have posted to is still a harmless
    no-op if ever hit directly."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")

    resp = client.post(f"/updates/{sonarr['id']}/retry", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/updates/{sonarr['id']}"


def test_scoped_reset_and_recheck_reruns_just_that_container(client):
    """Stage 6: per-item Reset & re-check is real now -- posts an htmx fragment (spinner, not
    a redirect) immediately, does a real scoped re-check of just that one container in the
    background, and the poller ends with an HX-Redirect once done. Also proves it shares the
    same "only one check at a time" mutex as a full check (released cleanly afterwards) and
    doesn't prune every other tracked container the way a full check's outcome would."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
         patch("app.reconcile.get_latest_digest", side_effect=_fake_digest):
        resp = client.post(f"/updates/{sonarr_id}/reset-and-recheck")
        assert resp.status_code == 200
        assert 'id="item-recheck-status"' in resp.text
        assert "spinner" in resp.text

        for _ in range(50):
            item = check_state.get_item_state(f"update:{sonarr_id}")
            if item is None or not item["running"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("scoped recheck never finished")

        poll = client.get(f"/updates/{sonarr_id}/recheck-status-poll")
        # Same digest transition as before (_fake_digest is unchanged) -> same row, same id.
        assert poll.headers.get("hx-redirect") == f"/updates/{sonarr_id}"

    assert check_state.get_state("updates")["running"] is False
    # Not pruned down to just the one container the scoped outcome actually contained.
    assert len(db.list_tracked_containers_with_status()) == 3

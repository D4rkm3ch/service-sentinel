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


def test_detail_page_has_check_now_and_reset_and_recheck_with_only_the_latter_confirmed(client):
    """Stage 6 polish: two distinct scoped actions now -- Check now (only replaces the row if
    the digest actually changed, no confirmation) and Reset & re-check (wipes this update's
    row first, forcing a fresh notes fetch even if nothing changed, confirmed like the
    destructive global button)."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")

    detail = client.get(f"/updates/{sonarr['id']}")
    assert "Check now" in detail.text
    assert "Reset &amp; re-check" in detail.text
    assert f'hx-post="/updates/{sonarr["id"]}/check-now"' in detail.text
    assert f'hx-post="/updates/{sonarr["id"]}/reset-and-recheck"' in detail.text

    check_now_pos = detail.text.index(f'/updates/{sonarr["id"]}/check-now')
    reset_pos = detail.text.index(f'/updates/{sonarr["id"]}/reset-and-recheck')
    # hx-confirm sits between the two hx-post attributes on the Reset & re-check button only.
    assert "hx-confirm" not in detail.text[check_now_pos:reset_pos]
    assert "hx-confirm" in detail.text[reset_pos:]


def test_mark_read_then_mark_unread_round_trip(client):
    """Both directions are now in-place htmx toggles (Stage 6 polish) -- neither navigates
    away, and the response swaps the button and the title-row badge together in one shot (a
    primary swap plus an out-of-band swap)."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    # Checked via the button's own hx-post target rather than a loose "Mark as unread" text
    # search -- the auto-mark-as-read script's explanatory comment (see detail.html) happens
    # to contain that exact phrase too, so a plain substring check is a false-positive trap.
    unread_btn = f'hx-post="/updates/{sonarr_id}/unread"'
    read_btn = f'hx-post="/updates/{sonarr_id}/read"'

    resp = client.post(f"/updates/{sonarr_id}/read")
    assert resp.status_code == 200
    assert unread_btn in resp.text
    assert 'id="read-status-badge" hx-swap-oob="true"' in resp.text
    assert "badge-read" in resp.text
    assert db.get_update(sonarr_id)["status"] == "read"

    detail = client.get(f"/updates/{sonarr_id}")
    assert unread_btn in detail.text
    assert 'id="read-toggle-btn"' in detail.text

    resp = client.post(f"/updates/{sonarr_id}/unread")
    assert resp.status_code == 200
    assert read_btn in resp.text
    assert 'id="read-status-badge" hx-swap-oob="true"' in resp.text
    assert "badge-unread" in resp.text
    assert db.get_update(sonarr_id)["status"] == "unread"

    # And the page itself, loaded fresh, reflects the same state (didn't just update the
    # fragment without actually persisting).
    detail = client.get(f"/updates/{sonarr_id}")
    assert unread_btn not in detail.text
    assert read_btn in detail.text


def test_auto_read_beacon_route_marks_read_and_returns_the_fragment(client):
    """navigator.sendBeacon() fires a POST to /read on page-leave (see detail.html) -- proves
    the route it hits behaves correctly even though the caller never reads the response."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    resp = client.post(f"/updates/{sonarr_id}/read")
    assert resp.status_code == 200
    assert db.get_update(sonarr_id)["status"] == "read"


def test_detail_page_registers_the_auto_read_script_only_when_eligible(client):
    """The pagehide listener only makes sense (and only renders) when the page loaded unread
    with real content to have seen -- an already-read update, or one with no content at all,
    has nothing for leaving-the-page to mark."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    detail = client.get(f"/updates/{sonarr_id}")
    assert "pagehide" in detail.text
    assert "suppressAutoRead" in detail.text

    client.post(f"/updates/{sonarr_id}/read")
    detail = client.get(f"/updates/{sonarr_id}")
    assert "pagehide" not in detail.text


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


def _run_scoped_action_and_wait(client, sonarr_id, path):
    with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
         patch("app.reconcile.get_latest_digest", side_effect=_fake_digest):
        resp = client.post(f"/updates/{sonarr_id}/{path}")
        assert resp.status_code == 200
        assert 'id="item-recheck-status"' in resp.text
        assert "spinner" in resp.text

        for _ in range(50):
            item = check_state.get_item_state(f"update:{sonarr_id}")
            if item is None or not item["running"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("scoped action never finished")

        return client.get(f"/updates/{sonarr_id}/recheck-status-poll")


def test_scoped_check_now_reruns_just_that_container_without_touching_an_unchanged_row(client):
    """Check now (non-destructive): posts an htmx fragment (spinner, not a redirect)
    immediately, does a real scoped re-check of just that one container in the background, and
    the poller ends with an HX-Redirect once done. Also proves it shares the same "only one
    check at a time" mutex as a full check (released cleanly afterwards) and doesn't prune
    every other tracked container the way a full check's outcome would."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    poll = _run_scoped_action_and_wait(client, sonarr_id, "check-now")
    # Same digest transition as before (_fake_digest is unchanged) -> row untouched, same id.
    assert poll.headers.get("hx-redirect") == f"/updates/{sonarr_id}"

    assert check_state.get_state("updates")["running"] is False
    # Not pruned down to just the one container the scoped outcome actually contained.
    assert len(db.list_tracked_containers_with_status()) == 3


def test_scoped_reset_and_recheck_forces_a_fresh_row_even_when_the_digest_is_unchanged(client):
    """Reset & re-check (destructive): deletes the update row first, so even though
    _fake_digest reports the exact same pending transition as the original check, the row
    comes back with a brand new id -- proving it genuinely forced a fresh notes fetch rather
    than silently no-op'ing like Check now would for the same unchanged digest."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    poll = _run_scoped_action_and_wait(client, sonarr_id, "reset-and-recheck")
    redirect_url = poll.headers.get("hx-redirect")
    assert redirect_url is not None
    assert redirect_url != f"/updates/{sonarr_id}"
    assert redirect_url.startswith("/updates/")

    assert check_state.get_state("updates")["running"] is False
    assert len(db.list_tracked_containers_with_status()) == 3

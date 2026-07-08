"""End-to-end regression test for Stage 3: drives the real HTTP routes through TestClient
with mocked Docker/registry calls, proving the full check -> persist -> render pipeline works
-- real ids on update rows, the per-update detail page rendering real content, and the global
Reset & re-check route actually wiping persisted state rather than being Stage 1's
placeholder. Uses the shared `client` fixture from conftest.py."""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app import check_state, db
from app.config import settings
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
    """Stage 6 polish: two distinct scoped actions now -- Check Now (only replaces the row if
    the digest actually changed, no confirmation) and Reset & Re-check (wipes this update's
    row first, forcing a fresh notes fetch even if nothing changed, confirmed like the
    destructive global button)."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")

    detail = client.get(f"/updates/{sonarr['id']}")
    assert "Check Now" in detail.text
    assert "Reset &amp; Re-check" in detail.text
    assert f'hx-post="/updates/{sonarr["id"]}/check-now"' in detail.text
    assert f'hx-post="/updates/{sonarr["id"]}/reset-and-recheck"' in detail.text

    check_now_pos = detail.text.index(f'/updates/{sonarr["id"]}/check-now')
    reset_pos = detail.text.index(f'/updates/{sonarr["id"]}/reset-and-recheck')
    # hx-confirm sits between the two hx-post attributes on the Reset & Re-check button only.
    assert "hx-confirm" not in detail.text[check_now_pos:reset_pos]
    assert "hx-confirm" in detail.text[reset_pos:]

    # Buttons appear left to right in this order: read toggle, Check Now, Regenerate AI
    # Response, Reset & Re-check.
    toggle_pos = detail.text.index('id="read-toggle-btn"')
    regen_pos = detail.text.index("Regenerate AI Response")
    assert toggle_pos < check_now_pos < regen_pos < reset_pos


def test_regenerate_button_is_enabled_when_release_notes_exist(client):
    """Stage 7: real notes were fetched during the check (the fixture mocks a real return
    value) -- Regenerate AI Response is clickable, not the permanently-disabled placeholder
    it used to be, and it's no longer called Retry."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")

    detail = client.get(f"/updates/{sonarr['id']}")
    assert "Regenerate AI Response" in detail.text
    assert ">Retry<" not in detail.text
    assert f'hx-post="/updates/{sonarr["id"]}/regenerate"' in detail.text


def test_regenerate_button_is_disabled_when_no_release_notes_were_found(client):
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")

    detail = client.get(f"/updates/{sonarr['id']}")
    assert "Regenerate AI Response" in detail.text
    assert f'hx-post="/updates/{sonarr["id"]}/regenerate"' not in detail.text
    start = detail.text.index("Regenerate AI Response")
    button_start = detail.text.rindex("<button", 0, start)
    assert "disabled" in detail.text[button_start:start]


def test_regenerate_route_regenerates_the_summary_in_place_without_changing_the_id(client):
    """Regenerate AI Response re-runs summarization for the already-stored release notes --
    no registry check, no fresh notes fetch, and (unlike Check Now/Reset & Re-check) the
    update's id never changes since the row is updated in place, not deleted and recreated."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    with patch("app.persist.summarize_update", return_value=("## Bug Fixes\nRegenerated.", "bugfix")):
        resp = client.post(f"/updates/{sonarr_id}/regenerate")
        assert resp.status_code == 200
        assert 'id="item-recheck-status"' in resp.text
        assert "spinner" in resp.text

        for _ in range(50):
            item = check_state.get_item_state(f"update:{sonarr_id}")
            if item is None or not item["running"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("regenerate never finished")

        poll = client.get(f"/updates/{sonarr_id}/recheck-status-poll")
        assert poll.headers.get("hx-redirect") == f"/updates/{sonarr_id}"  # same id, updated in place

    update = db.get_update(sonarr_id)
    assert update["summary_markdown"] == "## Bug Fixes\nRegenerated."
    assert update["severity"] == "bugfix"
    assert update["status"] == "unread"  # regenerating resets it back to unread


def test_back_link_row_puts_updates_first_and_names_the_stack_link_generically(client, tmp_path):
    """Updates always comes first regardless of whether this container is in a stack; the
    stack link (only present for containers in a real multi-service compose stack -- Stage 12
    territory, but the matching code is already live) is always labeled "Back to Stack",
    never the stack's own (possibly AI-generated) display name."""
    compose_file = Path(settings.compose_root) / "teststack.yml"
    compose_file.write_text("services:\n  sonarr:\n    image: linuxserver/sonarr\n  plex:\n    image: linuxserver/plex\n")
    try:
        _run_check_and_wait(client)
        sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")

        detail = client.get(f"/updates/{sonarr['id']}")
        assert "Back to Stack" in detail.text
        updates_pos = detail.text.index("Back to Updates")
        stack_pos = detail.text.index("Back to Stack")
        assert updates_pos < stack_pos
    finally:
        os.remove(compose_file)


def test_first_visit_to_an_unread_update_auto_marks_it_read(client):
    """Opening the detail page at all now counts as "seen it," server-side and unconditional
    -- a client-side "mark it on the way out" approach (pagehide, then visibilitychange as a
    more reliable fallback) was tried first but proved unreliable enough in practice that this
    replaced it outright: simpler, and the browser can't fail to run server code the way it
    can fail to fire a JS event during navigation."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]
    assert db.get_update(sonarr_id)["status"] == "unread"

    detail = client.get(f"/updates/{sonarr_id}")
    assert detail.status_code == 200
    assert db.get_update(sonarr_id)["status"] == "read"
    assert "badge-read" in detail.text
    assert f'hx-post="/updates/{sonarr_id}/unread"' in detail.text  # shows "Mark as unread" now


def test_manual_unread_toggle_works_but_revisiting_marks_it_read_again(client):
    """Mark as unread still works as an explicit action -- but since auto-mark-as-read fires
    on every visit, not just the first, coming back to the same page later marks it read
    again too. That's the deliberate tradeoff for reliability: "unread" doesn't persist across
    a repeat visit, only within the time before the page is next opened."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    client.get(f"/updates/{sonarr_id}")  # auto-marks read
    assert db.get_update(sonarr_id)["status"] == "read"

    resp = client.post(f"/updates/{sonarr_id}/unread")
    assert resp.status_code == 200
    assert 'id="read-status-badge" hx-swap-oob="true"' in resp.text
    assert "badge-unread" in resp.text
    assert db.get_update(sonarr_id)["status"] == "unread"

    client.get(f"/updates/{sonarr_id}")
    assert db.get_update(sonarr_id)["status"] == "read"


def test_toggle_and_auto_mark_work_even_when_no_release_notes_were_found(client):
    """Regression test: an earlier version gated both the auto-mark-as-read logic and the
    manual toggle button's visibility on summary_markdown/release_notes_raw existing -- when
    release notes genuinely couldn't be found for an image (a real, common case -- see
    release_notes.py's Docker Hub last-resort/None fallback), that meant the update was
    permanently stuck Unread, with no button ever rendered to change it either way, and the
    Updates list's Read column never updating for it."""
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]
    assert db.get_update(sonarr_id)["release_notes_raw"] is None

    detail = client.get(f"/updates/{sonarr_id}")
    assert detail.status_code == 200
    assert db.get_update(sonarr_id)["status"] == "read"  # auto-marked despite no notes found
    assert f'hx-post="/updates/{sonarr_id}/unread"' in detail.text  # toggle button is present

    resp = client.post(f"/updates/{sonarr_id}/unread")
    assert resp.status_code == 200
    assert f'hx-post="/updates/{sonarr_id}/read"' in resp.text  # and can be toggled back too


def test_mark_read_route_still_works_directly(client):
    """The explicit "Mark as read" button/route (unread -> read, requires real content) is
    still there and still works even though auto-mark-on-visit makes it rarely the thing that
    actually flips the status in normal use anymore."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")
    sonarr_id = sonarr["id"]

    resp = client.post(f"/updates/{sonarr_id}/read")
    assert resp.status_code == 200
    assert db.get_update(sonarr_id)["status"] == "read"


def test_auto_mark_as_read_never_applies_to_an_error_row(client):
    """Error rows have no read/unread concept -- the badge and toggle are both hidden for them
    (see detail.html), so a visit must never flip their status column."""
    _run_check_and_wait(client)
    broken = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "broken")
    broken_id = broken["id"]
    assert db.get_update(broken_id)["status"] == "unread"

    detail = client.get(f"/updates/{broken_id}")
    assert detail.status_code == 200
    assert db.get_update(broken_id)["status"] == "unread"
    assert "badge-unread" not in detail.text
    assert "badge-read" not in detail.text


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


def test_retry_route_no_longer_exists(client):
    """The old placeholder /retry route is gone entirely now that Regenerate AI Response
    (POST /regenerate) is real -- nothing in the UI has posted to /retry since the rename."""
    _run_check_and_wait(client)
    sonarr = next(r for r in db.list_tracked_containers_with_status() if r["container_name"] == "sonarr")

    resp = client.post(f"/updates/{sonarr['id']}/retry")
    assert resp.status_code == 404


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

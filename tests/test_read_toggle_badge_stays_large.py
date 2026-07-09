"""A real-world report: the Read/Unread badge on a container's detail page was the right size
on first load, but shrank back down after clicking "Mark as Read"/"Mark as Unread". Root cause:
detail.html's initial render used badge-lg (fixed earlier this session), but the toggle route's
own response (_read_toggle.html, swapped in via an out-of-band update on every click) still
rendered the badge without badge-lg -- so every click silently undid the sizing fix."""

from unittest.mock import patch

from app import db


def test_clicking_the_toggle_button_keeps_the_badge_at_badge_lg_size(client):
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name="read-toggle-size-test", image_repo="owner/read-toggle-size-test", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw=None,
        )
    update_id = db.get_latest_update_for_container("read-toggle-size-test")["id"]

    # Initial load auto-marks it read; flip it back to unread via the real toggle route, which
    # is what previously lost the badge-lg class on the OOB-swapped badge.
    client.get(f"/updates/{update_id}")
    resp = client.post(f"/updates/{update_id}/unread")
    assert 'class="badge badge-lg badge-unread"' in resp.text

    resp = client.post(f"/updates/{update_id}/read")
    assert 'class="badge badge-lg badge-read"' in resp.text

    db.delete_update(update_id)

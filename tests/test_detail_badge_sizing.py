"""The Read/Unread badge on the container detail page used to render visibly smaller than the
adjacent severity badge (the base .badge class alone -- 11px text, 2px/8px padding -- versus
.badge .badge-lg for severity -- 13px text, 4px/12px padding), a mismatch a real-world report
called out. Both badges sit in the same title row and should read as the same size."""

from pathlib import Path
from unittest.mock import patch

from app import db

DETAIL_TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "templates" / "detail.html"


def test_both_read_and_unread_badge_branches_carry_badge_lg():
    """Checked against the template source directly rather than a live page: visiting the
    detail page unconditionally auto-marks an unread update as read (see main.py's
    update_detail route), so the "Unread" branch can never actually be observed by GETting
    the page -- by the time any response renders, the status has already flipped."""
    text = DETAIL_TEMPLATE.read_text()
    assert 'class="badge badge-lg badge-unread"' in text
    assert 'class="badge badge-lg badge-read"' in text


def test_the_read_badge_is_reachable_live_and_carries_badge_lg(client):
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name="sonarr", image_repo="owner/sonarr", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw=None,
        )
    update_id = db.get_latest_update_for_container("sonarr")["id"]

    # The visit itself auto-marks it read (see main.py's update_detail route).
    detail = client.get(f"/updates/{update_id}")
    assert 'class="badge badge-lg badge-read"' in detail.text

    db.delete_update(update_id)

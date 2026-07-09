"""An explicit ask: some services are end-of-life and will always show a pending update --
Updates needs the same kind of Silence feature Logs/Compose have, but at the CONTAINER level
(not tied to any one pending update row, since persist.py deletes and recreates that row every
time the digest changes -- an EOL container gets a fresh one on every check)."""

from unittest.mock import patch

from app import db


def _seed_container_with_update(container_name: str):
    db.upsert_container_state(container_name, f"owner/{container_name}", "latest", "sha256:new")
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name=container_name, image_repo=f"owner/{container_name}", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw=None,
        )


def _cleanup(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))


def test_new_containers_default_to_not_silenced():
    db.upsert_container_state("silence-test-fresh", "owner/x", "latest", "sha256:a")
    row = db.get_container_state("silence-test-fresh")
    assert row["silenced"] == 0
    _cleanup("silence-test-fresh")


def test_silencing_a_container_hides_it_from_the_updates_list_by_default(client):
    _seed_container_with_update("silence-test-eol")
    db.set_container_silenced("silence-test-eol", True)

    resp = client.get("/updates")
    # Scoped to just the Updates section -- it always shows in Tracked containers regardless
    # (see test_silenced_container_still_shows_in_tracked_containers_regardless), same as
    # Logs/Compose's "All containers" table always shows everything.
    updates_section = resp.text[:resp.text.index("Tracked containers")]
    assert "silence-test-eol" not in updates_section

    resp = client.get("/updates?show_silenced=1")
    updates_section = resp.text[:resp.text.index("Tracked containers")]
    assert "silence-test-eol" in updates_section
    # No inline badge in the pending-updates list itself -- that's what caused the two-line
    # rows the user didn't want. The badge only lives in the Tracked containers column now
    # (see test_silenced_container_still_shows_in_tracked_containers_regardless).
    assert "badge-silenced" not in updates_section

    _cleanup("silence-test-eol")


def test_silenced_container_still_shows_in_tracked_containers_regardless(client):
    _seed_container_with_update("silence-test-tracked")
    db.set_container_silenced("silence-test-tracked", True)

    resp = client.get("/updates")
    tracked_section = resp.text[resp.text.index("Tracked containers"):]
    assert "silence-test-tracked" in tracked_section
    assert "badge-silenced\">Silenced</span>" in tracked_section

    _cleanup("silence-test-tracked")


def test_tracked_containers_table_has_a_dedicated_silenced_column(client):
    """Replaces the old inline per-row badge (which made rows two lines tall) with a real
    column at the end of the Tracked containers table -- a dash for anything not silenced."""
    _seed_container_with_update("silence-test-column")

    resp = client.get("/updates")
    assert "sort-link" in resp.text and "Silenced" in resp.text
    assert "csort=silenced" in resp.text
    tracked_section = resp.text[resp.text.index("Tracked containers"):]
    row = tracked_section[tracked_section.index("silence-test-column"):]
    row = row[:row.index("</tr>")]
    assert "badge-silenced" not in row
    assert "—" in row

    db.set_container_silenced("silence-test-column", True)
    resp = client.get("/updates")
    tracked_section = resp.text[resp.text.index("Tracked containers"):]
    row = tracked_section[tracked_section.index("silence-test-column"):]
    row = row[:row.index("</tr>")]
    assert "badge-silenced\">Silenced</span>" in row

    _cleanup("silence-test-column")


def test_silence_toggle_on_the_detail_page_is_in_place_not_a_redirect(client):
    _seed_container_with_update("silence-test-toggle")

    resp = client.post("/updates/container/silence-test-toggle/silence")
    assert resp.status_code == 200
    assert 'hx-post="/updates/container/silence-test-toggle/unsilence"' in resp.text
    assert 'id="container-silence-status-badge" hx-swap-oob="true"' in resp.text
    assert db.get_container_state("silence-test-toggle")["silenced"] == 1

    resp = client.post("/updates/container/silence-test-toggle/unsilence")
    assert db.get_container_state("silence-test-toggle")["silenced"] == 0

    _cleanup("silence-test-toggle")


def test_silenced_flag_survives_the_update_row_being_replaced(client):
    """The whole point: an EOL container gets a brand new updates row every time its digest
    changes (persist.py deletes and recreates it), but silenced must stick regardless."""
    _seed_container_with_update("silence-test-survives")
    db.set_container_silenced("silence-test-survives", True)

    old_id = db.get_latest_update_for_container("silence-test-survives")["id"]
    db.delete_update(old_id)
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name="silence-test-survives", image_repo="owner/silence-test-survives", tag="latest",
            old_digest="sha256:new", new_digest="sha256:newer",
            summary_markdown=None, source_url=None, release_notes_raw=None,
        )

    assert db.get_container_state("silence-test-survives")["silenced"] == 1

    _cleanup("silence-test-survives")


def test_detail_page_shows_the_silence_button_and_badge(client):
    _seed_container_with_update("silence-test-detail")
    update_id = db.get_latest_update_for_container("silence-test-detail")["id"]

    resp = client.get(f"/updates/{update_id}")
    assert 'hx-post="/updates/container/silence-test-detail/silence"' in resp.text
    assert resp.text.count("badge-lg badge-silenced") == 0  # not silenced yet

    db.set_container_silenced("silence-test-detail", True)
    resp = client.get(f"/updates/{update_id}")
    assert resp.text.count("badge-lg badge-silenced") == 1
    assert 'hx-post="/updates/container/silence-test-detail/unsilence"' in resp.text

    _cleanup("silence-test-detail")

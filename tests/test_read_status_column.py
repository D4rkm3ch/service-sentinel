"""Stage 6 polish: the Updates page didn't surface anything about whether a pending update had
already been viewed -- list_tracked_containers_with_status() didn't even select the updates
table's read/unread column. Covers both the db-level fix and its rendering as a dedicated Read
column between Status and the row chevron on the main Updates table."""

from unittest.mock import patch

import pytest

from app import db
from app.docker_client import TrackedContainer


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    yield
    db.reset_updates_data()


@pytest.fixture(autouse=True)
def no_real_release_notes_fetch():
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        yield


def _fake_containers():
    return [
        TrackedContainer(name="sonarr", image_repo="linuxserver/sonarr", tag="latest",
                          current_digest="sha256:old", labels={}),
        TrackedContainer(name="broken", image_repo="owner/broken", tag="latest",
                          current_digest="sha256:x", labels={}),
    ]


def _fake_digest(repo, tag):
    return {"linuxserver/sonarr": "sha256:new"}.get(repo)  # "owner/broken" -> None -> error


def test_list_tracked_containers_with_status_includes_read_status():
    with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
         patch("app.reconcile.get_latest_digest", side_effect=_fake_digest):
        from app import persist
        persist.run_and_persist_check()

    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    assert rows["sonarr"]["read_status"] == "unread"

    db.mark_update_status(rows["sonarr"]["id"], "read")
    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    assert rows["sonarr"]["read_status"] == "read"


def test_updates_page_shows_a_read_column(client):
    with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
         patch("app.reconcile.get_latest_digest", side_effect=_fake_digest):
        from app import persist
        persist.run_and_persist_check()

    page = client.get("/updates")
    assert "<th>Read</th>" in page.text
    assert "badge-unread\">Unread</span>" in page.text

    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    db.mark_update_status(rows["sonarr"]["id"], "read")

    page = client.get("/updates")
    assert "badge-read\">Read</span>" in page.text
    # The error row (never marked read/unread -- not applicable) shows a dash, not a badge.
    assert page.text.count("badge-unread\">Unread</span>") == 0

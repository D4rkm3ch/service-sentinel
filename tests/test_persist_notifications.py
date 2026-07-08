"""Stage 10: proves persist.py calls notifications.notify_updates_digest() at the right times
-- once per check run with every genuinely new/changed pending update or check error bucketed
into it, never for a repeat of the exact same transition, and only after the write transaction
has committed (so the rows notify_updates_digest() points at via update_id already exist in the
database by the time it's called). Mocks app.persist.notifications.notify_updates_digest
directly rather than app.notifications._send, since this file is about *whether/when/with what*
persist.py calls it, not what the digest itself decides to do with a call -- see
tests/test_notify_updates_digest.py for that."""

from unittest.mock import patch

import pytest

from app import db, persist

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    yield
    db.reset_updates_data()


@pytest.fixture(autouse=True)
def no_real_release_notes_fetch():
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        yield


def _outcome(*containers, checked_at="2026-01-01T00:00:00+00:00"):
    errors = sum(1 for c in containers if c["status"] == "error")
    return {"containers": list(containers), "errors": errors, "checked_at": checked_at}


def _c(name, status, repo="owner/repo", tag="latest", current_digest="sha256:old", latest_digest="sha256:new"):
    return {
        "container_name": name, "image_repo": repo, "tag": tag, "status": status,
        "current_digest": current_digest, "latest_digest": latest_digest,
    }


def test_a_new_pending_update_with_no_severity_yet_does_not_call_the_digest():
    """severity=="" (AI summarization hasn't succeeded yet, e.g. no provider configured in this
    test) is filtered out of both items and errors before persist.py even calls into
    notifications.py -- nothing worth notifying about yet."""
    with patch("app.persist.notifications.notify_updates_digest") as mock_digest:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
    mock_digest.assert_not_called()


def test_a_severity_backfill_calls_the_digest_with_the_real_row_id_already_committed():
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
    row_id = db.list_tracked_containers_with_status()[0]["id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE updates SET release_notes_raw = ? WHERE id = ?", ("Real notes here.", row_id))

    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist._summarize_container", return_value=("Summary text.", "breaking")), \
         patch("app.persist.notifications.notify_updates_digest") as mock_digest:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_digest.assert_called_once()
    items, errors = mock_digest.call_args[0]
    assert errors == []
    assert len(items) == 1
    assert items[0]["container_name"] == "sonarr"
    assert items[0]["image_repo"] == "owner/repo"
    assert items[0]["tag"] == "latest"
    assert items[0]["severity"] == "breaking"
    # A backfill write deletes the old row and inserts a fresh one -- assert the id actually
    # points at a live, readable row rather than the (now-deleted) original.
    new_row = db.list_tracked_containers_with_status()[0]
    assert items[0]["update_id"] == new_row["id"]
    assert db.get_update(items[0]["update_id"]) is not None


def test_an_unchanged_repeat_check_never_calls_the_digest_again():
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    with patch("app.persist.notifications.notify_updates_digest") as mock_digest:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_digest.assert_not_called()


def test_resolving_to_up_to_date_never_calls_the_digest():
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    with patch("app.persist.notifications.notify_updates_digest") as mock_digest:
        persist.persist_check_outcome(
            _outcome(_c("sonarr", "up_to_date", current_digest="sha256:new", latest_digest="sha256:new"))
        )

    mock_digest.assert_not_called()


def test_a_persistent_registry_error_calls_the_digest_once_not_on_every_check():
    with patch("app.persist.notifications.notify_updates_digest") as mock_digest:
        persist.persist_check_outcome(_outcome(_c("sonarr", "error", latest_digest=None)))
    mock_digest.assert_called_once()
    items, errors = mock_digest.call_args[0]
    assert items == []
    assert len(errors) == 1
    assert errors[0]["container_name"] == "sonarr"
    assert errors[0]["error"]

    with patch("app.persist.notifications.notify_updates_digest") as mock_digest:
        persist.persist_check_outcome(_outcome(_c("sonarr", "error", latest_digest=None)))
    mock_digest.assert_not_called()


def test_multiple_containers_in_one_check_bucket_into_a_single_digest_call():
    with patch("app.persist.release_notes.get_release_notes", return_value=("Real notes.", "https://example.com")), \
         patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist._summarize_container", return_value=("Summary text.", "feature")), \
         patch("app.persist.notifications.notify_updates_digest") as mock_digest:
        persist.persist_check_outcome(_outcome(
            _c("sonarr", "update_available", repo="owner/sonarr"),
            _c("radarr", "update_available", repo="owner/radarr"),
            _c("qbittorrent", "error", repo="owner/qbit", latest_digest=None),
        ))

    mock_digest.assert_called_once()
    items, errors = mock_digest.call_args[0]
    assert {i["container_name"] for i in items} == {"sonarr", "radarr"}
    assert {e["container_name"] for e in errors} == {"qbittorrent"}


def test_a_failing_digest_call_never_breaks_the_check():
    with patch("app.persist.release_notes.get_release_notes", return_value=("Real notes.", "https://example.com")), \
         patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist._summarize_container", return_value=("Summary text.", "feature")), \
         patch("app.persist.notifications.notify_updates_digest", side_effect=RuntimeError("apprise boom")) as mock_digest:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_digest.assert_called_once()  # confirms this test actually exercised the failure path

    row = db.list_tracked_containers_with_status()[0]
    assert row["status"] == "update_available"
    assert row["id"] is not None

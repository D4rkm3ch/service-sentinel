"""Stage 10: proves persist.py calls notifications.notify_update() at the right times -- once
per genuinely new/changed pending update or check error, never for a repeat of the exact same
transition, and only after its write transaction has committed (so the row notify_update()
points at via update_id already exists in the database by the time it's called). Mocks
app.persist.notifications.notify_update directly rather than app.notifications._send, since
this file is about *whether/when persist.py calls it*, not what notify_update() itself decides
to do with a call -- see tests/test_notify_update.py for that."""

from unittest.mock import patch

import pytest

from app import db, persist

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    db.set_notifications_enabled(True)
    db.set_feature_notify_enabled("updates", True)
    yield
    db.reset_updates_data()
    db.set_notifications_enabled(False)


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


def test_a_new_pending_update_notifies_once_with_the_real_row_id_already_committed():
    with patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_notify.assert_called_once()
    kwargs = mock_notify.call_args.kwargs
    assert kwargs["container_name"] == "sonarr"
    assert kwargs["image_repo"] == "owner/repo"
    assert kwargs["tag"] == "latest"
    assert kwargs["error"] is None

    row = db.list_tracked_containers_with_status()[0]
    assert kwargs["update_id"] == row["id"]
    # The row must already be readable by the time notify_update() was called.
    assert db.get_update(kwargs["update_id"]) is not None


def test_an_unchanged_repeat_check_never_notifies_again():
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    with patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_notify.assert_not_called()


def test_resolving_to_up_to_date_never_notifies():
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    with patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(
            _outcome(_c("sonarr", "up_to_date", current_digest="sha256:new", latest_digest="sha256:new"))
        )

    mock_notify.assert_not_called()


def test_a_newer_digest_on_top_of_a_pending_update_notifies_again():
    persist.persist_check_outcome(
        _outcome(_c("sonarr", "update_available", current_digest="sha256:old", latest_digest="sha256:v2"))
    )

    with patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(
            _outcome(_c("sonarr", "update_available", current_digest="sha256:old", latest_digest="sha256:v3"))
        )

    mock_notify.assert_called_once()
    assert mock_notify.call_args.kwargs["update_id"] is not None


def test_a_persistent_registry_error_notifies_once_not_on_every_check():
    with patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(_outcome(_c("sonarr", "error", latest_digest=None)))
    mock_notify.assert_called_once()
    assert mock_notify.call_args.kwargs["error"]

    with patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(_outcome(_c("sonarr", "error", latest_digest=None)))
    mock_notify.assert_not_called()


def test_severity_backfill_after_a_prior_summarization_failure_notifies_again():
    """The row already exists with real release_notes_raw but no severity (a prior AI call
    failed) -- persist.py's _needs_summary_retry path retries summarization and, this time,
    succeeds. That's not "unchanged" (severity actually changed), so it must notify again --
    the only point a severity threshold could ever meaningfully apply to this update."""
    with patch("app.persist.ai_provider.is_configured", return_value=False), \
         patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
    mock_notify.assert_called_once()
    assert mock_notify.call_args.kwargs["severity"] == ""

    row_id = db.list_tracked_containers_with_status()[0]["id"]
    # Simulate the earlier round having found real release notes, just not a summary/severity.
    with db.get_conn() as conn:
        conn.execute("UPDATE updates SET release_notes_raw = ? WHERE id = ?", ("Real notes here.", row_id))

    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist._summarize_container", return_value=("Summary text.", "breaking")), \
         patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_notify.assert_called_once()
    kwargs = mock_notify.call_args.kwargs
    assert kwargs["severity"] == "breaking"


def test_notifications_disabled_globally_skips_the_notify_call_entirely():
    db.set_notifications_enabled(False)
    with patch("app.persist.notifications.notify_update") as mock_notify:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
    mock_notify.assert_not_called()


def test_a_failing_notification_never_breaks_the_check():
    with patch("app.persist.notifications.notify_update", side_effect=RuntimeError("apprise boom")):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    row = db.list_tracked_containers_with_status()[0]
    assert row["status"] == "update_available"
    assert row["id"] is not None

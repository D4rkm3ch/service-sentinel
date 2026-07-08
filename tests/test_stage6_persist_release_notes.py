"""Stage 6 integration: persist.py must fetch release notes for genuinely-new
update_available transitions only -- never for unchanged/up_to_date/error containers, and
never re-fetched on a repeat check that finds the exact same pending update. Mocks
app.persist.release_notes.get_release_notes directly (real fetching is release_notes.py's
own responsibility and already covered by test_release_notes.py) so these tests are purely
about *when* persist.py decides to call it and what it does with the result."""

import threading
import time
from unittest.mock import patch

import pytest

from app import db, persist

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    yield
    db.reset_updates_data()


def _outcome(*containers, checked_at="2026-01-01T00:00:00+00:00"):
    errors = sum(1 for c in containers if c["status"] == "error")
    return {"containers": list(containers), "errors": errors, "checked_at": checked_at}


def _c(name, status, repo="owner/repo", tag="latest", current_digest="sha256:old", latest_digest="sha256:new",
       source_override=None, changelog_url_override=None):
    return {
        "container_name": name, "image_repo": repo, "tag": tag, "status": status,
        "current_digest": current_digest, "latest_digest": latest_digest,
        "source_override": source_override, "changelog_url_override": changelog_url_override,
    }


def test_new_update_available_fetches_release_notes_and_stores_them():
    with patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")) as mock_fetch:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_fetch.assert_called_once_with(
        "owner/repo", "latest", source_override=None, changelog_url_override=None,
    )
    row = db.list_tracked_containers_with_status()[0]
    update = db.get_update(row["id"])
    assert update["release_notes_raw"] == "Fixed a bug"
    assert update["source_url"] == "https://example.com"


def test_label_overrides_are_passed_through_to_release_notes_fetch():
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)) as mock_fetch:
        persist.persist_check_outcome(_outcome(
            _c("sonarr", "update_available", source_override="owner/custom", changelog_url_override="https://example.com/CHANGELOG"),
        ))

    mock_fetch.assert_called_once_with(
        "owner/repo", "latest", source_override="owner/custom", changelog_url_override="https://example.com/CHANGELOG",
    )


def test_up_to_date_container_never_triggers_a_fetch():
    with patch("app.persist.release_notes.get_release_notes") as mock_fetch:
        persist.persist_check_outcome(_outcome(_c("sonarr", "up_to_date", latest_digest="sha256:old")))

    mock_fetch.assert_not_called()


def test_error_container_never_triggers_a_fetch():
    with patch("app.persist.release_notes.get_release_notes") as mock_fetch:
        persist.persist_check_outcome(_outcome(_c("sonarr", "error", latest_digest=None)))

    mock_fetch.assert_not_called()


def test_repeated_check_with_same_pending_update_does_not_refetch():
    with patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")) as mock_fetch:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
        assert mock_fetch.call_count == 1

        # Same exact transition again -- unchanged() short-circuits in _persist_one before the
        # release-notes decision even matters, but prove the fetch itself isn't repeated too.
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
        assert mock_fetch.call_count == 1


def test_a_newer_digest_on_top_of_a_pending_one_triggers_a_fresh_fetch():
    with patch("app.persist.release_notes.get_release_notes", return_value=("v2 notes", "https://example.com/v2")) as mock_fetch:
        persist.persist_check_outcome(
            _outcome(_c("sonarr", "update_available", current_digest="sha256:old", latest_digest="sha256:v2"))
        )
        assert mock_fetch.call_count == 1

        mock_fetch.return_value = ("v3 notes", "https://example.com/v3")
        persist.persist_check_outcome(
            _outcome(_c("sonarr", "update_available", current_digest="sha256:old", latest_digest="sha256:v3"))
        )
        assert mock_fetch.call_count == 2

    row = db.list_tracked_containers_with_status()[0]
    update = db.get_update(row["id"])
    assert update["release_notes_raw"] == "v3 notes"


def test_release_notes_fetch_failure_does_not_break_persistence():
    """A network blip fetching release notes for one container must not abort the whole
    check -- the update row still gets persisted, just with no notes this time around."""
    with patch("app.persist.release_notes.get_release_notes", side_effect=RuntimeError("boom")):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    row = db.list_tracked_containers_with_status()[0]
    assert row["status"] == "update_available"
    update = db.get_update(row["id"])
    assert update["release_notes_raw"] is None


def test_progress_reports_release_notes_stage_only_when_something_needs_fetching():
    """Regression test for the "hangs at 59/59" bug reappearing once notes-fetching was added:
    persist_check_outcome must call on_progress(stage="release_notes", ...) once per fetch, so
    the UI has something to show for however long that phase takes -- and must never announce
    that stage at all when nothing needs fetching (an up_to_date-only check), rather than
    showing a meaningless "0/0"."""
    calls = []

    with patch("app.persist.release_notes.get_release_notes", return_value=("notes", "https://example.com")):
        persist.persist_check_outcome(
            _outcome(_c("sonarr", "update_available"), _c("plex", "up_to_date", latest_digest="sha256:old")),
            on_progress=lambda stage, done, total: calls.append((stage, done, total)),
        )

    release_notes_calls = [c for c in calls if c[0] == "release_notes"]
    assert release_notes_calls == [("release_notes", 0, 1), ("release_notes", 1, 1)]


def test_progress_never_reports_release_notes_stage_when_nothing_is_new():
    calls = []

    with patch("app.persist.release_notes.get_release_notes") as mock_fetch:
        persist.persist_check_outcome(
            _outcome(_c("plex", "up_to_date", latest_digest="sha256:old")),
            on_progress=lambda stage, done, total: calls.append((stage, done, total)),
        )

    mock_fetch.assert_not_called()
    assert calls == []


def test_run_and_persist_check_reports_both_stages_in_order(monkeypatch):
    """End-to-end through run_and_persist_check() (what persist.run_claimed_updates_check()
    actually calls): proves the "checking" stage from reconcile.run_check() and the
    "release_notes" stage from persist_check_outcome() both reach the same on_progress
    callback, in order, with the stage name that lets the UI tell them apart."""
    def fake_run_check(on_progress=None):
        if on_progress:
            on_progress(0, 1)
            on_progress(1, 1)
        return _outcome(_c("sonarr", "update_available"))

    monkeypatch.setattr("app.persist.reconcile.run_check", fake_run_check)
    calls = []

    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        persist.run_and_persist_check(on_progress=lambda stage, done, total: calls.append((stage, done, total)))

    assert calls == [
        ("checking", 0, 1), ("checking", 1, 1),
        ("release_notes", 0, 1), ("release_notes", 1, 1),
    ]


def test_release_notes_fetches_run_concurrently_not_sequentially():
    """Proves the thread pool is actually parallelizing fetches, not just wrapping the old
    sequential loop -- 4 fetches at 0.15s each must finish well under 4x0.15s if truly
    concurrent (settings.ai_summarize_concurrency defaults to 4, so all 4 fit in one batch)."""
    def slow_fetch(image_repo, tag, source_override=None, changelog_url_override=None):
        time.sleep(0.15)
        return (f"notes for {image_repo}", "https://example.com")

    containers = [_c(f"c{i}", "update_available", repo=f"owner/repo{i}") for i in range(4)]

    with patch("app.persist.release_notes.get_release_notes", side_effect=slow_fetch):
        start = time.monotonic()
        persist.persist_check_outcome(_outcome(*containers))
        elapsed = time.monotonic() - start

    assert elapsed < 0.4, f"4 fetches at 0.15s each took {elapsed:.2f}s -- doesn't look concurrent"
    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    for i in range(4):
        assert rows[f"c{i}"]["id"] is not None


def test_progress_covers_every_step_exactly_once_regardless_of_completion_order():
    """With several fetches running concurrently, done-count updates arrive from different
    worker threads and so can land in any order -- but every value from 1..total must still
    be reported exactly once (the shared lock in persist.py must serialize the increments
    correctly, same approach reconcile.run_check() already uses for the checking stage)."""
    def fetch(image_repo, tag, source_override=None, changelog_url_override=None):
        time.sleep(0.02)
        return (None, None)

    containers = [_c(f"c{i}", "update_available", repo=f"owner/repo{i}") for i in range(6)]
    calls = []
    calls_lock = threading.Lock()

    def on_progress(stage, done, total):
        with calls_lock:
            calls.append((stage, done, total))

    with patch("app.persist.release_notes.get_release_notes", side_effect=fetch):
        persist.persist_check_outcome(_outcome(*containers), on_progress=on_progress)

    release_notes_calls = [c for c in calls if c[0] == "release_notes"]
    done_values = sorted(c[1] for c in release_notes_calls)
    assert done_values == list(range(7))  # the initial (0, total) announce, then 1..6 once each
    assert all(c[2] == 6 for c in release_notes_calls)


def test_fetch_happens_before_the_write_transaction_opens():
    """Proves the network call genuinely happens outside the write transaction rather than
    merely being sequenced correctly by accident -- while get_release_notes is "running", the
    write phase hasn't started yet, so this container has no container_state row at all."""
    def fetch_and_check(*args, **kwargs):
        assert db.list_tracked_containers_with_status() == []
        return ("notes", "https://example.com")

    with patch("app.persist.release_notes.get_release_notes", side_effect=fetch_and_check):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    row = db.list_tracked_containers_with_status()[0]
    assert row["id"] is not None

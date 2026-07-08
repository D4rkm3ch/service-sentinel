"""Stage 7: AI summarization. persist.py must generate a summary_markdown + severity for a
genuinely-new update, but only once real release notes text actually came back for it -- and
must skip the whole phase (not just per-container) when ANTHROPIC_API_KEY isn't configured,
mirroring every other AI call site's own early-out. Mocks app.persist.summarize_update and
app.persist.release_notes.get_release_notes directly (summarize_update's own prompt/parsing
logic is summarizer.py's responsibility, not persist.py's) so these tests are purely about
*when* persist.py decides to call it, what it passes in, and what it does with the result."""

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


def test_new_update_with_notes_gets_summarized_and_stored():
    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")), \
         patch("app.persist.compose_lookup.find_service_config", return_value={"image": "owner/repo"}), \
         patch("app.persist.summarize_update", return_value=("## Bug Fixes\nFixed a bug.", "bugfix")) as mock_summarize:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_summarize.assert_called_once_with(
        container_name="sonarr", image_repo="owner/repo",
        old_tag_or_digest="sha256:old", new_tag_or_digest="sha256:new",
        release_notes="Fixed a bug", compose_config={"image": "owner/repo"},
    )
    row = db.list_tracked_containers_with_status()[0]
    update = db.get_update(row["id"])
    assert update["summary_markdown"] == "## Bug Fixes\nFixed a bug."
    assert update["severity"] == "bugfix"


def test_no_summarization_when_no_release_notes_were_found():
    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=(None, "https://hub.docker.com/r/owner/repo/tags")), \
         patch("app.persist.summarize_update") as mock_summarize:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_summarize.assert_not_called()
    row = db.list_tracked_containers_with_status()[0]
    update = db.get_update(row["id"])
    assert update["summary_markdown"] is None
    assert update["severity"] == ""


def test_no_summarization_at_all_when_api_key_is_not_configured():
    """Skipped entirely, not attempted-and-failed per container -- must never even try, let
    alone log a stream of "not configured" exceptions once per new update."""
    with patch("app.persist.ai_provider.is_configured", return_value=False), \
         patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")), \
         patch("app.persist.summarize_update") as mock_summarize:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_summarize.assert_not_called()
    row = db.list_tracked_containers_with_status()[0]
    update = db.get_update(row["id"])
    assert update["release_notes_raw"] == "Fixed a bug"  # notes still stored
    assert update["summary_markdown"] is None


def test_summarization_failure_falls_back_to_no_summary_not_a_broken_check():
    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")), \
         patch("app.persist.compose_lookup.find_service_config", return_value=None), \
         patch("app.persist.summarize_update", side_effect=RuntimeError("boom")):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    row = db.list_tracked_containers_with_status()[0]
    assert row["status"] == "update_available"
    update = db.get_update(row["id"])
    assert update["release_notes_raw"] == "Fixed a bug"  # notes preserved despite the failure
    assert update["summary_markdown"] is None
    assert update["severity"] == ""


def test_repeated_check_with_same_pending_update_does_not_resummarize():
    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")), \
         patch("app.persist.summarize_update", return_value=("summary", "bugfix")) as mock_summarize:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
        assert mock_summarize.call_count == 1

        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
        assert mock_summarize.call_count == 1  # unchanged transition -- notes/summary both skipped


def test_summarization_runs_concurrently_not_sequentially():
    """Same concurrency proof as Stage 6's release-notes fetch (settings.ai_summarize_concurrency
    caps both phases) -- 4 summarizations at 0.15s each must finish well under 4x0.15s."""
    def slow_summarize(**kwargs):
        time.sleep(0.15)
        return (f"summary for {kwargs['container_name']}", "feature")

    containers = [_c(f"c{i}", "update_available", repo=f"owner/repo{i}") for i in range(4)]

    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=("notes", "https://example.com")), \
         patch("app.persist.summarize_update", side_effect=slow_summarize):
        start = time.monotonic()
        persist.persist_check_outcome(_outcome(*containers))
        elapsed = time.monotonic() - start

    assert elapsed < 0.4, f"4 summarizations at 0.15s each took {elapsed:.2f}s -- doesn't look concurrent"
    for i in range(4):
        rows = db.list_tracked_containers_with_status()
        row = next(r for r in rows if r["container_name"] == f"c{i}")
        assert db.get_update(row["id"])["summary_markdown"] == f"summary for c{i}"


def test_progress_reports_summarizing_stage_only_for_containers_with_notes():
    calls = []
    lock = threading.Lock()

    def on_progress(stage, done, total):
        with lock:
            calls.append((stage, done, total))

    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", side_effect=[("notes", "url"), (None, None)]), \
         patch("app.persist.summarize_update", return_value=("summary", "bugfix")):
        persist.persist_check_outcome(
            _outcome(_c("sonarr", "update_available"), _c("radarr", "update_available", repo="owner/radarr")),
            on_progress=on_progress,
        )

    summarizing_calls = [c for c in calls if c[0] == "summarizing"]
    # Only one of the two containers got real notes text back -- summarizing announces a
    # total of 1, not 2, and never a meaningless "0/0" for the one that got skipped.
    assert summarizing_calls == [("summarizing", 0, 1), ("summarizing", 1, 1)]


def test_fetch_happens_before_the_write_transaction_opens():
    """Same property Stage 6 proved for release notes fetching, now for summarization too:
    while summarize_update is "running", the write phase hasn't started yet."""
    def summarize_and_check(**kwargs):
        assert db.list_tracked_containers_with_status() == []
        return ("summary", "bugfix")

    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=("notes", "url")), \
         patch("app.persist.summarize_update", side_effect=summarize_and_check):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))


def test_stuck_summary_with_notes_already_on_file_retries_on_the_next_check_and_succeeds():
    """A prior check fetched real notes but summarization itself failed (e.g. a rate-limited
    or quota-exhausted provider) -- the row is left with release_notes_raw but no severity.
    The next check, even with an unchanged digest, must retry summarization from the notes
    already on file rather than leaving it stuck forever (the same "retry on next check" idea
    Check now already applies to missing release notes, extended to a missing summary)."""
    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")), \
         patch("app.persist.summarize_update", return_value=None):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    first_id = db.list_tracked_containers_with_status()[0]["id"]
    stuck = db.get_update(first_id)
    assert stuck["release_notes_raw"] == "Fixed a bug"
    assert stuck["severity"] == ""

    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes") as mock_fetch, \
         patch("app.persist.summarize_update", return_value=("## Bug Fixes\nFixed a bug.", "bugfix")) as mock_summarize:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_fetch.assert_not_called()  # notes were already on file -- no need to refetch them
    mock_summarize.assert_called_once()
    row = db.list_tracked_containers_with_status()[0]
    update = db.get_update(row["id"])
    assert update["severity"] == "bugfix"
    assert update["summary_markdown"] == "## Bug Fixes\nFixed a bug."
    assert update["release_notes_raw"] == "Fixed a bug"  # preserved, not wiped


def test_stuck_summary_retry_that_fails_again_leaves_the_row_untouched():
    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")), \
         patch("app.persist.summarize_update", return_value=None):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    first_id = db.list_tracked_containers_with_status()[0]["id"]

    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.summarize_update", return_value=None) as mock_summarize:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_summarize.assert_called_once()  # retried...
    row = db.list_tracked_containers_with_status()[0]
    assert row["id"] == first_id  # ...but still failed, so nothing about the row changed
    update = db.get_update(first_id)
    assert update["release_notes_raw"] == "Fixed a bug"
    assert update["severity"] == ""


def test_a_container_with_a_real_severity_already_is_never_resummarized():
    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")), \
         patch("app.persist.summarize_update", return_value=("summary", "bugfix")):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    with patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist.release_notes.get_release_notes") as mock_fetch, \
         patch("app.persist.summarize_update") as mock_summarize:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    mock_fetch.assert_not_called()
    mock_summarize.assert_not_called()

    row = db.list_tracked_containers_with_status()[0]
    assert row["id"] is not None

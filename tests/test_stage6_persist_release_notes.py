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
        "owner/repo", "latest", source_override=None, changelog_url_override=None, since=None,
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
        "owner/repo", "latest", source_override="owner/custom", changelog_url_override="https://example.com/CHANGELOG", since=None,
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


def test_repeated_check_retries_release_notes_when_previously_empty():
    """Check now's retry-fix: if a prior fetch came up completely empty (release_notes_raw
    still None), the exact same unchanged digest transition must still trigger a fresh fetch on
    every later check -- unlike a genuinely unchanged row that already has notes on file (see
    test_repeated_check_with_same_pending_update_does_not_refetch above)."""
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)) as mock_fetch:
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
        assert mock_fetch.call_count == 1

        # Same exact transition again, notes still empty from the first attempt.
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
        assert mock_fetch.call_count == 2


def test_retried_fetch_that_finally_succeeds_actually_gets_written():
    """Guards against the retried fetch's result being silently discarded by the "unchanged"
    short-circuit in _persist_one -- the digest itself never changed, only whether notes exist,
    so the write must still happen once they're finally found."""
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
    row = db.list_tracked_containers_with_status()[0]
    assert db.get_update(row["id"])["release_notes_raw"] is None

    with patch("app.persist.release_notes.get_release_notes", return_value=("finally found it", "https://example.com")):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    row = db.list_tracked_containers_with_status()[0]
    assert db.get_update(row["id"])["release_notes_raw"] == "finally found it"


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
    concurrent. Pins the concurrency setting itself (so all 4 fit in one batch) rather than
    relying on its production default (lowered to 2 after real-world Gemini per-minute
    rate-limiting), since this test's point is proving genuine concurrency, not exercising
    whatever the current default happens to be."""
    def slow_fetch(image_repo, tag, source_override=None, changelog_url_override=None):
        time.sleep(0.15)
        return (f"notes for {image_repo}", "https://example.com")

    containers = [_c(f"c{i}", "update_available", repo=f"owner/repo{i}") for i in range(4)]

    with patch("app.persist.release_notes.get_release_notes", side_effect=slow_fetch), \
         patch("app.ai_provider.concurrency_limit", return_value=4):
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
    def fetch(image_repo, tag, source_override=None, changelog_url_override=None, since=None):
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


def test_run_and_persist_single_check_does_not_touch_an_unchanged_row_that_already_has_notes(monkeypatch):
    """Check now (non-destructive): re-checking a container whose digest hasn't moved, and
    which already has real release notes on file, must not re-fetch or replace the row -- same
    "unchanged" rule a full check already follows, just scoped to one container. (A row still
    missing notes *does* get retried on every check -- see the retry tests above.)"""
    with patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
    first_id = db.list_tracked_containers_with_status()[0]["id"]

    def fake_run_check_one(container_name, on_progress=None):
        assert container_name == "sonarr"
        return _outcome(_c("sonarr", "update_available"))  # exact same transition as before

    monkeypatch.setattr("app.persist.reconcile.run_check_one", fake_run_check_one)
    with patch("app.persist.release_notes.get_release_notes") as mock_fetch:
        persist.run_and_persist_single_check("sonarr")

    mock_fetch.assert_not_called()
    assert db.list_tracked_containers_with_status()[0]["id"] == first_id


def test_run_and_persist_single_reset_and_check_forces_a_fresh_row_and_refetch(monkeypatch):
    """Reset & re-check (destructive): deletes the existing row first, so even the exact same
    digest transition looks brand new to persist_check_outcome() -- forcing a fresh notes
    fetch and a new row/id, unlike Check now above."""
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
    first_id = db.list_tracked_containers_with_status()[0]["id"]

    def fake_run_check_one(container_name, on_progress=None):
        return _outcome(_c("sonarr", "update_available"))  # exact same transition as before

    monkeypatch.setattr("app.persist.reconcile.run_check_one", fake_run_check_one)
    with patch("app.persist.release_notes.get_release_notes", return_value=("fresh notes", "https://example.com")) as mock_fetch:
        persist.run_and_persist_single_reset_and_check("sonarr")

    mock_fetch.assert_called_once()
    rows = db.list_tracked_containers_with_status()
    assert rows[0]["id"] != first_id
    update = db.get_update(rows[0]["id"])
    assert update["release_notes_raw"] == "fresh notes"


def test_run_and_persist_single_reset_and_check_is_a_noop_if_nothing_was_tracked_yet(monkeypatch):
    """No existing row to delete -- must not raise, just behave like a normal fresh check."""
    def fake_run_check_one(container_name, on_progress=None):
        return _outcome(_c("sonarr", "up_to_date", latest_digest="sha256:old"))

    monkeypatch.setattr("app.persist.reconcile.run_check_one", fake_run_check_one)
    persist.run_and_persist_single_reset_and_check("sonarr")
    assert db.list_tracked_containers_with_status()[0]["status"] == "up_to_date"


# ---------------------------------------------------------------------------
# Stage 11: containers sharing an image:tag fetch release notes once between them, not once
# each -- qbittorrent + qbittorrentspare is a real fleet, not a hypothetical.
# ---------------------------------------------------------------------------

def test_containers_sharing_an_image_and_tag_fetch_release_notes_only_once():
    with patch("app.persist.release_notes.get_release_notes", return_value=("Fixed a bug", "https://example.com")) as mock_fetch:
        persist.persist_check_outcome(_outcome(
            _c("qbittorrent", "update_available", repo="owner/qbittorrent", tag="latest"),
            _c("qbittorrentspare", "update_available", repo="owner/qbittorrent", tag="latest"),
        ))

    mock_fetch.assert_called_once_with(
        "owner/qbittorrent", "latest", source_override=None, changelog_url_override=None, since=None,
    )
    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    for name in ("qbittorrent", "qbittorrentspare"):
        update = db.get_update(rows[name]["id"])
        assert update["release_notes_raw"] == "Fixed a bug"
        assert update["source_url"] == "https://example.com"


def test_same_image_different_tag_is_not_deduplicated():
    def fetch_for(image_repo, tag, source_override=None, changelog_url_override=None, since=None):
        return (f"notes for {tag}", "https://example.com")

    with patch("app.persist.release_notes.get_release_notes", side_effect=fetch_for) as mock_fetch:
        persist.persist_check_outcome(_outcome(
            _c("readarr-audiobooks", "update_available", repo="owner/readarr", tag="develop"),
            _c("readarr-ebooks", "update_available", repo="owner/readarr", tag="nightly"),
        ))

    assert mock_fetch.call_count == 2
    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    assert db.get_update(rows["readarr-audiobooks"]["id"])["release_notes_raw"] == "notes for develop"
    assert db.get_update(rows["readarr-ebooks"]["id"])["release_notes_raw"] == "notes for nightly"


def test_same_image_different_label_overrides_is_not_deduplicated():
    """Rare, but two services on the same image could genuinely point their
    servicesentinel.source label at different repos -- deduping past that would silently hand
    one of them the wrong container's notes."""
    def fetch_for(image_repo, tag, source_override=None, changelog_url_override=None, since=None):
        return (f"notes from {source_override}", "https://example.com")

    with patch("app.persist.release_notes.get_release_notes", side_effect=fetch_for) as mock_fetch:
        persist.persist_check_outcome(_outcome(
            _c("svc-a", "update_available", repo="owner/shared", tag="latest", source_override="owner/repo-a"),
            _c("svc-b", "update_available", repo="owner/shared", tag="latest", source_override="owner/repo-b"),
        ))

    assert mock_fetch.call_count == 2
    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    assert db.get_update(rows["svc-a"]["id"])["release_notes_raw"] == "notes from owner/repo-a"
    assert db.get_update(rows["svc-b"]["id"])["release_notes_raw"] == "notes from owner/repo-b"


def test_a_shared_fetch_failure_leaves_every_sharing_container_with_no_notes():
    with patch("app.persist.release_notes.get_release_notes", side_effect=RuntimeError("network down")):
        persist.persist_check_outcome(_outcome(
            _c("qbittorrent", "update_available", repo="owner/qbittorrent", tag="latest"),
            _c("qbittorrentspare", "update_available", repo="owner/qbittorrent", tag="latest"),
        ))

    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    for name in ("qbittorrent", "qbittorrentspare"):
        update = db.get_update(rows[name]["id"])
        assert update["release_notes_raw"] is None


def test_summarization_still_runs_once_per_container_even_when_notes_are_shared():
    """Deduplicating the fetch must never deduplicate the AI summary too -- summarization is
    config-aware by design (Stage 7), so two containers on the same image can legitimately get
    different summaries."""
    with patch("app.persist.release_notes.get_release_notes", return_value=("Real notes.", "https://example.com")), \
         patch("app.persist.ai_provider.is_configured", return_value=True), \
         patch("app.persist._summarize_container") as mock_summarize:
        mock_summarize.side_effect = [("Summary A", "feature", None), ("Summary B", "breaking", None)]
        persist.persist_check_outcome(_outcome(
            _c("qbittorrent", "update_available", repo="owner/qbittorrent", tag="latest"),
            _c("qbittorrentspare", "update_available", repo="owner/qbittorrent", tag="latest"),
        ))

    assert mock_summarize.call_count == 2
    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    severities = {db.get_update(rows[n]["id"])["severity"] for n in ("qbittorrent", "qbittorrentspare")}
    assert severities == {"feature", "breaking"}


def test_progress_jumps_by_the_shared_group_size_for_release_notes_stage():
    calls = []

    def on_progress(stage, done, total):
        if stage == "release_notes":
            calls.append((done, total))

    with patch("app.persist.release_notes.get_release_notes", return_value=("notes", "https://example.com")):
        persist.persist_check_outcome(_outcome(
            _c("qbittorrent", "update_available", repo="owner/qbittorrent", tag="latest"),
            _c("qbittorrentspare", "update_available", repo="owner/qbittorrent", tag="latest"),
            _c("sonarr", "update_available", repo="owner/sonarr", tag="latest"),
        ), on_progress=on_progress)

    assert calls[0] == (0, 3)
    assert calls[-1] == (3, 3)

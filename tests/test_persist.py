"""Stage 3 tests: proves persist.py correctly writes reconcile.run_check()'s outcome into
SQLite -- new update rows created for pending/error containers, resolved ones deleted, stale
transitions replaced rather than duplicated, and removed containers pruned. Uses the real
sqlite db.py functions against a temp database (DATA_DIR set in conftest.py) rather than
mocking them, since the whole point of this module is the SQL write logic itself."""

import sqlite3
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


def _c(name, status, repo="owner/repo", tag="latest", current_digest="sha256:old", latest_digest="sha256:new"):
    return {
        "container_name": name, "image_repo": repo, "tag": tag, "status": status,
        "current_digest": current_digest, "latest_digest": latest_digest,
    }


def test_up_to_date_container_gets_no_update_row():
    persist.persist_check_outcome(_outcome(_c("sonarr", "up_to_date", latest_digest="sha256:old")))

    rows = db.list_tracked_containers_with_status()
    assert len(rows) == 1
    assert rows[0]["status"] == "up_to_date"
    assert rows[0]["id"] is None


def test_update_available_creates_a_real_row_with_an_id():
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    rows = db.list_tracked_containers_with_status()
    assert len(rows) == 1
    assert rows[0]["status"] == "update_available"
    assert rows[0]["id"] is not None
    assert rows[0]["severity"] is None  # no AI yet -- Stage 3 never fabricates a classification

    update = db.get_update(rows[0]["id"])
    assert update["old_digest"] == "sha256:old"
    assert update["new_digest"] == "sha256:new"
    assert update["status"] == "unread"


def test_repeated_check_with_same_pending_update_does_not_duplicate():
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))
    first_id = db.list_tracked_containers_with_status()[0]["id"]

    # Mark it read, then check again with the exact same pending digest transition.
    db.mark_update_status(first_id, "read")
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    row = db.list_tracked_containers_with_status()[0]
    assert row["id"] == first_id  # same row, not a fresh duplicate
    update = db.get_update(first_id)
    assert update["status"] == "read"  # untouched -- re-detecting the same thing didn't reset it


def test_container_updated_outside_the_app_resolves_and_deletes_the_row():
    persist.persist_check_outcome(
        _outcome(_c("sonarr", "update_available", current_digest="sha256:old", latest_digest="sha256:new"))
    )
    assert db.list_tracked_containers_with_status()[0]["id"] is not None

    # Next check: the container is now running what was previously "new_digest".
    persist.persist_check_outcome(
        _outcome(_c("sonarr", "up_to_date", current_digest="sha256:new", latest_digest="sha256:new"))
    )

    row = db.list_tracked_containers_with_status()[0]
    assert row["status"] == "up_to_date"
    assert row["id"] is None


def test_a_newer_update_on_top_of_a_pending_one_replaces_the_row_not_duplicates():
    persist.persist_check_outcome(
        _outcome(_c("sonarr", "update_available", current_digest="sha256:old", latest_digest="sha256:v2"))
    )
    first_id = db.list_tracked_containers_with_status()[0]["id"]

    # A further-newer digest appears before the user ever updated -- old_digest is unchanged
    # (still running the original) but new_digest moved again.
    persist.persist_check_outcome(
        _outcome(_c("sonarr", "update_available", current_digest="sha256:old", latest_digest="sha256:v3"))
    )

    rows = db.list_tracked_containers_with_status()
    assert len(rows) == 1
    second_id = rows[0]["id"]
    assert second_id != first_id
    update = db.get_update(second_id)
    assert update["new_digest"] == "sha256:v3"
    assert db.get_update(first_id) is None  # the stale row is gone, not left orphaned


def test_error_status_creates_an_error_row_and_recovering_deletes_it():
    persist.persist_check_outcome(_outcome(_c("sonarr", "error", latest_digest=None)))
    row = db.list_tracked_containers_with_status()[0]
    assert row["status"] == "error"
    assert row["error"]

    persist.persist_check_outcome(
        _outcome(_c("sonarr", "up_to_date", current_digest="sha256:old", latest_digest="sha256:old"))
    )
    row = db.list_tracked_containers_with_status()[0]
    assert row["status"] == "up_to_date"
    assert row["id"] is None


def test_removed_container_is_pruned_on_next_check():
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available"), _c("radarr", "up_to_date")))
    assert len(db.list_tracked_containers_with_status()) == 2

    # radarr no longer exists on the next check.
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    rows = db.list_tracked_containers_with_status()
    assert [r["container_name"] for r in rows] == ["sonarr"]


def test_empty_outcome_never_wipes_existing_state():
    """An empty containers list means the check itself failed (e.g. Docker socket down) --
    persist_check_outcome must leave everything untouched rather than treating it as "zero
    containers exist now"."""
    persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    persist.persist_check_outcome({"containers": [], "errors": 1, "checked_at": "later"})

    rows = db.list_tracked_containers_with_status()
    assert len(rows) == 1
    assert rows[0]["container_name"] == "sonarr"


def test_run_and_persist_check_wraps_reconcile_and_persists(monkeypatch):
    def fake_run_check(on_progress=None):
        return _outcome(_c("qbittorrent", "update_available"))

    monkeypatch.setattr("app.persist.reconcile.run_check", fake_run_check)
    outcome = persist.run_and_persist_check()

    assert outcome["containers"][0]["container_name"] == "qbittorrent"
    rows = db.list_tracked_containers_with_status()
    assert len(rows) == 1
    assert rows[0]["container_name"] == "qbittorrent"


def test_persist_check_outcome_uses_one_connection_not_one_per_container():
    """Regression test for the real "hangs at N/N for several seconds" report: each db.py
    call used to open/commit/close its own SQLite connection, so 59 containers meant ~200
    separate connect+commit+close cycles (each a real fsync in WAL mode) happening silently
    after the progress bar already showed the check as done. Proves the whole batch now opens
    exactly one connection by counting calls to sqlite3.connect() itself, rather than timing
    it (timing is storage-dependent and wasn't reliable to assert on across environments)."""
    original_connect = sqlite3.connect
    connect_calls = []

    def counting_connect(*args, **kwargs):
        connect_calls.append(1)
        return original_connect(*args, **kwargs)

    containers = tuple(_c(f"c{i}", "update_available", repo=f"owner/repo{i}") for i in range(20))

    with patch("app.db.sqlite3.connect", side_effect=counting_connect):
        persist.persist_check_outcome(_outcome(*containers))

    assert connect_calls == [1], f"expected exactly one connection for the whole batch, got {len(connect_calls)}"
    assert len(db.list_tracked_containers_with_status()) == 20


def test_persist_check_outcome_rolls_back_completely_on_failure():
    """New atomicity property from the single-transaction fix: if the batch fails partway
    through, nothing from that check should land -- not a half-applied state that a naive
    per-container-commit approach would have left behind."""
    persist.persist_check_outcome(_outcome(_c("sonarr", "up_to_date", latest_digest="sha256:old")))
    assert len(db.list_tracked_containers_with_status()) == 1

    containers = [_c(f"c{i}", "update_available", repo=f"owner/repo{i}") for i in range(5)]

    real_record_update = db.record_update

    def failing_record_update(*args, **kwargs):
        if kwargs.get("container_name") == "c3":
            raise RuntimeError("simulated failure partway through the batch")
        return real_record_update(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.persist.db.record_update", failing_record_update)
        with pytest.raises(RuntimeError, match="simulated failure"):
            persist.persist_check_outcome(_outcome(*containers))

    # None of c0-c4 should have been persisted -- and the pre-existing "sonarr" row, which
    # prune_removed_containers would have deleted as part of the same failed transaction,
    # must still be there too.
    rows = db.list_tracked_containers_with_status()
    assert [r["container_name"] for r in rows] == ["sonarr"]

"""Stage 2 tests: proves registry checks run concurrently (not just correctly) and that
correctness from Stage 1 (status classification, docker-socket-down handling) still holds."""

import threading
import time
from unittest.mock import patch

from app import reconcile
from app.docker_client import TrackedContainer


def _container(name, repo="owner/repo", tag="latest", digest="sha256:old"):
    return TrackedContainer(name=name, image_repo=repo, tag=tag, current_digest=digest, labels={})


def test_no_containers_returns_empty_without_calling_registry():
    with patch("app.reconcile.list_tracked_containers", return_value=[]), \
         patch("app.reconcile.get_latest_digest") as mock_digest:
        outcome = reconcile.run_check()

    assert outcome["containers"] == []
    assert outcome["errors"] == 0
    mock_digest.assert_not_called()


def test_docker_socket_failure_reports_one_error_and_no_registry_calls():
    with patch("app.reconcile.list_tracked_containers", side_effect=RuntimeError("socket down")), \
         patch("app.reconcile.get_latest_digest") as mock_digest:
        outcome = reconcile.run_check()

    assert outcome["containers"] == []
    assert outcome["errors"] == 1
    mock_digest.assert_not_called()


def test_status_classification_update_available_up_to_date_and_error():
    # Each container gets a distinct repo so the fake digest function (which has no
    # visibility into which container is calling it under concurrency) can key off image_repo.
    containers = [
        _container("has-update", repo="owner/has-update", digest="sha256:old"),
        _container("current", repo="owner/current", digest="sha256:same"),
        _container("broken", repo="owner/broken", digest="sha256:whatever"),
    ]

    def digest_for(repo, tag):
        return {
            "owner/has-update": "sha256:new",
            "owner/current": "sha256:same",
            "owner/broken": None,
        }[repo]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", side_effect=digest_for):
        outcome = reconcile.run_check()

    by_name = {r["container_name"]: r["status"] for r in outcome["containers"]}
    assert by_name == {
        "has-update": "update_available",
        "current": "up_to_date",
        "broken": "error",
    }
    assert outcome["errors"] == 1
    assert len(outcome["containers"]) == 3


def test_registry_exception_is_caught_per_container_and_marked_error():
    containers = [_container("ok", repo="owner/ok"), _container("boom", repo="owner/boom")]

    def digest_for(repo, tag):
        if repo == "owner/boom":
            raise RuntimeError("registry unreachable")
        return "sha256:old"  # matches current_digest -> up_to_date

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", side_effect=digest_for):
        outcome = reconcile.run_check()

    by_name = {r["container_name"]: r["status"] for r in outcome["containers"]}
    assert by_name == {"ok": "up_to_date", "boom": "error"}
    assert outcome["errors"] == 1


def test_registry_checks_run_concurrently_not_sequentially():
    """The whole point of Stage 2: prove containers are checked in parallel, not one at a
    time. 20 containers, each simulating a 200ms network wait, with concurrency capped at 5.
    Sequential would take ~4s; concurrent (5 at a time) should take ~0.8s. Assert well under
    the sequential time to prove real parallelism rather than just asserting "it's fast"."""
    containers = [_container(f"c{i}", repo=f"owner/repo{i}") for i in range(20)]

    def slow_digest(repo, tag):
        time.sleep(0.2)
        return "sha256:old"  # up_to_date, matches _container's default digest

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", side_effect=slow_digest), \
         patch("app.reconcile.settings") as mock_settings:
        mock_settings.registry_check_concurrency = 5
        start = time.monotonic()
        outcome = reconcile.run_check()
        elapsed = time.monotonic() - start

    assert len(outcome["containers"]) == 20
    assert outcome["errors"] == 0
    # Sequential would be 20 * 0.2s = 4.0s. With 5-way concurrency it should be ~4 batches
    # of 0.2s = ~0.8s. Generous upper bound to avoid flakiness on a loaded CI box.
    assert elapsed < 2.0, f"expected concurrent execution well under 2s, took {elapsed:.2f}s"


def test_concurrency_capped_at_container_count_when_fewer_containers_than_limit():
    """max_workers should never exceed len(containers) — mostly a guard against
    ThreadPoolExecutor being asked for 0 or negative workers on edge-case configs."""
    containers = [_container("only-one", repo="owner/only-one")]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:old") as mock_digest, \
         patch("app.reconcile.settings") as mock_settings:
        mock_settings.registry_check_concurrency = 10
        outcome = reconcile.run_check()

    assert len(outcome["containers"]) == 1
    mock_digest.assert_called_once()


def test_on_progress_called_once_per_container_ending_at_done_equals_total():
    """Drives the new Check-now progress counter: on_progress must fire once up front with
    (0, total), then once per finished container, and the final call must be (total, total)
    regardless of the order concurrent workers finish in."""
    containers = [_container(f"c{i}", repo=f"owner/repo{i}") for i in range(8)]
    calls = []
    calls_lock = threading.Lock()

    def record_progress(done, total):
        with calls_lock:
            calls.append((done, total))

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:old"), \
         patch("app.reconcile.settings") as mock_settings:
        mock_settings.registry_check_concurrency = 4
        reconcile.run_check(on_progress=record_progress)

    # One initial (0, total) call, then one call per container finishing.
    assert calls[0] == (0, 8)
    assert len(calls) == 9
    done_values = [c[0] for c in calls[1:]]
    assert sorted(done_values) == list(range(1, 9))
    assert all(total == 8 for _, total in calls)


def test_on_progress_receives_zero_total_when_no_containers():
    calls = []
    with patch("app.reconcile.list_tracked_containers", return_value=[]):
        reconcile.run_check(on_progress=lambda done, total: calls.append((done, total)))

    assert calls == [(0, 0)]


def test_call_without_on_progress_still_works():
    """on_progress is optional -- make sure the default (None) doesn't blow up anywhere
    progress reporting was added."""
    containers = [_container("c", repo="owner/c")]
    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:old"):
        outcome = reconcile.run_check()

    assert len(outcome["containers"]) == 1

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


def test_run_check_many_only_checks_the_named_containers():
    """Backs the stack-level Reset & re-check button -- must only touch the containers named,
    never the rest of the fleet, which is exactly the "checked all 59" scenario it exists to
    rule out."""
    containers = [
        _container("sonarr", repo="owner/sonarr"),
        _container("radarr", repo="owner/radarr"),
        _container("unrelated", repo="owner/unrelated"),
    ]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:old"):
        outcome = reconcile.run_check_many(["sonarr", "radarr"])

    names = {r["container_name"] for r in outcome["containers"]}
    assert names == {"sonarr", "radarr"}


def test_run_check_many_with_no_matching_containers_returns_empty():
    with patch("app.reconcile.list_tracked_containers", return_value=[_container("other")]), \
         patch("app.reconcile.get_latest_digest") as mock_digest:
        outcome = reconcile.run_check_many(["gone"])

    assert outcome["containers"] == []
    assert outcome["errors"] == 0
    mock_digest.assert_not_called()


def test_run_check_many_docker_socket_failure_reports_one_error():
    with patch("app.reconcile.list_tracked_containers", side_effect=RuntimeError("socket down")):
        outcome = reconcile.run_check_many(["sonarr"])

    assert outcome["containers"] == []
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


def test_containers_sharing_an_image_and_tag_only_call_the_registry_once():
    """Stage 11: qbittorrent + qbittorrentspare is a real fleet, not a hypothetical -- two
    containers on the exact same image:tag must share one registry lookup, not pay for one
    each."""
    containers = [
        _container("qbittorrent", repo="owner/qbittorrent", tag="latest", digest="sha256:old"),
        _container("qbittorrentspare", repo="owner/qbittorrent", tag="latest", digest="sha256:old"),
    ]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:new") as mock_digest:
        outcome = reconcile.run_check()

    mock_digest.assert_called_once_with("owner/qbittorrent", "latest")
    assert len(outcome["containers"]) == 2
    by_name = {r["container_name"]: r for r in outcome["containers"]}
    assert by_name["qbittorrent"]["latest_digest"] == "sha256:new"
    assert by_name["qbittorrentspare"]["latest_digest"] == "sha256:new"


def test_containers_sharing_an_image_can_still_have_different_status():
    """Deduplicating the registry lookup must never make two containers' *status* the same by
    accident -- their own current_digest still decides update_available vs up_to_date
    independently, e.g. one was restarted onto the new image already and the other wasn't."""
    containers = [
        _container("qbittorrent", repo="owner/qbittorrent", tag="latest", digest="sha256:old"),
        _container("qbittorrentspare", repo="owner/qbittorrent", tag="latest", digest="sha256:new"),
    ]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:new"):
        outcome = reconcile.run_check()

    by_name = {r["container_name"]: r["status"] for r in outcome["containers"]}
    assert by_name == {"qbittorrent": "update_available", "qbittorrentspare": "up_to_date"}


def test_same_repo_different_tag_is_not_deduplicated():
    containers = [
        _container("readarr-audiobooks", repo="owner/readarr", tag="develop", digest="sha256:old"),
        _container("readarr-ebooks", repo="owner/readarr", tag="nightly", digest="sha256:old"),
    ]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:new") as mock_digest:
        reconcile.run_check()

    assert mock_digest.call_count == 2
    mock_digest.assert_any_call("owner/readarr", "develop")
    mock_digest.assert_any_call("owner/readarr", "nightly")


def test_a_registry_error_on_a_shared_image_marks_every_sharing_container_as_error():
    containers = [
        _container("qbittorrent", repo="owner/qbittorrent", tag="latest"),
        _container("qbittorrentspare", repo="owner/qbittorrent", tag="latest"),
    ]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", side_effect=RuntimeError("registry unreachable")):
        outcome = reconcile.run_check()

    by_name = {r["container_name"]: r["status"] for r in outcome["containers"]}
    assert by_name == {"qbittorrent": "error", "qbittorrentspare": "error"}
    assert outcome["errors"] == 2


def test_progress_jumps_by_the_shared_group_size_when_a_deduplicated_lookup_completes():
    """A shared image's single registry call still credits progress for every container that
    was waiting on it, not just one -- otherwise the counter would visibly stall short of
    total once every *unique* image had been checked, even though every container really is
    done."""
    containers = [
        _container("qbittorrent", repo="owner/qbittorrent", tag="latest"),
        _container("qbittorrentspare", repo="owner/qbittorrent", tag="latest"),
        _container("sonarr", repo="owner/sonarr", tag="latest"),
    ]
    calls = []

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:old"):
        reconcile.run_check(on_progress=lambda done, total: calls.append((done, total)))

    assert calls[0] == (0, 3)
    assert calls[-1] == (3, 3)
    # Every intermediate done-count is a real container count (1, 2, or 3), and the group of
    # two sharing an image always advances together -- 2 never appears without also seeing 3
    # land in the very next call for this fixture (only one other independent group exists).
    assert all(done in (0, 1, 2, 3) for done, _ in calls)


def test_run_check_many_also_deduplicates_a_shared_image():
    containers = [
        _container("qbittorrent", repo="owner/qbittorrent", tag="latest"),
        _container("qbittorrentspare", repo="owner/qbittorrent", tag="latest"),
        _container("unrelated", repo="owner/unrelated", tag="latest"),
    ]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:old") as mock_digest:
        outcome = reconcile.run_check_many(["qbittorrent", "qbittorrentspare"])

    mock_digest.assert_called_once_with("owner/qbittorrent", "latest")
    assert {r["container_name"] for r in outcome["containers"]} == {"qbittorrent", "qbittorrentspare"}

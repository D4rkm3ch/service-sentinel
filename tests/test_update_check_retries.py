"""Registry lookup retries for update checks.

A real-world report: update checks were "failing" on containers that were perfectly healthy.
A digest lookup is a single HTTP call to a third-party registry with a 10s timeout and, until
now, exactly one attempt -- so a momentary rate limit, a transient 5xx, or a DNS blip surfaced
as a hard "check failed" the operator had to notice and re-run by hand. Retries are configurable
in Settings (default 2, 0 restores the old single-attempt behavior).

The sleep between attempts is patched out throughout -- these assert the retry *logic*, and the
real delay is only there to give a rate-limited registry a moment to recover.
"""

from unittest.mock import patch

import pytest

from app import db, reconcile
from app.docker_client import TrackedContainer

db.init_db()


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("app.reconcile.time.sleep"):
        yield


@pytest.fixture(autouse=True)
def restore_retries():
    original = db.get_update_check_retries()
    yield
    db.set_update_check_retries(original)


def _container(name="sonarr", repo="linuxserver/sonarr", tag="latest"):
    return TrackedContainer(name=name, image_repo=repo, tag=tag, current_digest="sha256:old", labels={})


# ---------------------------------------------------------------------------
# _digest_with_retry
# ---------------------------------------------------------------------------

def test_a_successful_lookup_is_not_retried():
    with patch("app.reconcile.get_latest_digest", return_value="sha256:new") as mock_get:
        assert reconcile._digest_with_retry("owner/repo", "latest", retries=3) == "sha256:new"
    assert mock_get.call_count == 1


def test_a_failing_lookup_is_retried_up_to_the_limit():
    with patch("app.reconcile.get_latest_digest", return_value=None) as mock_get:
        assert reconcile._digest_with_retry("owner/repo", "latest", retries=2) is None
    # 1 initial attempt + 2 retries
    assert mock_get.call_count == 3


def test_a_lookup_that_recovers_mid_retry_returns_the_digest():
    with patch("app.reconcile.get_latest_digest", side_effect=[None, None, "sha256:new"]) as mock_get:
        assert reconcile._digest_with_retry("owner/repo", "latest", retries=3) == "sha256:new"
    # Stops as soon as it succeeds rather than using its whole budget.
    assert mock_get.call_count == 3


def test_zero_retries_is_exactly_one_attempt():
    with patch("app.reconcile.get_latest_digest", return_value=None) as mock_get:
        assert reconcile._digest_with_retry("owner/repo", "latest", retries=0) is None
    assert mock_get.call_count == 1


def test_an_unexpected_exception_is_swallowed_and_not_retried():
    """get_latest_digest handles its own HTTP errors and returns None; anything escaping it is a
    genuine bug, not the flaky-network case retries exist for. It must still not abort the
    surrounding check."""
    with patch("app.reconcile.get_latest_digest", side_effect=RuntimeError("bug")) as mock_get:
        assert reconcile._digest_with_retry("owner/repo", "latest", retries=3) is None
    assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# Threaded through the real check entry points
# ---------------------------------------------------------------------------

def test_run_check_passes_retries_through_to_the_registry_lookup():
    with patch("app.reconcile.list_tracked_containers", return_value=[_container()]), \
         patch("app.reconcile.get_latest_digest", return_value=None) as mock_get:
        result = reconcile.run_check(retries=2)
    assert mock_get.call_count == 3
    assert result["errors"] == 1


def test_run_check_one_retries_too():
    with patch("app.reconcile.list_tracked_containers", return_value=[_container()]), \
         patch("app.reconcile.get_latest_digest", return_value=None) as mock_get:
        reconcile.run_check_one("sonarr", retries=2)
    assert mock_get.call_count == 3


def test_run_check_many_retries_too():
    with patch("app.reconcile.list_tracked_containers", return_value=[_container()]), \
         patch("app.reconcile.get_latest_digest", return_value=None) as mock_get:
        reconcile.run_check_many(["sonarr"], retries=2)
    assert mock_get.call_count == 3


def test_a_recovered_retry_reports_the_container_as_up_to_date_not_an_error():
    """The whole point: a container whose first lookup blipped isn't reported as a check error."""
    container = _container()
    container = TrackedContainer(name="sonarr", image_repo="linuxserver/sonarr", tag="latest",
                                 current_digest="sha256:same", labels={})
    with patch("app.reconcile.list_tracked_containers", return_value=[container]), \
         patch("app.reconcile.get_latest_digest", side_effect=[None, "sha256:same"]):
        result = reconcile.run_check(retries=2)
    assert result["errors"] == 0
    assert result["containers"][0]["status"] == "up_to_date"


def test_retries_default_to_zero_so_existing_callers_are_unchanged():
    with patch("app.reconcile.list_tracked_containers", return_value=[_container()]), \
         patch("app.reconcile.get_latest_digest", return_value=None) as mock_get:
        reconcile.run_check()
    assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# The setting itself
# ---------------------------------------------------------------------------

def test_the_setting_defaults_to_two():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'update_check_retries'")
    assert db.get_update_check_retries() == db.UPDATE_RETRY_DEFAULT == 2


def test_the_setting_round_trips():
    db.set_update_check_retries(5)
    assert db.get_update_check_retries() == 5


def test_the_route_saves_a_valid_value(client):
    resp = client.post("/settings/updates/retries", data={"value": "4"})
    assert resp.json() == {"ok": True, "value": 4}
    assert db.get_update_check_retries() == 4


def test_the_route_rejects_a_non_number(client):
    resp = client.post("/settings/updates/retries", data={"value": "lots"})
    body = resp.json()
    assert body["ok"] is False
    assert "whole number" in body["message"]


def test_the_route_rejects_an_out_of_range_value(client):
    resp = client.post("/settings/updates/retries", data={"value": "99"})
    body = resp.json()
    assert body["ok"] is False
    assert str(db.UPDATE_RETRY_MAX) in body["message"]


def test_persist_uses_the_configured_retry_count():
    """The wiring that actually matters day to day: a scheduled/manual check reads the setting
    rather than silently using the default."""
    db.set_update_check_retries(3)
    from app import persist
    with patch("app.persist.reconcile.run_check", return_value={"containers": [], "errors": 0,
                                                                 "checked_at": db.now_iso()}) as mock_run:
        persist.run_and_persist_check()
    assert mock_run.call_args.kwargs["retries"] == 3

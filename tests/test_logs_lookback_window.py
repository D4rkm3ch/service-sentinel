"""A real-world report: Logs always looked back a fixed 24 hours whenever a container had no
checkpoint (first-ever check, or one just reset), which could re-surface an already-fixed issue
whose log lines were still sitting in Docker's own log buffer from before the fix. The lookback
window itself is now a user-configurable Settings dropdown (1h/3h/6h/12h/24h/3d/7d, default 6h)
instead of a fixed LOG_LOOKBACK_HOURS env var -- an earlier "no limit, since last reset" option
was considered and rejected (a follow-up concern: a container with weeks of verbose logs and no
recent checkpoint could make Docker seek through all of that for one fetch) in favor of always
being hour-bounded.

A second, independent setting -- logs_use_checkpoint (default on) -- controls whether checkpoints
are used to bound a fetch at all: on (the default) is today's normal incremental behavior (a
container with a checkpoint fetches strictly since it, ignoring the lookback hours entirely; the
hours only apply as the fallback for one with none). Off makes every check, for every container,
always use the fixed lookback window regardless of any stored checkpoint."""

from datetime import datetime, timedelta, timezone

from app import db, docker_client

db.init_db()


def setup_function(_):
    db.set_logs_lookback("6")
    db.set_logs_use_checkpoint(True)


def teardown_function(_):
    db.set_logs_lookback("6")
    db.set_logs_use_checkpoint(True)


# ---------------------------------------------------------------------------
# db.py -- the settings themselves
# ---------------------------------------------------------------------------

def test_logs_lookback_defaults_to_six_hours():
    assert db.get_logs_lookback() == "6"
    assert db.get_logs_lookback_hours() == 6


def test_logs_lookback_hour_options_map_to_the_right_hour_counts():
    for value, hours in (("1", 1), ("3", 3), ("6", 6), ("12", 12), ("24", 24), ("72", 72), ("168", 168)):
        db.set_logs_lookback(value)
        assert db.get_logs_lookback() == value
        assert db.get_logs_lookback_hours() == hours


def test_logs_lookback_no_longer_offers_an_unbounded_option():
    assert "since_reset" not in db.LOGS_LOOKBACK_HOURS
    assert None not in db.LOGS_LOOKBACK_HOURS.values()


def test_logs_use_checkpoint_defaults_to_on():
    assert db.get_logs_use_checkpoint() is True


def test_logs_use_checkpoint_round_trips():
    db.set_logs_use_checkpoint(False)
    assert db.get_logs_use_checkpoint() is False
    db.set_logs_use_checkpoint(True)
    assert db.get_logs_use_checkpoint() is True


# ---------------------------------------------------------------------------
# Settings route
# ---------------------------------------------------------------------------

def test_logs_lookback_route_saves_and_reflects_on_settings_page(client):
    resp = client.post("/settings/logs-lookback", data={"logs_lookback": "72"})
    assert resp.status_code == 200
    assert db.get_logs_lookback() == "72"
    assert db.get_logs_lookback_hours() == 72

    page = client.get("/settings")
    assert 'value="72" selected' in page.text
    assert "logs_lookback" in page.text


def test_logs_lookback_route_rejects_an_unknown_value(client):
    resp = client.post("/settings/logs-lookback", data={"logs_lookback": "not-a-real-option"})
    assert resp.status_code == 400


def test_logs_lookback_route_rejects_the_removed_since_reset_value(client):
    resp = client.post("/settings/logs-lookback", data={"logs_lookback": "since_reset"})
    assert resp.status_code == 400


def test_logs_use_checkpoint_route_saves(client):
    resp = client.post("/settings/logs-use-checkpoint", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_logs_use_checkpoint() is True

    resp = client.post("/settings/logs-use-checkpoint", data={})
    assert resp.status_code == 200
    assert db.get_logs_use_checkpoint() is False


def test_settings_page_has_a_lookback_window_section_with_both_subsections(client):
    text = client.get("/settings").text
    assert ">Lookback Window<" in text
    assert "<h4>Release Notes</h4>" in text
    assert "<h4>Runtime</h4>" in text
    assert "Use checkpoint" in text


# ---------------------------------------------------------------------------
# docker_client.get_container_logs_since -- the actual fetch window
# ---------------------------------------------------------------------------

class _FakeContainer:
    def __init__(self):
        self.logs_kwargs = None

    def logs(self, **kwargs):
        self.logs_kwargs = kwargs
        return b"line one\nline two\n"


class _FakeContainers:
    def __init__(self, container):
        self._container = container

    def get(self, name):
        return self._container


class _FakeClient:
    def __init__(self):
        self.container = _FakeContainer()
        self.containers = _FakeContainers(self.container)


def test_no_checkpoint_uses_the_configured_hour_window():
    db.set_logs_lookback("6")
    fake = _FakeClient()
    before = datetime.now(timezone.utc)
    docker_client.get_container_logs_since("some-container", None, 5000, client=fake)
    after = datetime.now(timezone.utc)

    since = fake.container.logs_kwargs["since"]
    assert before - timedelta(hours=6) <= since <= after - timedelta(hours=6)


def test_a_real_checkpoint_is_used_regardless_of_the_lookback_setting():
    """An ordinary Check now (a container with a real prior checkpoint) is never affected by
    this setting -- it always fetches strictly since its own last check."""
    db.set_logs_lookback("6")
    fake = _FakeClient()
    checkpoint = "2026-01-01T00:00:00+00:00"
    docker_client.get_container_logs_since("some-container", checkpoint, 5000, client=fake)
    assert fake.container.logs_kwargs["since"] == datetime.fromisoformat(checkpoint)


# ---------------------------------------------------------------------------
# log_watcher.run_log_check_for -- the checkpoint on/off toggle itself
# ---------------------------------------------------------------------------

def test_checkpoint_off_ignores_an_existing_checkpoint_and_uses_the_lookback_window(client):
    from unittest.mock import patch

    from app import log_watcher

    db.set_logs_use_checkpoint(False)
    db.set_logs_lookback("3")
    db.set_log_watch_checkpoint("checkpoint-off-container")

    with patch("app.log_watcher.get_container_logs_since", return_value=None) as mock_fetch:
        log_watcher.run_log_check_for(["checkpoint-off-container"])

    # since_iso passed through is None despite a real checkpoint existing -- get_container_logs_
    # since falls back to the configured lookback window in that case (see the docker_client
    # tests above).
    assert mock_fetch.call_args.args[1] is None


def test_checkpoint_on_passes_the_real_checkpoint_through(client):
    from unittest.mock import patch

    from app import log_watcher

    db.set_logs_use_checkpoint(True)
    db.set_log_watch_checkpoint("checkpoint-on-container")
    checkpoint = db.get_log_watch_checkpoint("checkpoint-on-container")

    with patch("app.log_watcher.get_container_logs_since", return_value=None) as mock_fetch:
        log_watcher.run_log_check_for(["checkpoint-on-container"])

    assert mock_fetch.call_args.args[1] == checkpoint

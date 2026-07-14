"""A real-world report: Logs always looked back a fixed 24 hours whenever a container had no
checkpoint (first-ever check, or one just reset), which could re-surface an already-fixed issue
whose log lines were still sitting in Docker's own log buffer from before the fix, since a
Reset & re-check used to clear the checkpoint outright. Two changes: (1) db.reset_logs_data's
default "since_reset" lookback mode now stamps the checkpoint to the reset moment instead of
clearing it (see test_logs_full_parity_actions.py's own reset tests for that half), and (2) the
lookback window itself is now a user-configurable Settings dropdown (mirroring the existing
Release Notes lookback picker) instead of a fixed LOG_LOOKBACK_HOURS env var."""

from datetime import datetime, timedelta, timezone

from app import db, docker_client

db.init_db()


def setup_function(_):
    db.set_logs_lookback("since_reset")


def teardown_function(_):
    db.set_logs_lookback("since_reset")


# ---------------------------------------------------------------------------
# db.py -- the setting itself
# ---------------------------------------------------------------------------

def test_logs_lookback_defaults_to_since_reset():
    assert db.get_logs_lookback() == "since_reset"
    assert db.get_logs_lookback_hours() is None


def test_logs_lookback_hour_options_map_to_the_right_hour_counts():
    for value, hours in (("6", 6), ("12", 12), ("24", 24), ("72", 72), ("168", 168)):
        db.set_logs_lookback(value)
        assert db.get_logs_lookback() == value
        assert db.get_logs_lookback_hours() == hours


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


def test_settings_page_has_a_lookback_window_section_with_both_subsections(client):
    text = client.get("/settings").text
    assert ">Lookback Window<" in text
    assert "<h4>Release Notes</h4>" in text
    assert "<h4>Runtime</h4>" in text


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


def test_no_checkpoint_under_since_reset_sets_no_time_bound_at_all():
    """Relies purely on `tail` to stay bounded -- same "uncapped, just bounded by the natural
    mechanism already in place" shape as Release Notes' own "since_check" option."""
    db.set_logs_lookback("since_reset")
    fake = _FakeClient()
    docker_client.get_container_logs_since("some-container", None, 5000, client=fake)
    assert "since" not in fake.container.logs_kwargs
    assert fake.container.logs_kwargs["tail"] == 5000


def test_no_checkpoint_under_an_hour_based_lookback_sets_that_many_hours_back():
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

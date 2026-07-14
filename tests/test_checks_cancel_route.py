"""HTTP-level test for the single, feature-agnostic /checks/cancel endpoint every Check Now
button (main page, stack, item, finding-detail) posts to once it's showing "Cancel" -- see
base.html and check_state.request_cancel_running_checks()."""

from app import check_state, db

db.init_db()


def _reset():
    # set_running() unconditionally clears each feature's cancel flag (unlike release_running,
    # which only touches the running bool) -- see check_state.py -- so this always leaves a
    # clean slate between tests regardless of what a prior test left set.
    for feature in check_state.FEATURES:
        check_state.set_running(feature)
        check_state.release_running(feature)


def setup_function(_):
    _reset()


def teardown_function(_):
    _reset()


def test_cancel_route_signals_whichever_feature_is_running(client):
    check_state.set_running("logs")
    resp = client.post("/checks/cancel")
    assert resp.status_code == 200
    assert check_state.is_cancel_requested("logs") is True
    assert check_state.is_cancel_requested("updates") is False


def test_cancel_route_is_harmless_when_nothing_is_running(client):
    resp = client.post("/checks/cancel")
    assert resp.status_code == 200
    for feature in check_state.FEATURES:
        assert check_state.is_cancel_requested(feature) is False

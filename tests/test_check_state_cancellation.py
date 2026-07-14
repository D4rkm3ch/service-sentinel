"""Cancel-button plumbing in check_state.py -- every feature's own running-mutex (try_start/
set_running) doubles as the scope for a per-feature cancel signal, since every scoped/full check
for a feature already shares that one mutex (see persist.py/log_watcher.py/compose_reviewer.py's
own docstrings). Setting the flag asks whichever check currently holds the mutex to stop between
items; it's cleared the moment that (or any later) check claims the mutex again, so a stale
cancel from a finished check can never bleed into the next one."""

from app import check_state, db

db.init_db()


def _reset():
    # release_running() alone doesn't clear a pending cancel flag (only (re)claiming the mutex
    # via try_start/set_running does, by design -- see check_state.py) -- set_running()
    # unconditionally claims the mutex (unlike try_start, which no-ops if already "running")
    # and clears the flag, so this always leaves a clean slate regardless of what a given test
    # left running.
    for feature in check_state.FEATURES:
        check_state.set_running(feature)
        check_state.release_running(feature)


def setup_function(_):
    _reset()


def teardown_function(_):
    _reset()


def test_is_cancel_requested_defaults_false():
    assert check_state.is_cancel_requested("updates") is False


def test_request_cancel_sets_the_flag():
    check_state.request_cancel("updates")
    assert check_state.is_cancel_requested("updates") is True


def test_request_cancel_is_scoped_to_its_own_feature():
    check_state.request_cancel("logs")
    assert check_state.is_cancel_requested("logs") is True
    assert check_state.is_cancel_requested("updates") is False
    assert check_state.is_cancel_requested("compose") is False


def test_set_running_clears_a_stale_cancel_flag():
    check_state.request_cancel("updates")
    assert check_state.is_cancel_requested("updates") is True
    check_state.set_running("updates")
    assert check_state.is_cancel_requested("updates") is False


def test_try_start_clears_a_stale_cancel_flag():
    check_state.request_cancel("compose")
    assert check_state.try_start("compose") is True
    assert check_state.is_cancel_requested("compose") is False


def test_request_cancel_running_checks_only_signals_features_actually_running():
    check_state.set_running("updates")
    cancelled = check_state.request_cancel_running_checks()
    assert cancelled == ["updates"]
    assert check_state.is_cancel_requested("updates") is True
    assert check_state.is_cancel_requested("logs") is False
    assert check_state.is_cancel_requested("compose") is False


def test_request_cancel_running_checks_signals_every_feature_currently_running():
    check_state.set_running("logs")
    check_state.set_running("compose")
    cancelled = check_state.request_cancel_running_checks()
    assert sorted(cancelled) == ["compose", "logs"]
    assert check_state.is_cancel_requested("logs") is True
    assert check_state.is_cancel_requested("compose") is True
    assert check_state.is_cancel_requested("updates") is False


def test_request_cancel_running_checks_is_a_no_op_when_nothing_is_running():
    cancelled = check_state.request_cancel_running_checks()
    assert cancelled == []

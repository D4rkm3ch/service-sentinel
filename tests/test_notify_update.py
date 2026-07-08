"""Stage 10 of the ground-up rebuild: real notifications for Updates. Direct unit tests for
notifications.notify_update() -- the dormant reference function persist.py now actually calls
once per genuinely new/changed pending update (see tests/test_persist.py for the wiring-level
tests that prove *when* it gets called). These tests cover its own decision logic in isolation:
the master/feature toggles, the severity threshold, the "unknown severity never notifies"
rule, and the separate opt-in gate for registry-check errors -- by mocking app.notifications.db
and app.notifications._send directly, per the established per-module mocking pattern."""

from unittest.mock import patch

from app import notifications


def _settings(**overrides):
    base = {
        "notifications_enabled": True,
        "feature_notify_enabled": True,
        "notify_updates_include_errors": False,
        "effective_severity": "bugfix",
    }
    base.update(overrides)
    return base


def _patched(settings):
    return (
        patch("app.notifications.db.get_notifications_enabled", return_value=settings["notifications_enabled"]),
        patch("app.notifications.db.get_feature_notify_enabled", return_value=settings["feature_notify_enabled"]),
        patch("app.notifications.db.get_notify_updates_include_errors", return_value=settings["notify_updates_include_errors"]),
        patch("app.notifications.db.get_effective_severity", return_value=settings["effective_severity"]),
    )


def test_master_toggle_off_suppresses_everything():
    with patch("app.notifications._send") as mock_send, \
         patch("app.notifications.db.get_notifications_enabled", return_value=False):
        notifications.notify_update("sonarr", "linuxserver/sonarr", "latest", 1, severity="breaking")
    mock_send.assert_not_called()


def test_feature_toggle_off_suppresses_everything():
    with patch("app.notifications._send") as mock_send, \
         patch("app.notifications.db.get_notifications_enabled", return_value=True), \
         patch("app.notifications.db.get_feature_notify_enabled", return_value=False):
        notifications.notify_update("sonarr", "linuxserver/sonarr", "latest", 1, severity="breaking")
    mock_send.assert_not_called()


def test_severity_meeting_threshold_sends():
    patches = _patched(_settings(effective_severity="feature"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_update("sonarr", "linuxserver/sonarr", "latest", 42, severity="breaking")
    mock_send.assert_called_once()
    title, body = mock_send.call_args[0]
    assert "sonarr" in title
    assert "/updates/42" in body


def test_severity_below_threshold_is_suppressed():
    patches = _patched(_settings(effective_severity="breaking"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_update("sonarr", "linuxserver/sonarr", "latest", 42, severity="feature")
    mock_send.assert_not_called()


def test_blank_severity_never_notifies_even_at_the_lowest_threshold():
    """A pending update whose AI summarization hasn't succeeded yet (e.g. Gemini quota-blocked)
    has nothing meaningful to compare against a threshold -- notify_update() must wait for a
    later check to backfill the real severity rather than firing early with an unknown one."""
    patches = _patched(_settings(effective_severity="bugfix"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_update("sonarr", "linuxserver/sonarr", "latest", 42, severity="")
    mock_send.assert_not_called()


def test_registry_error_is_suppressed_by_default():
    patches = _patched(_settings(notify_updates_include_errors=False))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_update(
                "sonarr", "linuxserver/sonarr", "latest", 42,
                error="Could not reach the registry to check for an update.",
            )
    mock_send.assert_not_called()


def test_registry_error_sends_when_opted_in_and_bypasses_the_severity_threshold():
    patches = _patched(_settings(notify_updates_include_errors=True, effective_severity="breaking"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_update(
                "sonarr", "linuxserver/sonarr", "latest", 42,
                error="Could not reach the registry to check for an update.",
            )
    mock_send.assert_called_once()
    title, body = mock_send.call_args[0]
    assert "sonarr" in title
    assert "Could not reach the registry" in body


def test_error_takes_priority_over_severity_when_both_are_present():
    """Shouldn't happen in practice (persist.py never sets both), but error must win rather
    than silently falling through to the severity-threshold branch."""
    patches = _patched(_settings(notify_updates_include_errors=True, effective_severity="bugfix"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_update(
                "sonarr", "linuxserver/sonarr", "latest", 42,
                severity="breaking", error="Could not reach the registry to check for an update.",
            )
    title, body = mock_send.call_args[0]
    assert "Could not reach the registry" in body

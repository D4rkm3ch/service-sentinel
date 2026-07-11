"""Direct unit tests for notifications.notify_findings_digest() -- Logs/Compose's counterpart to
notify_updates_digest, fires one Apprise call per severity level present among a check run's
genuinely new findings, matching Updates' batched-by-severity shape exactly. Mocks
app.notifications.db and app.notifications._send directly, per the established per-module
mocking pattern (see test_notify_updates_digest.py)."""

from unittest.mock import patch

from app import notifications
from app.notifications import NotifyType


def _settings(**overrides):
    base = {
        "notifications_enabled": True,
        "feature_notify_enabled": True,
        "effective_severity": "suggestion",
    }
    base.update(overrides)
    return base


def _patched(settings):
    return (
        patch("app.notifications.db.get_notifications_enabled", return_value=settings["notifications_enabled"]),
        patch("app.notifications.db.get_feature_notify_enabled", return_value=settings["feature_notify_enabled"]),
        patch("app.notifications.db.get_effective_severity", return_value=settings["effective_severity"]),
    )


def _item(subject="plex", severity="warning"):
    return {"subject": subject, "severity": severity}


def test_empty_items_never_call_send():
    with patch("app.notifications._send") as mock_send:
        notifications.notify_findings_digest("logs", [])
    mock_send.assert_not_called()


def test_master_toggle_off_suppresses_everything():
    with patch("app.notifications._send") as mock_send, \
         patch("app.notifications.db.get_notifications_enabled", return_value=False):
        notifications.notify_findings_digest("logs", [_item()])
    mock_send.assert_not_called()


def test_feature_toggle_off_suppresses_everything():
    with patch("app.notifications._send") as mock_send, \
         patch("app.notifications.db.get_notifications_enabled", return_value=True), \
         patch("app.notifications.db.get_feature_notify_enabled", return_value=False):
        notifications.notify_findings_digest("logs", [_item()])
    mock_send.assert_not_called()


def test_a_single_qualifying_item_sends_one_call():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2]:
            notifications.notify_findings_digest("logs", [_item(subject="plex", severity="critical")])
    mock_send.assert_called_once()
    title, body, notify_type = mock_send.call_args[0]
    assert title == "Log Issues"
    assert body.startswith("**Critical (1)**")
    assert "plex" in body
    assert notify_type == NotifyType.FAILURE


def test_compose_title_is_its_own_feature_name():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2]:
            notifications.notify_findings_digest("compose", [_item(subject="stack.yml", severity="warning")])
    title, body, _ = mock_send.call_args[0]
    assert title == "Compose Issues"
    assert body.startswith("**Warnings (1)**")
    assert "stack.yml" in body


def test_items_below_threshold_are_excluded_and_nothing_sends_if_none_qualify():
    patches = _patched(_settings(effective_severity="critical"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2]:
            notifications.notify_findings_digest("logs", [_item(severity="suggestion")])
    mock_send.assert_not_called()


def test_mixed_severities_send_one_call_each_lowest_severity_first():
    patches = _patched(_settings(effective_severity="suggestion"))
    items = [
        _item(subject="sonarr", severity="suggestion"),
        _item(subject="radarr", severity="critical"),
        _item(subject="plex", severity="warning"),
    ]
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2]:
            notifications.notify_findings_digest("logs", items)

    assert mock_send.call_count == 3
    calls = mock_send.call_args_list
    assert calls[0][0][0] == "Log Issues" and calls[1][0][0] == "Log Issues" and calls[2][0][0] == "Log Issues"
    assert "Suggestions" in calls[0][0][1]
    assert "sonarr" in calls[0][0][1]
    assert "Warnings" in calls[1][0][1]
    assert "plex" in calls[1][0][1]
    assert "Critical" in calls[2][0][1]
    assert "radarr" in calls[2][0][1]


def test_multiple_items_of_the_same_severity_share_one_call():
    patches = _patched(_settings())
    items = [_item(subject="zebra", severity="warning"), _item(subject="apple", severity="warning")]
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2]:
            notifications.notify_findings_digest("logs", items)
    mock_send.assert_called_once()
    title, body, _ = mock_send.call_args[0]
    assert title == "Log Issues"
    assert body.startswith("**Warnings (2)**")
    assert body.index("apple") < body.index("zebra")  # alphabetical within the group


def test_body_is_the_severity_line_then_just_names_no_link_no_category():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2]:
            notifications.notify_findings_digest("logs", [_item(subject="plex", severity="warning")])
    _, body, _ = mock_send.call_args[0]
    assert body.strip() == "**Warnings (1)**\n\n**plex**"


def test_no_emoji_anywhere():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2]:
            notifications.notify_findings_digest("logs", [_item(severity="critical")])
    title, body, _ = mock_send.call_args[0]
    for ch in title + body:
        assert not (0x1F300 <= ord(ch) <= 0x1FAFF), "no emoji anywhere, ever"

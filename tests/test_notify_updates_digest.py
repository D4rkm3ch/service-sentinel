"""Direct unit tests for notifications.notify_updates_digest() -- the single batched Apprise
call persist.py fires once per check run (see tests/test_persist_notifications.py for *when*
persist.py calls it; this file is about what the digest itself decides to do with a given
candidate list). Mocks app.notifications.db and app.notifications._send directly, per the
established per-module mocking pattern -- see tests/test_stage7_persist_summarization.py etc.
for prior art."""

from unittest.mock import patch

from app import notifications
from app.notifications import NotifyType


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


def _item(name="sonarr", severity="breaking", update_id=1, repo="owner/repo", tag="latest"):
    return {"container_name": name, "image_repo": repo, "tag": tag, "update_id": update_id, "severity": severity}


def _error(name="qbittorrent", update_id=2, repo="owner/repo", tag="latest", error="Could not reach the registry."):
    return {"container_name": name, "image_repo": repo, "tag": tag, "update_id": update_id, "error": error}


def test_empty_candidates_never_call_send():
    with patch("app.notifications._send") as mock_send:
        notifications.notify_updates_digest([], [])
    mock_send.assert_not_called()


def test_master_toggle_off_suppresses_everything():
    with patch("app.notifications._send") as mock_send, \
         patch("app.notifications.db.get_notifications_enabled", return_value=False):
        notifications.notify_updates_digest([_item()], [])
    mock_send.assert_not_called()


def test_feature_toggle_off_suppresses_everything():
    with patch("app.notifications._send") as mock_send, \
         patch("app.notifications.db.get_notifications_enabled", return_value=True), \
         patch("app.notifications.db.get_feature_notify_enabled", return_value=False):
        notifications.notify_updates_digest([_item()], [])
    mock_send.assert_not_called()


def test_a_single_qualifying_item_sends_once():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="breaking")], [])
    mock_send.assert_called_once()
    title, body, notify_type = mock_send.call_args[0]
    assert "1 update" in title
    assert "sonarr" in body
    assert "owner/repo:latest" in body
    assert "/updates/1" in body


def test_items_below_threshold_are_excluded_and_nothing_sends_if_none_qualify():
    patches = _patched(_settings(effective_severity="breaking"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="feature")], [])
    mock_send.assert_not_called()


def test_mixed_severities_only_qualifying_ones_appear_sorted_worst_first():
    patches = _patched(_settings(effective_severity="feature"))
    items = [
        _item(name="metube", severity="bugfix", update_id=1),  # below threshold, excluded
        _item(name="sonarr", severity="breaking", update_id=2),
        _item(name="bambuddy", severity="feature", update_id=3),
    ]
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest(items, [])
    title, body, notify_type = mock_send.call_args[0]
    assert "2 update" in title
    assert "metube" not in body
    assert body.index("sonarr") < body.index("bambuddy")  # worst severity first
    assert notify_type == NotifyType.FAILURE  # breaking present -> reddest available color


def test_registry_errors_excluded_by_default():
    patches = _patched(_settings(notify_updates_include_errors=False))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([], [_error()])
    mock_send.assert_not_called()


def test_registry_errors_included_when_opted_in_with_their_own_section():
    patches = _patched(_settings(notify_updates_include_errors=True))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([], [_error(name="qbittorrent", error="DNS lookup failed.")])
    mock_send.assert_called_once()
    title, body, notify_type = mock_send.call_args[0]
    assert "1 check error" in title
    assert "couldn't be checked" in body
    assert "qbittorrent" in body
    assert "DNS lookup failed." in body
    assert notify_type == NotifyType.FAILURE  # a check failure is treated as worth flagging


def test_errors_and_items_combine_into_one_digest_with_both_sections():
    patches = _patched(_settings(notify_updates_include_errors=True, effective_severity="bugfix"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="feature")], [_error()])
    mock_send.assert_called_once()
    title, body, notify_type = mock_send.call_args[0]
    assert "1 update" in title
    assert "1 check error" in title
    assert "sonarr" in body
    assert "couldn't be checked" in body


def test_body_includes_ansi_colored_severity_and_no_emoji():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="breaking")], [])
    _, body, _ = mock_send.call_args[0]
    assert "```ansi" in body
    assert "\x1b[1;31m" in body  # red, matching badge-sev-breaking
    assert "Breaking Change" in body
    for ch in body:
        assert not (0x1F300 <= ord(ch) <= 0x1FAFF), "no emoji anywhere, ever"


def test_body_links_to_each_update_and_to_the_updates_list():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(update_id=99)], [])
    _, body, _ = mock_send.call_args[0]
    assert "/updates/99" in body
    assert "[View all updates](/updates)" in body


def test_send_is_called_with_markdown_body_format_and_notify_type():
    with patch("app.notifications.db.get_apprise_urls", return_value=["discord://id/token/?format=markdown"]), \
         patch("app.notifications.apprise.Apprise") as mock_apprise_cls:
        mock_instance = mock_apprise_cls.return_value
        notifications._send("Title", "Body", NotifyType.WARNING)

    mock_instance.notify.assert_called_once()
    kwargs = mock_instance.notify.call_args.kwargs
    assert kwargs["notify_type"] == NotifyType.WARNING
    assert kwargs["body_format"] == notifications.NotifyFormat.MARKDOWN

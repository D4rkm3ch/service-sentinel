"""Direct unit tests for notifications.notify_updates_digest() -- fires one Apprise call per
severity level present in a check run (plus one more for check errors, if opted in), never one
call per update and never one call mixing severities together. See
tests/test_persist_notifications.py for *when* persist.py calls this; this file is about what
the digest itself decides to send for a given candidate list. Mocks app.notifications.db and
app.notifications._send directly, per the established per-module mocking pattern."""

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


def test_a_single_qualifying_item_sends_one_call():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="breaking")], [])
    mock_send.assert_called_once()
    title, body, notify_type = mock_send.call_args[0]
    assert title == "Update Issues"
    assert "Breaking Change (1)" in body
    assert "sonarr" in body
    assert "owner/repo" not in body and "latest" not in body  # just the name, no trailing image:tag
    assert notify_type == NotifyType.FAILURE


def test_items_below_threshold_are_excluded_and_nothing_sends_if_none_qualify():
    patches = _patched(_settings(effective_severity="breaking"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="feature")], [])
    mock_send.assert_not_called()


def test_mixed_severities_send_one_call_each_lowest_severity_first():
    patches = _patched(_settings(effective_severity="feature"))
    items = [
        _item(name="metube", severity="bugfix", update_id=1),  # below threshold, excluded entirely
        _item(name="sonarr", severity="breaking", update_id=2),
        _item(name="bambuddy", severity="feature", update_id=3),
    ]
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest(items, [])

    assert mock_send.call_count == 2  # one for feature, one for breaking -- bugfix excluded
    calls = mock_send.call_args_list
    # Every call's title is just the feature name -- severity + count live in the body instead.
    assert calls[0][0][0] == "Update Issues"
    assert calls[1][0][0] == "Update Issues"
    # feature (lowest of the two qualifying severities) sent first, breaking last/most recent.
    assert "New Features" in calls[0][0][1]
    assert "bambuddy" in calls[0][0][1]
    assert calls[0][0][2] == NotifyType.SUCCESS
    assert "Breaking Change" in calls[1][0][1]
    assert "sonarr" in calls[1][0][1]
    assert calls[1][0][2] == NotifyType.FAILURE
    # Neither message mixes the other severity's container in.
    assert "sonarr" not in calls[0][0][1]
    assert "bambuddy" not in calls[1][0][1]
    assert "metube" not in calls[0][0][1] and "metube" not in calls[1][0][1]


def test_multiple_items_of_the_same_severity_share_one_call():
    patches = _patched(_settings())
    items = [
        _item(name="zebra", severity="breaking", update_id=1),
        _item(name="apple", severity="breaking", update_id=2),
    ]
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest(items, [])
    mock_send.assert_called_once()
    title, body, _ = mock_send.call_args[0]
    assert title == "Update Issues"
    assert "Breaking Change (2)" in body
    assert body.index("apple") < body.index("zebra")  # alphabetical within the group
    assert "---" not in body  # a blank line separates items, not a horizontal rule


def test_registry_errors_excluded_by_default():
    patches = _patched(_settings(notify_updates_include_errors=False))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([], [_error()])
    mock_send.assert_not_called()


def test_registry_errors_included_when_opted_in_as_their_own_call():
    patches = _patched(_settings(notify_updates_include_errors=True))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([], [_error(name="qbittorrent", error="DNS lookup failed.")])
    mock_send.assert_called_once()
    title, body, notify_type = mock_send.call_args[0]
    assert title == "Check errors (1)"
    assert "qbittorrent" in body
    assert "DNS lookup failed." in body
    assert notify_type == NotifyType.FAILURE


def test_errors_and_a_severity_group_are_two_separate_calls_errors_first():
    patches = _patched(_settings(notify_updates_include_errors=True, effective_severity="bugfix"))
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="feature")], [_error()])

    assert mock_send.call_count == 2
    calls = mock_send.call_args_list
    assert "Check errors" in calls[0][0][0]
    assert calls[1][0][0] == "Update Issues"
    assert "New Features" in calls[1][0][1]
    assert "sonarr" not in calls[0][0][1]
    assert "qbittorrent" not in calls[1][0][1]


def test_no_emoji_anywhere():
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="breaking")], [])
    title, body, _ = mock_send.call_args[0]
    for ch in title + body:
        assert not (0x1F300 <= ord(ch) <= 0x1FAFF), "no emoji anywhere, ever"


def test_body_has_no_links_at_all():
    """Per-item [View](url) lines and the "View all updates" footer link were both dropped --
    with no PUBLIC_URL configured (the common case), these were relative paths Discord doesn't
    render as clickable at all, just dead markdown-looking text cluttering the message."""
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(update_id=99)], [])
    _, body, _ = mock_send.call_args[0]
    assert "/updates/99" not in body
    assert "[View" not in body


def test_title_is_just_the_feature_name_no_app_branding_prefix():
    """The webhook's own name (set directly in Discord, e.g. "Spidey Bot") already identifies
    the source -- repeating "release-radar" inside every message would just be noise. Severity
    and count live in the body's first line instead (Discord renders a title larger/bolder than
    the body, so "Update Issues" as the title / "Breaking Change (1)" as the first body line
    reads as a big line + a smaller line under it, without a second Apprise call)."""
    patches = _patched(_settings())
    with patch("app.notifications._send") as mock_send:
        with patches[0], patches[1], patches[2], patches[3]:
            notifications.notify_updates_digest([_item(severity="breaking")], [])
    title, body, _ = mock_send.call_args[0]
    assert title == "Update Issues"
    assert body.startswith("**Breaking Change (1)**")


def test_send_uses_an_asset_that_strips_apprises_own_branding():
    with patch("app.notifications.db.get_apprise_urls", return_value=["discord://id/token/?format=markdown"]), \
         patch("app.notifications.apprise.Apprise") as mock_apprise_cls:
        notifications._send("Title", "Body", NotifyType.WARNING)

    mock_apprise_cls.assert_called_once_with(asset=notifications._ASSET)
    assert notifications._ASSET.app_id == ""
    assert notifications._ASSET.image_url_mask == ""
    assert notifications._ASSET.image_url_logo == ""


def test_asset_suppresses_apprises_branded_avatar_from_the_real_payload():
    """End-to-end proof (not just checking the asset's attributes): with this asset, Apprise's
    Discord plugin must never put its own branded icon URL in the outbound payload at all, so
    Discord falls back to the target webhook's own configured avatar."""
    import json
    from unittest.mock import MagicMock

    captured = []

    def fake_post(url, **kwargs):
        captured.append(kwargs.get("data"))
        resp = MagicMock()
        resp.status_code = 204
        resp.content = b""
        return resp

    with patch("requests.post", side_effect=fake_post), \
         patch("app.notifications.db.get_apprise_urls", return_value=["discord://123/abc/?format=markdown"]):
        notifications._send("Breaking Change (1)", "**sonarr**", NotifyType.FAILURE)

    assert len(captured) == 1
    payload = json.loads(captured[0])
    assert "avatar_url" not in payload
    assert payload["embeds"][0]["author"]["name"] == ""


def test_embed_colors_match_the_dashboards_own_severity_colors():
    """Discord's embed accent bar must match the same severity's badge color on the dashboard
    (style.css's --text-dim/--accent/--warn/--error), not Apprise's generic blue/green/yellow/red
    defaults -- see _ASSET's html_notify_map override."""
    import json
    from unittest.mock import MagicMock

    expected = {
        NotifyType.INFO: 0x868C98,     # --text-dim
        NotifyType.SUCCESS: 0x5EC9A6,  # --accent
        NotifyType.WARNING: 0xD9A441,  # --warn
        NotifyType.FAILURE: 0xD9705E,  # --error
    }
    for notify_type, expected_color in expected.items():
        captured = []

        def fake_post(url, **kwargs):
            captured.append(kwargs.get("data"))
            resp = MagicMock()
            resp.status_code = 204
            resp.content = b""
            return resp

        with patch("requests.post", side_effect=fake_post), \
             patch("app.notifications.db.get_apprise_urls", return_value=["discord://123/abc/?format=markdown"]):
            notifications._send("Updates", "**Some Severity (1)**", notify_type)

        payload = json.loads(captured[0])
        assert payload["embeds"][0]["color"] == expected_color, f"{notify_type} should render as {expected_color:#x}"


def test_send_is_called_with_markdown_body_format_and_notify_type():
    with patch("app.notifications.db.get_apprise_urls", return_value=["discord://id/token/?format=markdown"]), \
         patch("app.notifications.apprise.Apprise") as mock_apprise_cls:
        mock_instance = mock_apprise_cls.return_value
        notifications._send("Title", "Body", NotifyType.WARNING)

    mock_instance.notify.assert_called_once()
    kwargs = mock_instance.notify.call_args.kwargs
    assert kwargs["notify_type"] == NotifyType.WARNING
    assert kwargs["body_format"] == notifications.NotifyFormat.MARKDOWN

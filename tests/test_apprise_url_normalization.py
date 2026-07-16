"""Apprise URLs are saved exactly as typed, with no special-casing of any one service. An
earlier version auto-appended `?format=markdown` to a bare discord:// URL (see notifications.py's
own module docstring for why that query string matters to Discord specifically) -- removed per a
real-world report that special-casing Discord in both the UI copy and the actual saved value
read as hamstringing anyone using a different Apprise-supported service."""

from unittest.mock import patch

from app import db


def test_a_discord_url_is_saved_exactly_as_typed(client):
    with patch("app.main.send_test_notification", return_value=(True, "ok")) as mock_test:
        resp = client.post("/settings/notify/apprise-test", data={
            "apprise_urls": "discord://id/token",
        })
    assert resp.status_code == 200
    mock_test.assert_called_once_with(urls=["discord://id/token"])
    assert db.get_apprise_urls() == ["discord://id/token"]


def test_a_discord_url_with_format_markdown_typed_manually_is_preserved(client):
    with patch("app.main.send_test_notification", return_value=(True, "ok")) as mock_test:
        client.post("/settings/notify/apprise-test", data={
            "apprise_urls": "discord://id/token?format=markdown",
        })
    mock_test.assert_called_once_with(urls=["discord://id/token?format=markdown"])
    assert db.get_apprise_urls() == ["discord://id/token?format=markdown"]


def test_a_non_discord_url_is_saved_exactly_as_typed(client):
    with patch("app.main.send_test_notification", return_value=(True, "ok")) as mock_test:
        client.post("/settings/notify/apprise-test", data={
            "apprise_urls": "slack://token/channel",
        })
    mock_test.assert_called_once_with(urls=["slack://token/channel"])
    assert db.get_apprise_urls() == ["slack://token/channel"]


def test_multiple_comma_separated_urls_are_each_preserved(client):
    with patch("app.main.send_test_notification", return_value=(True, "ok")) as mock_test:
        client.post("/settings/notify/apprise-test", data={
            "apprise_urls": "discord://id/token, slack://token/channel",
        })
    mock_test.assert_called_once_with(urls=["discord://id/token", "slack://token/channel"])


def test_a_failed_test_never_saves_anything(client):
    db.set_apprise_urls("")
    with patch("app.main.send_test_notification", return_value=(False, "nope")):
        client.post("/settings/notify/apprise-test", data={"apprise_urls": "discord://id/token"})
    assert db.get_apprise_urls() == []

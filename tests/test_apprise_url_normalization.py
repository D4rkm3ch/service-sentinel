"""A Discord webhook URL needs ?format=markdown for Apprise to build a colored embed at all --
without it, Discord falls back to a flat plain-text message with no severity color (see
notifications.py's module docstring). Users used to have to remember to type that themselves;
per a real-world request it's now appended automatically, the same way an email signup form
fills in "@example.com" after whatever you type -- covers that the saved value (and the exact
value tested) both carry it, and that a URL for another Apprise-supported service, or a Discord
URL that already has its own query string, is left untouched."""

from unittest.mock import patch

from app import db


def test_a_bare_discord_url_gets_format_markdown_appended_on_save(client):
    with patch("app.main.send_test_notification", return_value=(True, "ok")) as mock_test:
        resp = client.post("/settings/notify/apprise-test", data={
            "apprise_urls": "discord://id/token",
        })
    assert resp.status_code == 200
    mock_test.assert_called_once_with(urls=["discord://id/token?format=markdown"])
    assert db.get_apprise_urls() == ["discord://id/token?format=markdown"]


def test_a_discord_url_with_its_own_query_string_is_left_alone(client):
    with patch("app.main.send_test_notification", return_value=(True, "ok")) as mock_test:
        client.post("/settings/notify/apprise-test", data={
            "apprise_urls": "discord://id/token?avatar=no",
        })
    mock_test.assert_called_once_with(urls=["discord://id/token?avatar=no"])
    assert db.get_apprise_urls() == ["discord://id/token?avatar=no"]


def test_a_non_discord_url_is_never_touched(client):
    with patch("app.main.send_test_notification", return_value=(True, "ok")) as mock_test:
        client.post("/settings/notify/apprise-test", data={
            "apprise_urls": "slack://token/channel",
        })
    mock_test.assert_called_once_with(urls=["slack://token/channel"])
    assert db.get_apprise_urls() == ["slack://token/channel"]


def test_multiple_comma_separated_urls_are_each_normalized_independently(client):
    with patch("app.main.send_test_notification", return_value=(True, "ok")) as mock_test:
        client.post("/settings/notify/apprise-test", data={
            "apprise_urls": "discord://id/token, slack://token/channel",
        })
    mock_test.assert_called_once_with(urls=["discord://id/token?format=markdown", "slack://token/channel"])


def test_a_failed_test_never_saves_anything(client):
    db.set_apprise_urls("")
    with patch("app.main.send_test_notification", return_value=(False, "nope")):
        client.post("/settings/notify/apprise-test", data={"apprise_urls": "discord://id/token"})
    assert db.get_apprise_urls() == []

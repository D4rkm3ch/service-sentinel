"""Security hardening: Discord parses @everyone/@here and <@id>/<@&id>/<#id> mention syntax in
plain webhook message content, not just in embeds (security_hardening_plan.md finding #9).
notifications._send_finding_severity_group builds a notification body directly from an
AI-generated finding title -- if a compromised or misbehaving container wrote one of these into
its own log output, and the AI echoed enough of it back verbatim into a finding title, that text
would be sent as-is inside a real Discord webhook message and ping the whole channel (or a real
role/user if the id happens to be one).

Fixed by notifications._defuse_discord_mentions(), applied to every _send() call's title and
body uniformly (not just the finding path), breaking the exact-match pattern Discord's parser
needs with an invisible zero-width space rather than a visible character, so the text still
reads identically to a human -- verified below by asserting the *visible* text (mentions
stripped of the zero-width space) is unchanged."""

from unittest.mock import MagicMock, patch

from app import notifications
from app.notifications import _defuse_discord_mentions


def _visible(text: str) -> str:
    return text.replace("\u200b", "")


# ---------------------------------------------------------------------------
# _defuse_discord_mentions itself
# ---------------------------------------------------------------------------

def test_at_everyone_is_defused():
    result = _defuse_discord_mentions("Warning: @everyone should see this")
    assert "@everyone" not in result
    assert "\u200b" in result
    assert _visible(result) == "Warning: @everyone should see this"


def test_at_here_is_defused():
    result = _defuse_discord_mentions("@here check this out")
    assert "@here" not in result
    assert _visible(result) == "@here check this out"


def test_user_mention_is_defused():
    result = _defuse_discord_mentions("Ping <@123456789012345678> about this")
    assert "<@123456789012345678>" not in result
    assert _visible(result) == "Ping <@123456789012345678> about this"


def test_nickname_mention_is_defused():
    result = _defuse_discord_mentions("Ping <@!123456789012345678> about this")
    assert "<@!123456789012345678>" not in result
    assert _visible(result) == "Ping <@!123456789012345678> about this"


def test_role_mention_is_defused():
    result = _defuse_discord_mentions("Attention <@&987654321098765432> team")
    assert "<@&987654321098765432>" not in result
    assert _visible(result) == "Attention <@&987654321098765432> team"


def test_channel_mention_is_defused():
    result = _defuse_discord_mentions("See <#111222333444555666> for details")
    assert "<#111222333444555666>" not in result
    assert _visible(result) == "See <#111222333444555666> for details"


def test_multiple_mentions_in_one_string_are_all_defused():
    result = _defuse_discord_mentions("@everyone <@123456789012345678> @here")
    assert "@everyone" not in result
    assert "@here" not in result
    assert "<@123456789012345678>" not in result
    assert _visible(result) == "@everyone <@123456789012345678> @here"


def test_ordinary_text_with_at_sign_is_untouched():
    """A container name or email-shaped string with a plain '@' that isn't Discord mention
    syntax must not be mangled."""
    result = _defuse_discord_mentions("user@example.com reported an issue")
    assert result == "user@example.com reported an issue"


def test_text_with_no_mentions_is_returned_unchanged():
    result = _defuse_discord_mentions("Missing healthcheck on plex")
    assert result == "Missing healthcheck on plex"


# ---------------------------------------------------------------------------
# Wired into _send() itself, not just available as a standalone helper
# ---------------------------------------------------------------------------

def test_send_defuses_mentions_in_title_and_body():
    with patch("app.notifications.db.get_apprise_urls", return_value=["discord://id/token"]), \
         patch("app.notifications.apprise.Apprise") as mock_apprise_cls:
        mock_instance = MagicMock()
        mock_apprise_cls.return_value = mock_instance

        notifications._send("@everyone Alert", "Ping <@123456789012345678> now", notifications.NotifyType.WARNING)

    assert mock_instance.notify.called
    _, kwargs = mock_instance.notify.call_args
    assert "@everyone" not in kwargs["title"]
    assert "<@123456789012345678>" not in kwargs["body"]
    assert _visible(kwargs["title"]) == "@everyone Alert"
    assert _visible(kwargs["body"]) == "Ping <@123456789012345678> now"


def test_finding_title_containing_a_mention_is_defused_end_to_end():
    """The exact scenario the finding describes: an AI-generated finding title containing
    Discord mention syntax, reaching _send_finding_severity_group -> _send -> Apprise."""
    with patch("app.notifications.db.get_apprise_urls", return_value=["discord://id/token"]), \
         patch("app.notifications.apprise.Apprise") as mock_apprise_cls:
        mock_instance = MagicMock()
        mock_apprise_cls.return_value = mock_instance

        notifications._send_finding_severity_group(
            "logs", "critical",
            [{"subject": "plex", "title": "@everyone the container is down"}],
        )

    _, kwargs = mock_instance.notify.call_args
    assert "@everyone" not in kwargs["body"]
    assert "plex" in kwargs["body"]
    assert "the container is down" in kwargs["body"]

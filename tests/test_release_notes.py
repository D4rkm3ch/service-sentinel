"""Stage 6: real release notes, no AI, no web search. get_release_notes() must never reach
_web_search_release_notes() -- that's Stage 8's job, tested completely alone. Every test here
mocks httpx directly rather than hitting real registries/GitHub, since the point is proving
the priority order and caching behavior, not network connectivity (already covered by
test_image_ref_parsing.py / registry.py's own tests for the registry side)."""

from unittest.mock import MagicMock, patch

from app import release_notes


def _github_response(status_code=200, body="Fixed a bug", html_url="https://github.com/owner/repo/releases/tag/v1"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"body": body, "html_url": html_url}
    return resp


def test_changelog_url_override_takes_priority_over_everything():
    with patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes.db.get_release_notes_source") as mock_cache:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        resp = MagicMock(status_code=200, text="raw changelog text")
        resp.raise_for_status.return_value = None
        mock_client.get.return_value = resp

        notes, url = release_notes.get_release_notes(
            "owner/repo", "latest", changelog_url_override="https://example.com/CHANGELOG.md",
        )

    assert notes == "raw changelog text"
    assert url == "https://example.com/CHANGELOG.md"
    mock_cache.assert_not_called()  # never even checked the cache -- override wins outright


def test_cached_github_source_is_tried_first_and_skips_guessing():
    with patch("app.release_notes.db.get_release_notes_source", return_value={"method": "github", "location": "owner/cached-repo"}), \
         patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes._guess_github_repos") as mock_guess:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.get.return_value = _github_response()

        notes, url = release_notes.get_release_notes("owner/repo", "v1.0.0")

    assert notes == "Fixed a bug"
    mock_guess.assert_not_called()


def test_cached_source_that_stops_working_falls_through_to_fresh_discovery():
    with patch("app.release_notes.db.get_release_notes_source", return_value={"method": "github", "location": "owner/moved-repo"}), \
         patch("app.release_notes.db.set_release_notes_source") as mock_set_cache, \
         patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes._guess_github_repos", return_value=["owner/new-repo"]):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        # The cached repo 404s on every candidate tag AND the "latest release" fallback;
        # the freshly-guessed repo succeeds.
        not_found = MagicMock(status_code=404)
        mock_client.get.side_effect = [not_found, not_found, not_found, not_found, _github_response()]

        notes, url = release_notes.get_release_notes("owner/repo", "v1.0.0")

    assert notes == "Fixed a bug"
    mock_set_cache.assert_called_once_with("owner/repo", "github", "owner/new-repo")


def test_source_override_is_tried_before_naming_convention_guesses():
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes.db.set_release_notes_source"), \
         patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes._guess_github_repos", return_value=["guessed/repo"]):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.get.return_value = _github_response()

        release_notes.get_release_notes("owner/repo", "v1.0.0", source_override="manual/override")

    # Only one GitHub repo was ever actually fetched -- the override -- since it succeeded
    # on the first try and the loop returns immediately without reaching the guessed repo.
    assert mock_client.get.call_count == 1
    assert "manual/override" in mock_client.get.call_args[0][0]


def test_web_search_is_never_called_stage_6_scope():
    """The whole point of this stage: even when every direct source fails, get_release_notes()
    must fall straight to the Docker Hub last resort, never reaching the web search fallback."""
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes._guess_github_repos", return_value=[]), \
         patch("app.release_notes._web_search_release_notes") as mock_web_search:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = MagicMock(status_code=404)

        notes, url = release_notes.get_release_notes("somenamespace/someimage", "latest")

    mock_web_search.assert_not_called()
    assert notes is None
    assert url == "https://hub.docker.com/r/somenamespace/someimage/tags"


def test_absolute_last_resort_skipped_for_ghcr_images():
    """ghcr.io images have no Docker Hub tags page to fall back to -- must return (None, None)
    rather than a link that doesn't correspond to the actual image."""
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes._guess_github_repos", return_value=["owner/repo"]), \
         patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = MagicMock(status_code=404)

        notes, url = release_notes.get_release_notes("ghcr.io/owner/repo", "latest")

    assert (notes, url) == (None, None)


def test_guess_github_repos_ghcr():
    assert release_notes._guess_github_repos("ghcr.io/owner/repo") == ["owner/repo"]


def test_guess_github_repos_linuxserver():
    assert release_notes._guess_github_repos("lscr.io/linuxserver/sonarr") == [
        "linuxserver/docker-sonarr", "linuxserver/sonarr",
    ]
    assert release_notes._guess_github_repos("linuxserver/radarr") == [
        "linuxserver/docker-radarr", "linuxserver/radarr",
    ]


def test_guess_github_repos_plain_dockerhub_two_part():
    assert release_notes._guess_github_repos("qmcgaw/gluetun") == ["qmcgaw/gluetun"]


def test_guess_github_repos_returns_nothing_for_unnamespaced_official_images():
    assert release_notes._guess_github_repos("postgres") == []
    assert release_notes._guess_github_repos("library/postgres") == []


def test_successful_guess_caches_the_source():
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes.db.set_release_notes_source") as mock_set_cache, \
         patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = _github_response()

        release_notes.get_release_notes("qmcgaw/gluetun", "latest")

    mock_set_cache.assert_called_once_with("qmcgaw/gluetun", "github", "qmcgaw/gluetun")

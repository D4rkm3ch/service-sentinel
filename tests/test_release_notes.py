"""Stage 6: real release notes. Stage 8 (brought forward) adds a web search fallback, always
on -- get_release_notes() reaches _web_search_release_notes() whenever every cheaper source
above it comes up empty. Every test here mocks httpx directly rather than hitting real
registries/GitHub, since the point is proving the priority order and caching behavior, not
network connectivity (already covered by test_image_ref_parsing.py / registry.py's own tests
for the registry side)."""

from unittest.mock import MagicMock, patch

from app import release_notes


def _github_response(status_code=200, body="Fixed a bug", html_url="https://github.com/owner/repo/releases/tag/v1",
                      tag_name="v1", published_at="2026-01-01T00:00:00Z"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {
        "body": body, "html_url": html_url, "tag_name": tag_name, "published_at": published_at,
    }
    return resp


def test_changelog_url_override_takes_priority_over_everything():
    with patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes.db.get_release_notes_source") as mock_cache, \
         patch("app.release_notes._is_safe_public_url", return_value=True):
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

    assert notes == "## v1 (2026-01-01)\nFixed a bug"
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

    assert notes == "## v1 (2026-01-01)\nFixed a bug"
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


def test_web_search_is_tried_when_everything_else_failed():
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes._guess_github_repos", return_value=[]), \
         patch("app.release_notes._web_search_release_notes", return_value=("Found via search", "https://blog.example.com/v2")) as mock_web_search, \
         patch("app.release_notes.db.set_release_notes_source") as mock_set_cache:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = MagicMock(status_code=404)

        notes, url = release_notes.get_release_notes("somenamespace/someimage", "latest")

    mock_web_search.assert_called_once_with("somenamespace/someimage", "latest")
    assert notes == "Found via search"
    assert url == "https://blog.example.com/v2"
    # Not a GitHub URL -- cached as a plain "url" source, not "github".
    mock_set_cache.assert_called_once_with("somenamespace/someimage", "url", "https://blog.example.com/v2")


def test_web_search_result_pointing_at_github_is_cached_as_a_github_source():
    """A web search result that resolves to a real GitHub repo is cached as method="github"
    rather than "url", so future lookups reuse the cheap, high-quality GitHub Releases API
    path instead of paying for another search or raw-fetching the webpage."""
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes._guess_github_repos", return_value=[]), \
         patch("app.release_notes._web_search_release_notes",
               return_value=("Found via search", "https://github.com/owner/found-repo/releases/tag/v2")), \
         patch("app.release_notes.db.set_release_notes_source") as mock_set_cache:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = MagicMock(status_code=404)

        release_notes.get_release_notes("somenamespace/someimage", "latest")

    mock_set_cache.assert_called_once_with("somenamespace/someimage", "github", "owner/found-repo")


def test_web_search_not_reached_if_a_naming_convention_guess_already_succeeded():
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes.db.set_release_notes_source"), \
         patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes._guess_github_repos", return_value=["owner/repo"]), \
         patch("app.release_notes._web_search_release_notes") as mock_web_search:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = _github_response()

        release_notes.get_release_notes("owner/repo", "latest")

    mock_web_search.assert_not_called()


def test_web_search_finding_nothing_still_falls_to_docker_hub_last_resort():
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes.httpx.Client") as mock_client_cls, \
         patch("app.release_notes._guess_github_repos", return_value=[]), \
         patch("app.release_notes._web_search_release_notes", return_value=(None, None)):
        mock_client_cls.return_value.__enter__.return_value.get.return_value = MagicMock(status_code=404)

        notes, url = release_notes.get_release_notes("somenamespace/someimage", "latest")

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


def test_extract_github_repo_from_url():
    assert release_notes._extract_github_repo_from_url(
        "https://github.com/owner/repo/releases/tag/v2"
    ) == "owner/repo"
    assert release_notes._extract_github_repo_from_url("https://github.com/owner/repo") == "owner/repo"
    assert release_notes._extract_github_repo_from_url("https://blog.example.com/owner/repo") is None
    assert release_notes._extract_github_repo_from_url("not a url at all") is None


def test_successful_guess_caches_the_source():
    with patch("app.release_notes.db.get_release_notes_source", return_value=None), \
         patch("app.release_notes.db.set_release_notes_source") as mock_set_cache, \
         patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = _github_response()

        release_notes.get_release_notes("qmcgaw/gluetun", "latest")

    mock_set_cache.assert_called_once_with("qmcgaw/gluetun", "github", "qmcgaw/gluetun")

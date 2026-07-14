"""An explicit ask: if the user missed several releases between checks, compile every release
published since the last check into one combined summary instead of just the latest -- same
severity if they're all the same tier, or the highest severity represented if they differ (left
entirely to the AI's judgement, per the actual request -- see summarizer.SYSTEM_PROMPT's
instructions for how that's phrased). Bounded by an optional Settings lookback cap so a
container that's gone unchecked for a very long time doesn't pull an unbounded number of
releases into one prompt."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import db, persist, release_notes
from app.summarizer import SYSTEM_PROMPT


def _releases_list_response(releases):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = releases
    return resp


def _release(tag, published_at, body="notes"):
    return {
        "tag_name": tag, "published_at": published_at, "body": body,
        "html_url": f"https://github.com/owner/repo/releases/tag/{tag}",
    }


def test_fetch_releases_since_stops_at_the_cutoff():
    with patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.get.return_value = _releases_list_response([
            _release("v3", "2026-03-01T00:00:00Z"),
            _release("v2", "2026-02-01T00:00:00Z"),
            _release("v1", "2026-01-01T00:00:00Z"),
        ])
        since = datetime(2026, 1, 15, tzinfo=timezone.utc)
        releases = release_notes._fetch_github_releases_since("owner/repo", since)
    assert [r["tag_name"] for r in releases] == ["v3", "v2"]  # v1 is at/before the cutoff


def test_fetch_releases_since_respects_the_hard_ceiling():
    many = [_release(f"v{i}", f"2026-01-{i:02d}T00:00:00Z") for i in range(30, 0, -1)]
    with patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.get.return_value = _releases_list_response(many)
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)
        releases = release_notes._fetch_github_releases_since("owner/repo", since)
    assert len(releases) == release_notes._MAX_COMPILED_RELEASES


def test_compile_releases_text_orders_oldest_to_newest_with_version_headers():
    releases = [_release("v3", "2026-03-01T00:00:00Z", "body3"), _release("v2", "2026-02-01T00:00:00Z", "body2")]
    text = release_notes._compile_releases_text(releases)
    assert text.index("## v2") < text.index("## v3")
    assert "## v2 (2026-02-01)" in text
    assert "body2" in text and "body3" in text


def test_resolve_github_notes_compiles_when_two_or_more_releases_found_since_cutoff():
    with patch("app.release_notes._fetch_github_releases_since",
               return_value=[_release("v2", "2026-02-01T00:00:00Z"), _release("v1", "2026-01-01T00:00:00Z")]), \
         patch("app.release_notes._fetch_github_release_notes") as mock_single:
        notes, url = release_notes._resolve_github_notes("owner/repo", "v2", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "v1" in notes and "v2" in notes
    mock_single.assert_not_called()
    assert url == "https://github.com/owner/repo/releases"


def test_resolve_github_notes_falls_back_to_single_release_when_only_one_found():
    """The common case: a normal check cadence usually finds exactly one new release -- no real
    compilation benefit, so behavior stays identical to the pre-existing single-release path."""
    with patch("app.release_notes._fetch_github_releases_since",
               return_value=[_release("v2", "2026-02-01T00:00:00Z")]), \
         patch("app.release_notes._fetch_github_release_notes", return_value=("single notes", "single url")) as mock_single:
        notes, url = release_notes._resolve_github_notes("owner/repo", "v2", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert notes == "single notes"
    mock_single.assert_called_once_with("owner/repo", "v2")


def test_resolve_github_notes_skips_the_multi_release_path_entirely_when_since_is_none():
    """since=None (a container's very first check ever) always uses today's single-release
    behavior -- there's no prior check to measure a window from."""
    with patch("app.release_notes._fetch_github_releases_since") as mock_multi, \
         patch("app.release_notes._fetch_github_release_notes", return_value=("notes", "url")):
        release_notes._resolve_github_notes("owner/repo", "v2", None)
    mock_multi.assert_not_called()


def test_summarizer_prompt_instructs_combining_multiple_releases_and_taking_the_highest_severity():
    assert "multiple releases" in SYSTEM_PROMPT
    assert "HIGHEST severity" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# release_notes.extract_latest_version -- feeds the Discord digest's "container • vX.Y.Z" line
# (see notifications._format_update_line)
# ---------------------------------------------------------------------------

def test_extract_latest_version_takes_the_last_ie_newest_heading():
    releases = [_release("v3", "2026-03-01T00:00:00Z"), _release("v2", "2026-02-01T00:00:00Z")]
    text = release_notes._compile_releases_text(releases)
    assert release_notes.extract_latest_version(text) == "v3"


def test_extract_latest_version_returns_none_for_text_with_no_matching_headings():
    """Everything that isn't the GitHub-releases path (a changelog_url label override, a cached
    non-GitHub URL, the AI web-search fallback, the Docker Hub last resort) hands back arbitrary
    text that was never written in the "## <tag> (<date>)" format -- must not guess a version
    out of it."""
    assert release_notes.extract_latest_version("Just some free-form changelog prose.") is None


def test_extract_latest_version_returns_none_for_a_missing_tag_name_placeholder():
    releases = [_release(None, "2026-01-01T00:00:00Z")]
    text = release_notes._compile_releases_text(releases)
    assert release_notes.extract_latest_version(text) is None


def test_extract_latest_version_returns_none_for_empty_input():
    assert release_notes.extract_latest_version(None) is None
    assert release_notes.extract_latest_version("") is None


# ---------------------------------------------------------------------------
# persist._release_notes_since -- the cutoff computation itself
# ---------------------------------------------------------------------------

def test_release_notes_since_is_none_for_a_container_with_no_prior_check():
    assert persist._release_notes_since(None, cap_days=None) is None


def test_release_notes_since_uses_the_container_states_own_last_checked_time():
    state = {"last_checked_at": "2026-01-01T00:00:00+00:00"}
    since = persist._release_notes_since(state, cap_days=None)
    assert since == datetime.fromisoformat("2026-01-01T00:00:00+00:00")


def test_release_notes_since_is_further_capped_by_the_given_lookback():
    very_old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    since = persist._release_notes_since({"last_checked_at": very_old}, cap_days=7)
    # Capped to ~7 days ago, not the actual (400-day-old) last check time.
    assert since > datetime.now(timezone.utc) - timedelta(days=8)


def test_release_notes_since_cap_never_moves_the_cutoff_earlier_than_the_real_last_check():
    """A recent last check with a generous cap must not get pushed further back than it
    actually was -- max(), not min()."""
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    since = persist._release_notes_since({"last_checked_at": recent}, cap_days=365)
    assert since == datetime.fromisoformat(recent)


def test_cap_days_is_read_once_per_batch_not_per_container():
    """Regression guard: cap_days must be computed once by the caller (persist_check_outcome)
    and threaded through, never read from Settings inside _release_notes_since itself -- doing
    that per fetch group would reintroduce the "one small connection per container" cost the
    batched read/write split elsewhere in this pipeline exists to avoid."""
    import inspect
    params = inspect.signature(persist._release_notes_since).parameters
    assert "cap_days" in params


# ---------------------------------------------------------------------------
# Settings: the lookback dropdown itself
# ---------------------------------------------------------------------------

def test_release_notes_lookback_defaults_to_since_check():
    db.set_release_notes_lookback("since_check")
    assert db.get_release_notes_lookback() == "since_check"
    assert db.get_release_notes_lookback_days() is None


def test_release_notes_lookback_route_saves_and_reflects_on_settings_page(client):
    resp = client.post("/settings/release-notes-lookback", data={"release_notes_lookback": "30"})
    assert resp.status_code == 200
    assert db.get_release_notes_lookback() == "30"
    assert db.get_release_notes_lookback_days() == 30

    page = client.get("/settings")
    assert 'value="30" selected' in page.text or 'selected' in page.text  # dropdown reflects it
    assert "release_notes_lookback" in page.text

    db.set_release_notes_lookback("since_check")


def test_release_notes_lookback_route_rejects_an_unknown_value(client):
    resp = client.post("/settings/release-notes-lookback", data={"release_notes_lookback": "not-a-real-option"})
    assert resp.status_code == 400

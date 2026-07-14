"""A real-world report: opening a stack/service page could take a very long time. Root cause --
_get_or_build_overview() used to call the AI provider synchronously, right inside the GET route,
whenever a subject's findings hash no longer matched its cached overview (which happens after
every check that touches that subject, i.e. often). Fixed by serving the stale-but-present cached
overview immediately and refreshing it in a background thread instead -- the page only still
blocks on the very first view of a subject that has never had an overview generated at all."""

import time
from unittest.mock import patch

from app import db
from app.main import _get_or_build_overview

db.init_db()


def _wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_stale_cached_overview_is_served_immediately_and_refreshed_in_background():
    source, subject = "logs", "stale-overview-subject"
    findings = [
        {"id": 1, "title": "a", "status": "active"},
        {"id": 2, "title": "b", "status": "active"},
    ]
    db.set_subject_summary(source, subject, "stale-hash-does-not-match", "Stale overview text.")

    with patch("app.main.summarize_findings_overview", return_value="Fresh overview text."):
        result = _get_or_build_overview(source, subject, subject, findings)

    # The stale cached text comes back immediately -- not a value that depended on the (mocked,
    # but conceptually slow) AI call having already completed.
    assert result == "Stale overview text."

    assert _wait_until(lambda: db.get_subject_summary(source, subject)["summary_markdown"] == "Fresh overview text.")


def test_first_ever_view_with_no_cache_still_blocks_and_returns_the_fresh_result():
    """No prior cache means there's nothing stale to serve while a background refresh catches
    up -- this one case still calls the AI provider inline, same as before."""
    source, subject = "logs", "never-cached-overview-subject"
    findings = [
        {"id": 1, "title": "a", "status": "active"},
        {"id": 2, "title": "b", "status": "active"},
    ]
    with patch("app.main.summarize_findings_overview", return_value="First ever overview.") as mock_overview:
        result = _get_or_build_overview(source, subject, subject, findings)

    mock_overview.assert_called_once()
    assert result == "First ever overview."


def test_force_still_blocks_even_with_a_cache_present():
    """force=True (the explicit Regenerate buttons) keeps its existing synchronous "wait for a
    fresh take" behavior regardless of whether a cache exists."""
    source, subject = "logs", "force-regenerate-overview-subject"
    findings = [
        {"id": 1, "title": "a", "status": "active"},
        {"id": 2, "title": "b", "status": "active"},
    ]
    db.set_subject_summary(source, subject, "some-hash", "Old overview.")

    with patch("app.main.summarize_findings_overview", return_value="Forced fresh overview.") as mock_overview:
        result = _get_or_build_overview(source, subject, subject, findings, force=True)

    mock_overview.assert_called_once()
    assert result == "Forced fresh overview."

"""Branding invariants: the app is Service Sentinel, and nothing anywhere -- labels, database
filename, page chrome -- references any other name. (An earlier pre-release working name had
compatibility fallbacks here; since the app was never published under it, that code was removed
outright rather than carried as dead weight, and these tests now assert the absence.)"""

from pathlib import Path

from app.config import settings
from app.docker_client import (
    CHANGELOG_LABEL,
    IGNORE_LABEL,
    LOGS_IGNORE_LABEL,
    SOURCE_LABEL,
    TrackedContainer,
)

ROOT = Path(__file__).resolve().parent.parent


def test_label_prefix_is_servicesentinel():
    assert IGNORE_LABEL == "servicesentinel.ignore"
    assert LOGS_IGNORE_LABEL == "servicesentinel.logs.ignore"
    assert SOURCE_LABEL == "servicesentinel.source"
    assert CHANGELOG_LABEL == "servicesentinel.changelog_url"


def test_container_with_no_override_labels_returns_none_not_empty_string():
    c = TrackedContainer(name="x", image_repo="owner/repo", tag="latest", current_digest=None, labels={})
    assert c.source_override is None
    assert c.changelog_url_override is None
    assert c.logs_ignored is False


def test_db_path_uses_service_sentinel_filename():
    assert settings.db_path.name == "service_sentinel.db"


def test_no_legacy_name_references_remain_in_app_code():
    """The pre-release working name must not survive anywhere in the shipped app -- not as
    label fallbacks, not as a database-filename migration, not in templates."""
    app_dir = ROOT / "app"
    for path in app_dir.rglob("*"):
        if path.suffix in (".py", ".html", ".css") and path.is_file():
            text = path.read_text().lower()
            assert "releaseradar" not in text, f"legacy label prefix found in {path}"
            assert "release_radar" not in text, f"legacy db filename found in {path}"
            assert "release-radar" not in text, f"legacy name found in {path}"


def test_app_static_has_both_logo_variants():
    assert (ROOT / "app" / "static" / "logo-black.svg").exists()
    assert (ROOT / "app" / "static" / "logo-white.svg").exists()


def test_base_html_shows_brand_and_favicon():
    text = (ROOT / "app" / "templates" / "base.html").read_text()
    assert "Service Sentinel" in text
    assert 'rel="icon"' in text
    assert "logo-white.svg" in text


def test_fastapi_app_title_and_page_titles_use_brand_name(client):
    resp = client.get("/")
    assert "Service Sentinel" in resp.text
    assert "<title>Overview - Service Sentinel</title>" in resp.text

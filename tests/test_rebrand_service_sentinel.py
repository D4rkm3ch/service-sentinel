"""The app was rebranded from release-radar to Service Sentinel. Covers the two pieces of
real (non-cosmetic) behavior that had to keep working across the rename:

1. Existing compose files using the old `releaseradar.*` label prefix must keep being
   recognized -- there's no reason to force every user to touch their compose files just
   because the app's name changed.
2. An existing install's SQLite database (release_radar.db) must be picked up under the new
   filename (service_sentinel.db) on first startup after the upgrade, not silently orphaned.
"""

from pathlib import Path

from app.config import settings
from app.docker_client import (
    CHANGELOG_LABEL,
    IGNORE_LABEL,
    LOGS_IGNORE_LABEL,
    SOURCE_LABEL,
    TrackedContainer,
    _label,
)

ROOT = Path(__file__).resolve().parent.parent


def test_new_label_prefix_is_servicesentinel():
    assert IGNORE_LABEL == "servicesentinel.ignore"
    assert LOGS_IGNORE_LABEL == "servicesentinel.logs.ignore"
    assert SOURCE_LABEL == "servicesentinel.source"
    assert CHANGELOG_LABEL == "servicesentinel.changelog_url"


def test_legacy_releaseradar_labels_still_work_as_a_fallback():
    legacy_labels = {
        "releaseradar.ignore": "true",
        "releaseradar.source": "owner/legacy-repo",
        "releaseradar.changelog_url": "https://example.com/CHANGELOG",
        "releaseradar.logs.ignore": "true",
    }
    assert _label(legacy_labels, IGNORE_LABEL, "releaseradar.ignore") == "true"

    c = TrackedContainer(name="x", image_repo="owner/repo", tag="latest", current_digest=None, labels=legacy_labels)
    assert c.source_override == "owner/legacy-repo"
    assert c.changelog_url_override == "https://example.com/CHANGELOG"
    assert c.logs_ignored is True


def test_new_label_prefix_takes_priority_over_legacy_when_both_present():
    labels = {"servicesentinel.source": "owner/new-repo", "releaseradar.source": "owner/old-repo"}
    c = TrackedContainer(name="x", image_repo="owner/repo", tag="latest", current_digest=None, labels=labels)
    assert c.source_override == "owner/new-repo"


def test_container_with_no_override_labels_returns_none_not_empty_string():
    c = TrackedContainer(name="x", image_repo="owner/repo", tag="latest", current_digest=None, labels={})
    assert c.source_override is None
    assert c.changelog_url_override is None
    assert c.logs_ignored is False


def test_db_path_uses_new_filename():
    assert settings.db_path.name == "service_sentinel.db"


def test_init_db_migrates_a_legacy_database_file_forward(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "service_sentinel.db")
    legacy_path = tmp_path / "release_radar.db"
    sqlite3.connect(legacy_path).execute("CREATE TABLE marker (id INTEGER)").connection.commit()

    from app import db
    db.init_db()

    assert settings.db_path.exists()
    assert not legacy_path.exists()
    # The migrated file, not a fresh one, must be what got initialized -- the marker table
    # (which nothing in SCHEMA creates) proves it's the same file that was renamed into place.
    conn = sqlite3.connect(settings.db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "marker" in tables


def test_app_static_has_both_logo_variants():
    assert (ROOT / "app" / "static" / "logo-black.svg").exists()
    assert (ROOT / "app" / "static" / "logo-white.svg").exists()


def test_base_html_shows_new_brand_and_favicon():
    text = (ROOT / "app" / "templates" / "base.html").read_text()
    assert "Service Sentinel" in text
    assert "release-radar" not in text
    assert 'rel="icon"' in text
    assert "logo-white.svg" in text


def test_fastapi_app_title_and_page_titles_use_new_name(client):
    resp = client.get("/")
    assert "Service Sentinel" in resp.text
    assert "release-radar" not in resp.text
    assert "<title>Overview — Service Sentinel</title>" in resp.text

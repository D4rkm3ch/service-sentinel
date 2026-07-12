"""build_stack_index() is called on every Updates/Logs page render AND their 20-second
self-refreshing table partials (via _attach_stack_info), and subject_display_name() once per
Compose table row -- all of which used to re-read and re-parse compose YAML from scratch every
time, so an idle open tab re-parsed the whole compose tree several times a minute forever.
Both are now cached against the files' (mtime, size) fingerprints. These tests pin the two
things that matter: unchanged files are NOT re-parsed, and any real change (edit, add, remove)
IS picked up immediately -- a stale cache here would show wrong stack groupings until restart,
which is worse than the parsing cost ever was."""

from unittest.mock import patch

import yaml

from app import compose_lookup
from app.config import settings


def _write(path, services):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump({"services": {name: {"image": f"x/{name}"} for name in services}}))


def test_unchanged_compose_tree_is_not_reparsed(tmp_path):
    with patch.object(settings, "compose_root", tmp_path):
        _write(tmp_path / "media" / "compose.yaml", ["sonarr", "radarr"])

        first = compose_lookup.build_stack_index()
        assert [sorted(e["service_names"]) for e in first] == [["radarr", "sonarr"]]

        with patch("app.compose_lookup.yaml.safe_load",
                   side_effect=AssertionError("unchanged tree was re-parsed")) as mock_parse:
            second = compose_lookup.build_stack_index()
        assert mock_parse.call_count == 0
        assert second == first


def test_an_edited_compose_file_invalidates_the_cache(tmp_path):
    with patch.object(settings, "compose_root", tmp_path):
        target = tmp_path / "media" / "compose.yaml"
        _write(target, ["sonarr"])
        compose_lookup.build_stack_index()

        _write(target, ["sonarr", "lidarr"])  # rewrite changes size (and usually mtime)
        index = compose_lookup.build_stack_index()
        assert sorted(index[0]["service_names"]) == ["lidarr", "sonarr"]


def test_added_and_removed_files_invalidate_the_cache(tmp_path):
    with patch.object(settings, "compose_root", tmp_path):
        _write(tmp_path / "a" / "compose.yaml", ["appa"])
        assert len(compose_lookup.build_stack_index()) == 1

        _write(tmp_path / "b" / "compose.yaml", ["appb"])
        assert len(compose_lookup.build_stack_index()) == 2

        (tmp_path / "b" / "compose.yaml").unlink()
        assert len(compose_lookup.build_stack_index()) == 1


def test_service_names_cache_returns_fresh_names_after_an_edit(tmp_path):
    with patch.object(settings, "compose_root", tmp_path):
        target = tmp_path / "stack" / "compose.yaml"
        _write(target, ["one"])
        assert compose_lookup.get_service_names_for_file(str(target)) == ["one"]

        # Cached path: same file, unchanged -> no re-parse.
        with patch("app.compose_lookup.yaml.safe_load",
                   side_effect=AssertionError("unchanged file was re-parsed")):
            assert compose_lookup.get_service_names_for_file(str(target)) == ["one"]

        _write(target, ["one", "two"])
        assert sorted(compose_lookup.get_service_names_for_file(str(target))) == ["one", "two"]


def test_find_service_config_still_resolves_through_the_cached_index(tmp_path):
    with patch.object(settings, "compose_root", tmp_path):
        target = tmp_path / "media" / "compose.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.dump({"services": {"sonarr": {
            "image": "lscr.io/linuxserver/sonarr:latest",
            "environment": {"TZ": "UTC", "API_KEY": "supersecret"},
        }}}))

        config = compose_lookup.find_service_config("sonarr")
        assert config is not None
        assert config["service_name"] == "sonarr"
        assert config["compose_file"] == str(target)
        assert config["environment"]["API_KEY"] == "[REDACTED]"  # redaction still applies
        assert config["environment"]["TZ"] == "UTC"

"""build_stack_index() is called on every Updates/Logs page render AND their 20-second
self-refreshing table partials (via _attach_stack_info), and subject_display_name() once per
Compose table row -- all of which used to re-read and re-parse compose YAML from scratch every
time, so an idle open tab re-parsed the whole compose tree several times a minute forever.
Both are now cached against the files' (mtime, size) fingerprints: unchanged files are NOT
re-parsed, and a real change (edit, add, remove) is picked up on the very next call -- no
staleness window, ever.

The directory walk that produces that fingerprint turned out to be the expensive part, not
just the YAML parse -- with a real-sized compose tree it's pure-Python pathlib work that holds
the GIL, and concurrent page renders (several open tabs, each auto-refreshing) were each
repeating it independently, serializing under load instead of overlapping. It's now coalesced
("single-flight"): callers that arrive while a walk is already in flight share that one walk's
result instead of starting their own; a call that arrives once the in-flight walk has finished
always starts a brand new one. This deliberately does NOT trade away immediate change
visibility -- a time-based cache was tried first and reverted specifically because it broke
that guarantee everywhere a test (and, in production, a user) writes/edits a compose file and
expects the very next render to see it."""

import threading
import time
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


def test_concurrent_calls_share_one_walk_instead_of_one_each(tmp_path):
    """The actual bug fix: callers that overlap in real time must only walk the directory
    once between them -- this is what stops several open tabs (or a burst of status polls)
    from each independently re-walking a real-sized compose tree and serializing under the
    GIL. Uses a slow fake rglob to force genuine overlap between threads, deterministically,
    rather than relying on timing."""
    with patch.object(settings, "compose_root", tmp_path):
        _write(tmp_path / "media" / "compose.yaml", ["sonarr"])

        real_rglob = type(tmp_path).rglob
        call_count = 0
        call_count_lock = threading.Lock()

        def slow_rglob(self, *args, **kwargs):
            nonlocal call_count
            with call_count_lock:
                call_count += 1
            time.sleep(0.2)  # hold the "walk" open long enough for other threads to arrive
            return real_rglob(self, *args, **kwargs)

        results = []

        def call_build_stack_index():
            results.append(compose_lookup.build_stack_index())

        with patch.object(type(tmp_path), "rglob", slow_rglob):
            threads = [threading.Thread(target=call_build_stack_index) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        # _walk_compose_root() calls rglob twice per walk (*.yml, then *.yaml) -- 2 total
        # means exactly one walk happened for all 10 overlapping callers, not one each.
        assert call_count == 2, f"rglob was called {call_count} times for 10 overlapping callers, expected 2 (one walk)"
        assert len(results) == 10
        for r in results:
            assert [sorted(e["service_names"]) for e in r] == [["sonarr"]]


def test_a_call_after_the_inflight_walk_finishes_always_gets_a_fresh_one(tmp_path):
    """No staleness window: once a walk completes, the very next call -- even immediately
    after, with no delay -- must see a change made in between, not a cached result."""
    with patch.object(settings, "compose_root", tmp_path):
        target = tmp_path / "media" / "compose.yaml"
        _write(target, ["sonarr"])
        compose_lookup.build_stack_index()

        _write(target, ["sonarr", "radarr"])
        index = compose_lookup.build_stack_index()  # no sleep, no clock mocking
        assert sorted(index[0]["service_names"]) == ["radarr", "sonarr"]


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

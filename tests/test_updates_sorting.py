"""Stage 7 polish: the Updates table's sort logic was a pre-Stage-7 stub -- _sort_and_filter_rows
only ever implemented "image" specially and fell back to alphabetical-by-container for
Importance/Detected/Status, and the Stack column never populated at all (_annotate_with_stack
existed but nothing called it). This covers the real fix: Importance ranks by actual severity
with unclassified/error rows always pinned to the top regardless of direction, Detected sorts
by real timestamps, Stack actually populates and sorts, and the default view opens already
sorted most-important-first."""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app import db
from app.config import settings

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    yield
    db.reset_updates_data()


def _seed(name, severity="", error=None, image_repo="owner/x", tag="latest"):
    db.upsert_container_state(name, image_repo, tag, "sha256:old")
    db.record_update(
        container_name=name, image_repo=image_repo, tag=tag,
        old_digest="sha256:old", new_digest="sha256:new",
        summary_markdown=None, source_url=None,
        error=error, severity=severity,
    )
    time.sleep(0.005)  # ensures distinguishable created_at ordering between seeds


def test_default_sort_is_importance_most_severe_first(client):
    _seed("bugfix-app", severity="bugfix")
    _seed("feature-app", severity="feature")
    _seed("action-app", severity="action_needed")
    _seed("breaking-app", severity="breaking")
    _seed("unclassified-app", severity="")
    _seed("broken-app", error="registry unreachable")

    page = client.get("/updates")
    order = [n for n in [
        "broken-app", "unclassified-app", "breaking-app", "action-app", "feature-app", "bugfix-app",
    ] if page.text.index(n) is not None]
    positions = {n: page.text.index(n) for n in order}
    # Unclassified (error + no-severity) pinned at the top, alphabetical among themselves.
    assert positions["broken-app"] < positions["unclassified-app"] < positions["breaking-app"]
    # Then strictly by severity, most severe first.
    assert positions["breaking-app"] < positions["action-app"] < positions["feature-app"] < positions["bugfix-app"]


def test_importance_reverse_still_pins_unclassified_to_the_top(client):
    _seed("bugfix-app", severity="bugfix")
    _seed("breaking-app", severity="breaking")
    _seed("unclassified-app", severity="")

    page = client.get("/updates", params={"sort": "importance", "dir": "desc"})
    pos = {n: page.text.index(n) for n in ["bugfix-app", "breaking-app", "unclassified-app"]}
    # Unclassified still first even on the reversed click...
    assert pos["unclassified-app"] < pos["bugfix-app"]
    assert pos["unclassified-app"] < pos["breaking-app"]
    # ...but the classified group itself is genuinely reversed (least severe first now).
    assert pos["bugfix-app"] < pos["breaking-app"]


def test_detected_sorts_by_real_timestamp_not_alphabetically(client):
    _seed("zzz-first", severity="bugfix")
    _seed("aaa-second", severity="bugfix")

    newest_first = client.get("/updates", params={"sort": "detected", "dir": "desc"})
    assert newest_first.text.index("aaa-second") < newest_first.text.index("zzz-first")

    oldest_first = client.get("/updates", params={"sort": "detected", "dir": "asc"})
    assert oldest_first.text.index("zzz-first") < oldest_first.text.index("aaa-second")


def test_status_column_has_no_text_badge_and_error_rows_get_the_warning_icon_and_row_class(client):
    _seed("ok-app", severity="bugfix")
    _seed("broken-app", error="registry unreachable")

    page = client.get("/updates")
    assert "Update available" not in page.text
    assert "status-warning-icon" in page.text
    assert "row-error" in page.text
    # The healthy row must not also be tagged as an error row.
    ok_row_start = page.text.rindex("<tr", 0, page.text.index("ok-app"))
    ok_row_end = page.text.index("</tr>", ok_row_start)
    assert "row-error" not in page.text[ok_row_start:ok_row_end]


def test_stack_column_actually_populates_and_sorts(client):
    compose_file = Path(settings.compose_root) / "sortstack.yml"
    compose_file.write_text("services:\n  sonarr:\n    image: linuxserver/sonarr\n  plex:\n    image: linuxserver/plex\n")
    try:
        _seed("sonarr", severity="bugfix", image_repo="linuxserver/sonarr")
        _seed("plex", severity="bugfix", image_repo="linuxserver/plex")
        _seed("lonely-app", severity="bugfix")  # not in any stack

        page = client.get("/updates")
        assert "—" in page.text  # lonely-app's stack cell
        assert 'class="stack-cell"' in page.text  # sonarr/plex both resolved to a real stack

        by_stack = client.get("/updates", params={"sort": "stack", "dir": "asc"})
        # Ungrouped (lonely-app) always sorts last regardless of direction.
        assert by_stack.text.index("sonarr") < by_stack.text.index("lonely-app")
        assert by_stack.text.index("plex") < by_stack.text.index("lonely-app")
    finally:
        compose_file.unlink()


def test_lastchecked_sort_works_for_the_full_containers_table(client):
    _seed("zzz-first", severity="bugfix")
    _seed("aaa-second", severity="bugfix")

    newest_first = client.get("/updates", params={"csort": "lastchecked", "cdir": "desc"})
    assert newest_first.text.index("aaa-second") < newest_first.text.index("zzz-first")

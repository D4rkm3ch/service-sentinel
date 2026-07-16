"""Stage 7 polish: the Updates table's sort logic was a pre-Stage-7 stub -- _sort_and_filter_rows
only ever implemented "image" specially and fell back to alphabetical-by-container for
Importance/Detected/Status, and the Stack column never populated at all (_annotate_with_stack
existed but nothing called it). This covers the real fix: Importance ranks by actual severity
with unclassified/error rows always pinned to the top regardless of direction, Detected sorts
by real timestamps, Stack actually populates and sorts, and the default view opens already
sorted most-important-first."""

import time
from pathlib import Path

import pytest

from app import db
from app.config import settings

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    yield
    db.reset_updates_data()


def _seed(name, severity="", error=None, image_repo="owner/x", tag="latest", release_notes_raw="Some release notes."):
    db.upsert_container_state(name, image_repo, tag, "sha256:old")
    db.record_update(
        container_name=name, image_repo=image_repo, tag=tag,
        old_digest="sha256:old", new_digest="sha256:new",
        summary_markdown=None, source_url=None,
        error=error, severity=severity,
        release_notes_raw=release_notes_raw,
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


def test_read_column_sorts_needs_manual_check_then_unread_then_read_alphabetically(client):
    _seed("zebra-unread", severity="bugfix")
    _seed("apple-unread", severity="feature")
    _seed("zebra-read", severity="bugfix")
    _seed("apple-read", severity="feature")
    _seed("zebra-broken", error="registry unreachable")
    _seed("apple-broken", error="registry unreachable")

    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    db.mark_update_status(rows["zebra-read"]["id"], "read")
    db.mark_update_status(rows["apple-read"]["id"], "read")

    page = client.get("/updates", params={"sort": "status", "dir": "asc"})
    names = ["apple-broken", "zebra-broken", "apple-unread", "zebra-unread", "apple-read", "zebra-read"]
    positions = {n: page.text.index(n) for n in names}
    # Needs-manual-check first, then Unread, then Read -- alphabetical within each group.
    assert positions["apple-broken"] < positions["zebra-broken"] < positions["apple-unread"]
    assert positions["apple-unread"] < positions["zebra-unread"] < positions["apple-read"]
    assert positions["apple-read"] < positions["zebra-read"]


def test_read_column_reverse_flips_group_order_but_not_the_alphabetical_tiebreak(client):
    _seed("zebra-unread", severity="bugfix")
    _seed("apple-unread", severity="feature")
    _seed("zebra-read", severity="bugfix")
    _seed("apple-read", severity="feature")

    rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
    db.mark_update_status(rows["zebra-read"]["id"], "read")
    db.mark_update_status(rows["apple-read"]["id"], "read")

    page = client.get("/updates", params={"sort": "status", "dir": "desc"})
    positions = {n: page.text.index(n) for n in ["apple-read", "zebra-read", "apple-unread", "zebra-unread"]}
    # Read now comes before Unread (the group order flipped)...
    assert positions["apple-read"] < positions["zebra-read"] < positions["apple-unread"]
    # ...but "apple" still comes before "zebra" within each group either way.
    assert positions["apple-read"] < positions["zebra-read"]
    assert positions["apple-unread"] < positions["zebra-unread"]


def test_stack_column_actually_populates_and_sorts(client):
    compose_file = Path(settings.compose_root) / "sortstack.yml"
    compose_file.write_text("services:\n  sonarr:\n    image: linuxserver/sonarr\n  plex:\n    image: linuxserver/plex\n")
    try:
        _seed("sonarr", severity="bugfix", image_repo="linuxserver/sonarr")
        _seed("plex", severity="bugfix", image_repo="linuxserver/plex")
        _seed("lonely-app", severity="bugfix")  # not in any stack

        page = client.get("/updates")
        lonely_row = page.text[page.text.index("lonely-app"):]
        assert '<span class="meta">-</span>' in lonely_row[:lonely_row.index("</tr>")]  # no real stack
        assert 'class="stack-cell"' in page.text  # sonarr/plex both resolved to a real stack

        by_stack = client.get("/updates", params={"sort": "stack", "dir": "asc"})
        # Ungrouped (lonely-app) always sorts last regardless of direction.
        assert by_stack.text.index("sonarr") < by_stack.text.index("lonely-app")
        assert by_stack.text.index("plex") < by_stack.text.index("lonely-app")
    finally:
        compose_file.unlink()


def test_notes_not_found_sorts_below_bugfix_and_is_not_pinned_top(client):
    _seed("breaking-app", severity="breaking")
    _seed("bugfix-app", severity="bugfix")
    _seed("no-notes-app", severity="", release_notes_raw=None)
    _seed("unclassified-app", severity="")  # has notes, just no severity yet -- still pinned top

    ascending = client.get("/updates", params={"sort": "importance", "dir": "asc"})
    pos = {n: ascending.text.index(n) for n in [
        "breaking-app", "bugfix-app", "no-notes-app", "unclassified-app",
    ]}
    # Genuinely unclassified (has notes, no severity) still pinned to the very top.
    assert pos["unclassified-app"] < pos["breaking-app"]
    # "Notes not found" ranks below even bugfix, but sorts with the ranked group, not pinned top.
    assert pos["breaking-app"] < pos["bugfix-app"] < pos["no-notes-app"]

    descending = client.get("/updates", params={"sort": "importance", "dir": "desc"})
    dpos = {n: descending.text.index(n) for n in [
        "breaking-app", "bugfix-app", "no-notes-app", "unclassified-app",
    ]}
    # Unclassified still pinned top even reversed...
    assert dpos["unclassified-app"] < dpos["bugfix-app"]
    # ...but the ranked group (including notes-not-found) genuinely flips: least severe first now.
    assert dpos["no-notes-app"] < dpos["bugfix-app"] < dpos["breaking-app"]


def test_notes_not_found_gets_its_own_dull_badge_not_a_blank_dash(client):
    _seed("no-notes-app", severity="", release_notes_raw=None)
    _seed("errored-app", error="registry unreachable", release_notes_raw=None)

    page = client.get("/updates")
    assert "Notes Not Found" in page.text
    # An error row must never be relabeled as "notes not found" -- it's a real error, pinned top.
    error_row_start = page.text.rindex("<tr", 0, page.text.index("errored-app"))
    error_row_end = page.text.index("</tr>", error_row_start)
    assert "Notes Not Found" not in page.text[error_row_start:error_row_end]


def test_lastchecked_sort_works_for_the_full_containers_table(client):
    _seed("zzz-first", severity="bugfix")
    _seed("aaa-second", severity="bugfix")

    newest_first = client.get("/updates", params={"csort": "lastchecked", "cdir": "desc"})
    assert newest_first.text.index("aaa-second") < newest_first.text.index("zzz-first")


def test_updates_table_shows_a_version_column_instead_of_the_raw_image(client):
    """A real-world ask: the raw image:tag string wasn't useful at a glance -- the same resolved
    version Discord's own digest already shows (see notifications._format_update_line) is what
    belongs in the table instead."""
    _seed("versioned-app", severity="bugfix", image_repo="owner/versioned-app", tag="latest",
          release_notes_raw="## v2.4.1 (2026-01-01)\n\nSome notes.")
    _seed("unversioned-app", severity="bugfix", release_notes_raw="Some notes with no heading.")

    page = client.get("/updates")
    assert "sort=version" in page.text

    # Scoped to the Updates Found table's own row -- the separate Tracked containers table
    # below it still legitimately shows the raw image:tag in its own Image column, untouched.
    # ">versioned-app<" (not the bare substring) so this can't accidentally match inside
    # "unversioned-app"'s own cell text.
    versioned_row_start = page.text.rindex("<tr", 0, page.text.index(">versioned-app<"))
    versioned_row_end = page.text.index("</tr>", versioned_row_start)
    versioned_row = page.text[versioned_row_start:versioned_row_end]
    assert "v2.4.1" in versioned_row
    # The raw image:tag is still available as a hover tooltip, just no longer the visible cell
    # text -- confirm it moved into a title attribute, not that it's gone entirely.
    assert 'title="owner/versioned-app:latest">' in versioned_row
    assert ">owner/versioned-app:latest<" not in versioned_row

    # No resolvable version falls back to a plain dash, same empty-state convention as every
    # other column in this table.
    unversioned_row_start = page.text.rindex("<tr", 0, page.text.index("unversioned-app"))
    unversioned_row_end = page.text.index("</tr>", unversioned_row_start)
    assert "meta\">-</span>" in page.text[unversioned_row_start:unversioned_row_end]


def test_version_column_sorts_alphabetically(client):
    _seed("version-b-app", severity="bugfix", release_notes_raw="## v2.0.0 (2026-01-01)\n\nNotes.")
    _seed("version-a-app", severity="bugfix", release_notes_raw="## v1.0.0 (2026-01-01)\n\nNotes.")

    ascending = client.get("/updates", params={"sort": "version", "dir": "asc"})
    assert ascending.text.index("v1.0.0") < ascending.text.index("v2.0.0")

    descending = client.get("/updates", params={"sort": "version", "dir": "desc"})
    assert descending.text.index("v2.0.0") < descending.text.index("v1.0.0")

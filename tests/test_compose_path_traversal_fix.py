"""A real-world report: every Compose route that takes a `path` query/form parameter passed it
straight to the filesystem (compose_reviewer.run_compose_check_for's path.read_text(), and
compose_lookup.get_service_names_for_file's stat/read) with nothing checking it actually pointed
somewhere inside COMPOSE_ROOT. A request like path=/etc/passwd would read that file's real
content, send it to whichever AI provider is configured for a "compose review," and store the
result as a finding, visible on the dashboard -- a genuine arbitrary local file read reachable
by anyone who can send a request to the app at all (there's no authentication anywhere in this
app). Fixed by app.main._validate_compose_path(), called first thing by every route that accepts
a compose file path, rejecting anything that doesn't resolve inside COMPOSE_ROOT with a 404
before any of them touch the filesystem or a database row."""

import os
from unittest.mock import patch

import pytest

from app import compose_reviewer, db
from app.config import settings
from app.main import _validate_compose_path

db.init_db()

_OUTSIDE_PATH = "/etc/passwd"
_TRAVERSAL_PATH = str(settings.compose_root) + "/../../../etc/passwd"


# ---------------------------------------------------------------------------
# _validate_compose_path itself
# ---------------------------------------------------------------------------

def test_validate_compose_path_accepts_a_path_actually_inside_compose_root():
    inside = os.path.join(str(settings.compose_root), "some-stack", "compose.yml")
    assert _validate_compose_path(inside) == inside


def test_validate_compose_path_rejects_a_path_entirely_outside_compose_root():
    with pytest.raises(Exception) as exc_info:
        _validate_compose_path(_OUTSIDE_PATH)
    assert getattr(exc_info.value, "status_code", None) == 404


def test_validate_compose_path_rejects_dot_dot_traversal_back_out_of_compose_root():
    with pytest.raises(Exception) as exc_info:
        _validate_compose_path(_TRAVERSAL_PATH)
    assert getattr(exc_info.value, "status_code", None) == 404


def test_validate_compose_path_rejects_a_relative_path_resolved_against_cwd_not_compose_root():
    """A bare relative filename (no leading slash) resolves against the process's current
    working directory, not COMPOSE_ROOT -- confirming this is rejected too, not just absolute
    escapes, since the process CWD is very unlikely to itself be COMPOSE_ROOT."""
    with pytest.raises(Exception) as exc_info:
        _validate_compose_path("some-relative-file-outside-compose-root.yml")
    assert getattr(exc_info.value, "status_code", None) == 404


def test_validate_compose_path_returns_the_original_unresolved_string_on_success():
    """Must return the exact original string, not the resolved one -- every existing
    finding/checkpoint is keyed on whatever string compose_lookup's own file walk originally
    produced, and silently normalizing it here would orphan every one of them."""
    inside = os.path.join(str(settings.compose_root), "keep-me-exact.yml")
    assert _validate_compose_path(inside) == inside


# ---------------------------------------------------------------------------
# Every route that accepts `path` rejects one outside COMPOSE_ROOT
# ---------------------------------------------------------------------------

def test_compose_file_detail_rejects_a_path_outside_compose_root(client):
    resp = client.get(f"/compose/file?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404


def test_compose_file_check_now_rejects_a_path_outside_compose_root(client):
    with patch.object(compose_reviewer, "run_compose_check_for") as mock_check:
        resp = client.post(f"/compose/file/check-now?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404
    mock_check.assert_not_called()


def test_compose_file_reset_and_recheck_rejects_a_path_outside_compose_root(client):
    with patch.object(compose_reviewer, "run_compose_check_for") as mock_check:
        resp = client.post(f"/compose/file/reset-and-recheck?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404
    mock_check.assert_not_called()


def test_compose_file_regenerate_rejects_a_path_outside_compose_root(client):
    resp = client.post(f"/compose/file/regenerate?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404


def test_compose_file_status_poll_rejects_a_path_outside_compose_root(client):
    resp = client.get(f"/compose/file/status-poll?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404


def test_compose_file_read_rejects_a_path_outside_compose_root(client):
    resp = client.post(f"/compose/file/read?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404


def test_compose_file_unread_rejects_a_path_outside_compose_root(client):
    resp = client.post(f"/compose/file/unread?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404


def test_compose_file_silence_rejects_a_path_outside_compose_root(client):
    resp = client.post(f"/compose/file/silence?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404


def test_compose_file_unsilence_rejects_a_path_outside_compose_root(client):
    resp = client.post(f"/compose/file/unsilence?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404


def test_compose_file_rename_rejects_a_path_outside_compose_root(client):
    resp = client.post("/compose/file/rename", data={"path": _OUTSIDE_PATH, "name": "New Name"})
    assert resp.status_code == 404


def test_compose_file_reset_name_rejects_a_path_outside_compose_root(client):
    resp = client.post("/compose/file/reset-name", data={"path": _OUTSIDE_PATH})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The actual exploit scenario: a real, sensitive file never gets read or exposed as a finding
# ---------------------------------------------------------------------------

def test_check_now_never_reads_a_file_outside_compose_root_even_if_it_exists(client):
    """The concrete attack this closes: path=/etc/passwd (a file that genuinely exists and is
    readable) must never reach compose_reviewer.run_compose_check_for -- if it did, that
    function calls path.read_text() and would send the file's real content to the configured AI
    provider. Asserting the mock is never called (via the real HTTP route, not the validator
    directly) is the end-to-end proof this can't happen anymore."""
    assert os.path.exists(_OUTSIDE_PATH)  # sanity: this test is only meaningful if the file is real
    with patch.object(compose_reviewer, "run_compose_check_for") as mock_check:
        resp = client.post(f"/compose/file/check-now?path={_OUTSIDE_PATH}")
    assert resp.status_code == 404
    mock_check.assert_not_called()

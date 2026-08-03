"""Log checkpoint correctness -- a real-world report that the checkpoint system "wasn't
working".

Two genuine defects, both fixed here and pinned by these tests:

1. The checkpoint was stamped BEFORE the AI triage phase ran, so a triage call that failed
   (rate limit, provider error, truncation ceiling) or was cancelled still advanced every
   container's checkpoint past logs nothing had actually analyzed. The next check fetched
   "since the checkpoint", found nothing new, and whatever was in those logs was lost for
   good -- which reads exactly like "the checker missed it".

2. The checkpoint value was a "now" captured AFTER the whole fetch loop finished, so anything a
   container logged during a long check -- after its own fetch, before the stamp -- fell into a
   gap the next fetch skipped straight over. It's now the time the check STARTED, so that
   window is re-read (a few harmless duplicate lines) instead of dropped.
"""

from unittest.mock import patch

import pytest

from app import db, log_watcher

db.init_db()

CONTAINERS = ["alpha", "beta"]


def _reset():
    db.reset_logs_data()


@pytest.fixture(autouse=True)
def clean():
    _reset()
    yield
    _reset()


def _run(log_text="ERROR something exploded", triage_result=None, triage_raises=False):
    """Runs one log check over CONTAINERS with Docker and the AI both stubbed."""
    def _fake_logs(name, since, max_lines, client=None):
        return log_text

    def _fake_triage(chunk, include_fix=False, active_findings_by_container=None):
        if triage_raises:
            raise RuntimeError("provider blew up")
        return triage_result if triage_result is not None else []

    with patch("app.log_watcher.open_docker_client", return_value=None), \
         patch("app.log_watcher.get_container_logs_since", side_effect=_fake_logs), \
         patch("app.log_watcher.analyze_logs_batch", side_effect=_fake_triage), \
         patch("app.log_watcher.notify_findings_digest"), \
         patch("app.log_watcher.notify_logs_check_errors"):
        return log_watcher.run_log_check_for(CONTAINERS)


# ---------------------------------------------------------------------------
# Defect 1: never advance past logs that were never analyzed
# ---------------------------------------------------------------------------

def test_failed_triage_leaves_checkpoints_untouched():
    """The core guarantee: if the AI call for a container's logs fails, that container keeps
    its old checkpoint so the very next check re-fetches and re-analyzes those same logs."""
    _run(triage_raises=True)
    checkpoints = db.get_log_watch_checkpoints(CONTAINERS)
    assert checkpoints == {}, "a failed triage must not advance any checkpoint"


def test_successful_triage_advances_checkpoints():
    _run(triage_result=[])
    checkpoints = db.get_log_watch_checkpoints(CONTAINERS)
    assert set(checkpoints) == set(CONTAINERS)


def test_a_container_with_nothing_suspicious_still_advances():
    """A clean container has genuinely been checked -- there was simply nothing to send the AI,
    so it never depends on a triage call succeeding and advances on its own."""
    _run(log_text="everything is fine here")
    checkpoints = db.get_log_watch_checkpoints(CONTAINERS)
    assert set(checkpoints) == set(CONTAINERS)


def test_clean_containers_advance_even_when_another_containers_triage_fails():
    """Mixed check: 'beta' has suspicious logs whose triage fails, 'alpha' is clean. Only the
    one that actually needed (and failed) analysis is held back."""
    def _fake_logs(name, since, max_lines, client=None):
        return "ERROR beta is broken" if name == "beta" else "all good"

    with patch("app.log_watcher.open_docker_client", return_value=None), \
         patch("app.log_watcher.get_container_logs_since", side_effect=_fake_logs), \
         patch("app.log_watcher.analyze_logs_batch", side_effect=RuntimeError("boom")), \
         patch("app.log_watcher.notify_findings_digest"), \
         patch("app.log_watcher.notify_logs_check_errors"):
        log_watcher.run_log_check_for(CONTAINERS)

    checkpoints = db.get_log_watch_checkpoints(CONTAINERS)
    assert "alpha" in checkpoints
    assert "beta" not in checkpoints


def test_logs_are_reexamined_on_the_next_check_after_a_failed_triage():
    """End to end proof of the fix: a failed check followed by a successful one still finds the
    issue, because the first check never advanced past it."""
    _run(triage_raises=True)
    result = _run(triage_result=[{
        "container": "alpha", "title": "Something exploded", "category": "error",
        "severity": "warning", "description": "boom",
    }])
    assert result["findings_found"] == 1
    assert [f["title"] for f in db.list_findings("logs")] == ["Something exploded"]


# ---------------------------------------------------------------------------
# Defect 2: stamp the check's start time, not its end time
# ---------------------------------------------------------------------------

def test_checkpoint_is_the_checks_start_time_not_its_finish_time():
    """Anything logged while the check is still running has to be re-read next time rather than
    skipped, so the stamp must predate the fetching."""
    before = db.now_iso()
    _run(triage_result=[])
    after = db.now_iso()

    stamped = db.get_log_watch_checkpoints(CONTAINERS)["alpha"]
    assert before <= stamped <= after


def test_set_log_watch_checkpoints_accepts_an_explicit_timestamp():
    db.set_log_watch_checkpoints(["alpha"], at="2020-01-01T00:00:00+00:00")
    assert db.get_log_watch_checkpoints(["alpha"])["alpha"] == "2020-01-01T00:00:00+00:00"


def test_set_log_watch_checkpoints_still_defaults_to_now():
    before = db.now_iso()
    db.set_log_watch_checkpoints(["alpha"])
    assert db.get_log_watch_checkpoints(["alpha"])["alpha"] >= before

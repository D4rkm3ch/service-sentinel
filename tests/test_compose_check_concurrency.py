"""A real-world report: a 43-file homelab's Compose check took ~4 minutes despite being pure
local file I/O plus AI calls -- no external release-note fetching at all, unlike Updates.
Tracked to run_compose_check_for's per-file loop calling review_compose_file (a real AI call)
sequentially, one file at a time -- the exact same shape as the earlier-fixed Logs slowness.
Locks in that changed files are reviewed concurrently (capped by ai_provider.concurrency_limit(),
same shape as persist.py's Updates pipeline and log_watcher's Logs triage) instead of one after
another."""

import time
from unittest.mock import patch

from app import ai_provider, compose_reviewer, db
from app.config import settings


def _compose_file(name: str, *services: str) -> "__import__('pathlib').Path":
    from pathlib import Path
    path = Path(settings.compose_root) / name
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}:latest\n" for s in services)
    path.write_text(body)
    return path


def test_changed_files_are_reviewed_concurrently_not_one_after_another():
    files = [_compose_file(f"conc{i}.yml", f"svc{i}") for i in range(6)]
    delay = 0.3

    def slow_review(path_str, redacted, include_fix=False):
        time.sleep(delay)
        return []

    try:
        with patch("app.compose_reviewer.review_compose_file", side_effect=slow_review), \
             patch("app.ai_provider.settings.ai_summarize_concurrency", 4):
            start = time.monotonic()
            compose_reviewer.run_compose_check_for(files)
            elapsed = time.monotonic() - start

        # Sequential would take >= 6 * delay; concurrent (4 workers) should land well under that.
        assert elapsed < delay * 6 * 0.6, f"reviews look sequential -- took {elapsed:.2f}s for 6 files at {delay}s each"
    finally:
        for f in files:
            f.unlink()
        with db.get_conn() as conn:
            for f in files:
                conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(f),))


def test_progress_still_reports_upfront_and_reaches_the_full_total():
    files = [_compose_file(f"prog{i}.yml", f"svc{i}") for i in range(5)]
    calls = []

    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[]):
            compose_reviewer.run_compose_check_for(
                files, on_progress=lambda stage, done, total: calls.append((stage, done, total)),
            )
        assert calls[0] == ("checking_compose_files", 0, 5)
        assert calls[-1] == ("checking_compose_files", 5, 5)
        assert all(stage == "checking_compose_files" for stage, _, _ in calls)
        # Every value from 1..5 shows up exactly once, regardless of which worker finished when.
        dones = sorted(d for _, d, _ in calls[1:])
        assert dones == [1, 2, 3, 4, 5]
    finally:
        for f in files:
            f.unlink()
        with db.get_conn() as conn:
            for f in files:
                conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(f),))


def test_a_failed_review_does_not_lose_findings_from_other_files():
    files = [_compose_file(f"mix{i}.yml", f"svc{i}") for i in range(3)]

    def fake_review(path_str, redacted, include_fix=False):
        if "mix0" in path_str:
            raise RuntimeError("simulated AI failure")
        return [{"title": "Real issue", "category": "reliability", "severity": "warning",
                  "description": "desc"}]

    try:
        with patch("app.compose_reviewer.review_compose_file", side_effect=fake_review):
            result = compose_reviewer.run_compose_check_for(files)

        assert result["errors"] == 1
        assert result["reviewed"] == 2
        assert result["findings_found"] == 2
    finally:
        for f in files:
            f.unlink()
        with db.get_conn() as conn:
            for f in files:
                conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(f),))
                conn.execute("DELETE FROM compose_check_errors WHERE file_path = ?", (str(f),))
                conn.execute("DELETE FROM findings WHERE source = 'compose' AND subject = ?", (str(f),))

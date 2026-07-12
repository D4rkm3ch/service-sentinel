"""A real-world report: a scoped single-item Check Now on a Logs/Compose sub-page (stack,
service, finding, or compose file) sat at a bare, totalless "Checking…" for the entire
duration of the check -- unlike the main pages, which show live "Checking X (N/M)…" text.
Root cause: run_log_check_for/run_compose_check_for only ever called on_progress once a file
or container's own (possibly slow, AI-driven) work had already finished, never upfront before
starting -- so main.py's _progress_text (no total means no "(N/M)" text at all, just the bare
"Checking…" fallback) had nothing to render until the single item was already done. Updates'
own pipeline (persist._run_concurrent_phase) already got this right, calling on_progress with
(stage, 0, total) before the phase's work starts; this brings Logs and Compose in line."""

from pathlib import Path
from unittest.mock import patch

from app import compose_reviewer, db, log_watcher
from app.config import settings


def _compose_file(name: str, *services: str) -> Path:
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def test_compose_check_reports_progress_upfront_before_the_first_files_review_finishes():
    compose_file = _compose_file("progress-upfront.yml", "progress-svc")
    calls = []
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[]):
            compose_reviewer.run_compose_check_for(
                [compose_file], on_progress=lambda stage, done, total: calls.append((stage, done, total)),
            )
        assert calls[0] == ("checking_compose_files", 0, 1)
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(compose_file),))


def test_compose_check_reports_no_progress_at_all_for_an_empty_path_list():
    calls = []
    compose_reviewer.run_compose_check_for([], on_progress=lambda stage, done, total: calls.append((stage, done, total)))
    assert calls == []


def test_log_check_reports_progress_upfront_before_the_first_containers_fetch_finishes():
    calls = []
    with patch("app.log_watcher.get_container_logs_since", return_value=""):
        log_watcher.run_log_check_for(
            ["progress-upfront-container"], on_progress=lambda stage, done, total: calls.append((stage, done, total)),
        )
    assert calls[0] == ("checking_logs", 0, 1)


def test_log_check_reports_no_progress_at_all_for_an_empty_container_list():
    calls = []
    log_watcher.run_log_check_for([], on_progress=lambda stage, done, total: calls.append((stage, done, total)))
    assert calls == []

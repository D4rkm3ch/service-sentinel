"""A real-world audit found Compose still had the one-connection-per-item anti-pattern already
fixed for Updates (persist.py's container_state_by_name batching) and Logs (db.get/set_log_watch_
checkpoints): compose_reviewer.run_compose_check_for's fast sequential pass called
db.get_compose_file_hash(path) once per file inside its own pure-local-I/O loop, and stamped the
hash for a redaction-skipped file (db.set_compose_file_hash) the same way -- a real-world 43-file
homelab meant well over 43 separate connect/commit cycles for work that has no AI call anywhere
nearby to justify it. Fixed with db.get_compose_file_hashes/set_compose_file_hashes, batched the
same way Logs' checkpoint read/write already are. Mirrors test_logs_full_parity_actions.py's own
connection-count regression test for run_log_check_for."""

import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

from app import compose_reviewer, db
from app.config import settings

db.init_db()


def _compose_file(name: str, content: str) -> Path:
    path = Path(settings.compose_root) / name
    path.write_text(content)
    return path


def test_run_compose_check_for_uses_a_fixed_number_of_connections_not_one_per_file():
    """All files already hashed and unchanged -- no AI review needed, so this only exercises the
    fast sequential pass. Expect exactly 2 connections (one batched hash read, one batched
    clear-errors write) regardless of file count -- no connection for set_compose_file_hashes
    (nothing to stamp, every file was already unchanged) or record_compose_check_errors/
    notify_findings_digest (both no-op on empty input)."""
    paths = []
    try:
        for i in range(12):
            content = f"services:\n  conn-batch-{i}:\n    image: owner/conn-batch-{i}\n"
            path = _compose_file(f"conn-batch-{i}.yml", content)
            paths.append(path)
            db.set_compose_file_hash(str(path), hashlib.sha256(content.encode()).hexdigest())

        original_connect = sqlite3.connect
        connect_calls = []

        def counting_connect(*args, **kwargs):
            connect_calls.append(1)
            return original_connect(*args, **kwargs)

        with patch("app.db.sqlite3.connect", side_effect=counting_connect):
            result = compose_reviewer.run_compose_check_for(paths)

        assert connect_calls == [1, 1], f"expected a fixed 2-connection batch, got {len(connect_calls)}"
        assert result == {"checked": 12, "reviewed": 0, "findings_found": 0, "errors": 0, "rate_limited": 0, "cancelled": False}
    finally:
        for path in paths:
            path.unlink()
        with db.get_conn() as conn:
            qs = ",".join("?" * len(paths))
            conn.execute(f"DELETE FROM compose_file_state WHERE file_path IN ({qs})", [str(p) for p in paths])


def test_run_compose_check_for_batches_hash_writes_for_redaction_skipped_files():
    """New/changed files whose redaction step returns None (see redact_compose_file_text) get
    their hash stamped without an AI review -- also part of the fast sequential pass, so those
    writes must go through the same single batched set_compose_file_hashes call, not one
    connection per skipped file."""
    paths = []
    try:
        for i in range(6):
            content = f"services:\n  redact-skip-{i}:\n    image: owner/redact-skip-{i}\n"
            paths.append(_compose_file(f"redact-skip-{i}.yml", content))

        original_connect = sqlite3.connect
        connect_calls = []

        def counting_connect(*args, **kwargs):
            connect_calls.append(1)
            return original_connect(*args, **kwargs)

        with patch("app.compose_reviewer.redact_compose_file_text", return_value=None), \
             patch("app.db.sqlite3.connect", side_effect=counting_connect):
            result = compose_reviewer.run_compose_check_for(paths)

        assert connect_calls == [1, 1, 1], f"expected a fixed 3-connection batch, got {len(connect_calls)}"
        assert result["checked"] == 6
        assert result["reviewed"] == 0

        for path in paths:
            content = path.read_text()
            assert db.get_compose_file_hash(str(path)) == hashlib.sha256(content.encode()).hexdigest()
    finally:
        for path in paths:
            path.unlink()
        with db.get_conn() as conn:
            qs = ",".join("?" * len(paths))
            conn.execute(f"DELETE FROM compose_file_state WHERE file_path IN ({qs})", [str(p) for p in paths])

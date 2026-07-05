import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS container_state (
    container_name TEXT PRIMARY KEY,
    image_repo TEXT NOT NULL,
    tag TEXT NOT NULL,
    last_seen_digest TEXT,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    image_repo TEXT NOT NULL,
    tag TEXT NOT NULL,
    old_digest TEXT,
    new_digest TEXT,
    summary_markdown TEXT,
    source_url TEXT,
    status TEXT NOT NULL DEFAULT 'unread',
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_updates_container ON updates(container_name);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,               -- 'logs' or 'compose'
    subject TEXT NOT NULL,              -- container name (logs) or file path (compose)
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,             -- error, security, reliability, optimization
    severity TEXT NOT NULL,             -- suggestion, warning, critical
    description_markdown TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',  -- active or silenced
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(source, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_findings_source ON findings(source);

CREATE TABLE IF NOT EXISTS log_watch_state (
    container_name TEXT PRIMARY KEY,
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compose_file_state (
    file_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    last_reviewed_at TEXT NOT NULL
);
"""

# All three features ship off by default — nothing runs, nothing spends tokens, until the
# person turns each one on from the Overview page.
DEFAULT_FEATURE_STATE = {
    "updates": "false",
    "logs": "false",
    "compose": "false",
}

SEVERITY_ORDER = {"suggestion": 0, "warning": 1, "critical": 2}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_FEATURE_STATE.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (f"feature_{key}_enabled", value),
            )


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Feature toggles
# ---------------------------------------------------------------------------

def get_feature_enabled(feature: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (f"feature_{feature}_enabled",))
        row = cur.fetchone()
        return row is not None and row["value"] == "true"


def set_feature_enabled(feature: str, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"feature_{feature}_enabled", "true" if enabled else "false"),
        )


def get_all_feature_states() -> dict:
    return {name: get_feature_enabled(name) for name in DEFAULT_FEATURE_STATE}


# ---------------------------------------------------------------------------
# Schedules — a "master" schedule everything uses by default, with an optional
# per-feature override. Stored as small JSON specs rather than raw cron strings
# so the UI can offer friendly presets (daily / every N hours / weekly) as well
# as a custom-cron escape hatch.
# ---------------------------------------------------------------------------

DEFAULT_MASTER_SCHEDULE = {"mode": "daily", "hour": 6, "minute": 0}


def _get_json_setting(key: str, default: dict) -> dict:
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = cur.fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return default


def _set_json_setting(key: str, value: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, json.dumps(value)),
        )


def get_master_schedule() -> dict:
    return _get_json_setting("schedule_master", DEFAULT_MASTER_SCHEDULE)


def set_master_schedule(spec: dict) -> None:
    _set_json_setting("schedule_master", spec)


def get_feature_uses_master_schedule(feature: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (f"schedule_{feature}_use_master",))
        row = cur.fetchone()
        return row is None or row["value"] == "true"


def set_feature_uses_master_schedule(feature: str, use_master: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"schedule_{feature}_use_master", "true" if use_master else "false"),
        )


def get_feature_schedule(feature: str) -> dict:
    """The feature's own schedule spec, used only when it's not following the master."""
    return _get_json_setting(f"schedule_{feature}", DEFAULT_MASTER_SCHEDULE)


def set_feature_schedule(feature: str, spec: dict) -> None:
    _set_json_setting(f"schedule_{feature}", spec)


def get_effective_schedule(feature: str) -> dict:
    """What actually governs this feature's timing right now — its own override if it has
    one enabled, otherwise the master schedule."""
    if get_feature_uses_master_schedule(feature):
        return get_master_schedule()
    return get_feature_schedule(feature)


# ---------------------------------------------------------------------------
# Container digest tracking (updates feature)
# ---------------------------------------------------------------------------

def get_container_state(container_name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM container_state WHERE container_name = ?", (container_name,)
        )
        return cur.fetchone()


def upsert_container_state(container_name: str, image_repo: str, tag: str, digest: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO container_state (container_name, image_repo, tag, last_seen_digest, last_checked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(container_name) DO UPDATE SET
                image_repo=excluded.image_repo,
                tag=excluded.tag,
                last_seen_digest=excluded.last_seen_digest,
                last_checked_at=excluded.last_checked_at
            """,
            (container_name, image_repo, tag, digest, now_iso()),
        )


def all_container_states() -> list[sqlite3.Row]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM container_state ORDER BY container_name")
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------

def record_update(
    container_name: str,
    image_repo: str,
    tag: str,
    old_digest: str | None,
    new_digest: str | None,
    summary_markdown: str | None,
    source_url: str | None,
    error: str | None = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO updates
                (container_name, image_repo, tag, old_digest, new_digest, summary_markdown, source_url, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (container_name, image_repo, tag, old_digest, new_digest, summary_markdown, source_url, error, now_iso()),
        )
        return cur.lastrowid


def list_recent_updates(limit: int = 100) -> list[sqlite3.Row]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM updates ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()


def get_update(update_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM updates WHERE id = ?", (update_id,))
        return cur.fetchone()


def mark_update_status(update_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE updates SET status = ? WHERE id = ?", (status, update_id))


def latest_update_summary() -> dict:
    """Small health summary for the Overview card: how many unread/error updates are open."""
    with get_conn() as conn:
        cur = conn.execute("SELECT COUNT(*) AS n FROM updates WHERE status = 'unread'")
        unread = cur.fetchone()["n"]
        cur = conn.execute("SELECT MAX(created_at) AS t FROM updates")
        last_at = cur.fetchone()["t"]
    return {"unread": unread, "last_at": last_at}


# ---------------------------------------------------------------------------
# Findings (shared by the log watcher and compose reviewer)
# ---------------------------------------------------------------------------

def make_fingerprint(source: str, subject: str, title: str) -> str:
    raw = f"{source}:{subject}:{title.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def upsert_finding(
    source: str,
    subject: str,
    title: str,
    category: str,
    severity: str,
    description_markdown: str,
) -> tuple[int, bool]:
    """Inserts a new finding, or if the same (source, fingerprint) already exists, bumps its
    occurrence count and last-seen time instead of creating a duplicate. A silenced finding
    stays silenced even if it recurs — recurrence updates it quietly rather than reviving it.

    Returns (finding_id, is_new) — is_new is what callers use to decide whether to notify.
    """
    fingerprint = make_fingerprint(source, subject, title)
    now = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id FROM findings WHERE source = ? AND fingerprint = ?", (source, fingerprint)
        )
        existing = cur.fetchone()
        if existing:
            conn.execute(
                """
                UPDATE findings
                SET occurrence_count = occurrence_count + 1,
                    last_seen_at = ?,
                    description_markdown = ?
                WHERE id = ?
                """,
                (now, description_markdown, existing["id"]),
            )
            return existing["id"], False

        cur = conn.execute(
            """
            INSERT INTO findings
                (source, subject, fingerprint, title, category, severity, description_markdown,
                 occurrence_count, status, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)
            """,
            (source, subject, fingerprint, title, category, severity, description_markdown, now, now),
        )
        return cur.lastrowid, True


def list_findings(source: str, include_silenced: bool = False) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if include_silenced:
            cur = conn.execute(
                "SELECT * FROM findings WHERE source = ? ORDER BY last_seen_at DESC", (source,)
            )
        else:
            cur = conn.execute(
                "SELECT * FROM findings WHERE source = ? AND status = 'active' ORDER BY last_seen_at DESC",
                (source,),
            )
        return cur.fetchall()


def get_finding(finding_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,))
        return cur.fetchone()


def set_finding_status(finding_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE findings SET status = ? WHERE id = ?", (status, finding_id))


def findings_health_summary(source: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE source = ? AND status = 'active'", (source,)
        )
        active = cur.fetchone()["n"]
        cur = conn.execute(
            "SELECT MAX(last_seen_at) AS t FROM findings WHERE source = ?", (source,)
        )
        last_at = cur.fetchone()["t"]
    return {"active": active, "last_at": last_at}


# ---------------------------------------------------------------------------
# Log watcher per-container checkpoint
# ---------------------------------------------------------------------------

def get_log_watch_checkpoint(container_name: str) -> str | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT last_checked_at FROM log_watch_state WHERE container_name = ?", (container_name,)
        )
        row = cur.fetchone()
        return row["last_checked_at"] if row else None


def set_log_watch_checkpoint(container_name: str) -> None:
    now = now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO log_watch_state (container_name, last_checked_at) VALUES (?, ?)
            ON CONFLICT(container_name) DO UPDATE SET last_checked_at = excluded.last_checked_at
            """,
            (container_name, now),
        )


# ---------------------------------------------------------------------------
# Compose file change tracking
# ---------------------------------------------------------------------------

def get_compose_file_hash(file_path: str) -> str | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT content_hash FROM compose_file_state WHERE file_path = ?", (file_path,)
        )
        row = cur.fetchone()
        return row["content_hash"] if row else None


def set_compose_file_hash(file_path: str, content_hash: str) -> None:
    now = now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO compose_file_state (file_path, content_hash, last_reviewed_at) VALUES (?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET content_hash = excluded.content_hash, last_reviewed_at = excluded.last_reviewed_at
            """,
            (file_path, content_hash, now),
        )

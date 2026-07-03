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
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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


def all_container_states() -> list[sqlite3.Row]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM container_state ORDER BY container_name")
        return cur.fetchall()

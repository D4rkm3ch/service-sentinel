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
    severity TEXT NOT NULL DEFAULT 'warning',
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
    suggested_fix TEXT,                 -- only populated when Deep Analysis is on for this source
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

CREATE TABLE IF NOT EXISTS subject_summaries (
    source TEXT NOT NULL,
    subject TEXT NOT NULL,
    findings_hash TEXT NOT NULL,
    summary_markdown TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, subject)
);

CREATE TABLE IF NOT EXISTS stacks (
    stack_id TEXT PRIMARY KEY,          -- the compose file's own path, stable identifier
    display_name TEXT NOT NULL,
    name_source TEXT NOT NULL DEFAULT 'ai',  -- 'ai' or 'manual' — manual names never auto-regenerate
    services_hash TEXT,                 -- hash of the service list the AI name was based on
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stack_analyses (
    stack_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,         -- hash of member names + their current digests
    analysis_markdown TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_notes_cache (
    image_repo TEXT PRIMARY KEY,
    method TEXT NOT NULL,               -- 'github' or 'url'
    location TEXT NOT NULL,             -- 'owner/repo' for github, or a URL
    last_success_at TEXT NOT NULL
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
DEFAULT_SEVERITY = "suggestion"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_setting(key: str, default: str) -> str:
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row is not None else default


def _set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_FEATURE_STATE.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (f"feature_{key}_enabled", value),
            )
        # Migration: existing installs created the updates table before it had a severity
        # column. CREATE TABLE IF NOT EXISTS above won't add it to an already-existing table,
        # so add it explicitly, tolerating the case where it's already there.
        try:
            conn.execute("ALTER TABLE updates ADD COLUMN severity TEXT NOT NULL DEFAULT 'warning'")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        # Same pattern for the findings table's suggested_fix column (Deep Analysis feature).
        try:
            conn.execute("ALTER TABLE findings ADD COLUMN suggested_fix TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

        # Explicitly seed defaults rather than relying only on read-time fallbacks — this is
        # the same belt-and-suspenders approach as the feature toggles above, and avoids any
        # ambiguity in what a fresh install's severity pickers show on first load.
        # Updates uses its own 4-tier scale (bugfix/feature/action_needed/breaking), separate
        # from the 3-tier scale (suggestion/warning/critical) Logs and Compose still use.
        default_settings = {
            "notify_severity_master": DEFAULT_SEVERITY,
            "notify_severity_updates": "bugfix",
            "notify_severity_logs": DEFAULT_SEVERITY,
            "notify_severity_compose": DEFAULT_SEVERITY,
            "deep_analysis_logs_enabled": "false",
            "deep_analysis_compose_enabled": "false",
        }
        for key, value in default_settings.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, value)
            )

        # Migration: an existing install may already have notify_severity_updates seeded with
        # a value from the old shared 3-tier scale — correct it to the new scale's default.
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'notify_severity_updates'"
        ).fetchone()
        if row and row["value"] not in ("bugfix", "feature", "action_needed", "breaking"):
            conn.execute(
                "UPDATE app_settings SET value = 'bugfix' WHERE key = 'notify_severity_updates'"
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
# Notifications — Apprise only. A master on/off, a single set of Apprise URLs,
# a per-feature on/off, and a severity threshold that follows the same
# master/override pattern as scheduling (a general severity, with an optional
# per-feature override for Logs and Compose — Updates notifications aren't
# severity-graded, so there's no severity setting for that one).
# ---------------------------------------------------------------------------


def get_notifications_enabled() -> bool:
    return _get_setting("notify_enabled", "false") == "true"


def set_notifications_enabled(enabled: bool) -> None:
    _set_setting("notify_enabled", "true" if enabled else "false")


def get_apprise_urls() -> list[str]:
    raw = _get_setting("notify_apprise_urls", "")
    return [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]


def set_apprise_urls(raw: str) -> None:
    _set_setting("notify_apprise_urls", raw or "")


def get_feature_notify_enabled(feature: str) -> bool:
    # Defaults to on: once someone's turned the master switch on, the least surprising
    # default is "notify for everything" — they can dial a specific tab back from there.
    return _get_setting(f"notify_{feature}_enabled", "true") == "true"


def set_feature_notify_enabled(feature: str, enabled: bool) -> None:
    _set_setting(f"notify_{feature}_enabled", "true" if enabled else "false")


def get_severity_master() -> str:
    return _get_setting("notify_severity_master", DEFAULT_SEVERITY)


def set_severity_master(value: str) -> None:
    _set_setting("notify_severity_master", value)


def get_feature_uses_master_severity(feature: str) -> bool:
    return _get_setting(f"notify_severity_{feature}_use_master", "true") == "true"


def set_feature_uses_master_severity(feature: str, use_master: bool) -> None:
    _set_setting(f"notify_severity_{feature}_use_master", "true" if use_master else "false")


def get_feature_severity(feature: str) -> str:
    return _get_setting(f"notify_severity_{feature}", DEFAULT_SEVERITY)


def set_feature_severity(feature: str, value: str) -> None:
    _set_setting(f"notify_severity_{feature}", value)


def get_effective_severity(feature: str) -> str:
    if feature == "updates":
        # Updates uses its own 4-tier scale (bugfix/feature/action_needed/breaking), which
        # has no meaningful correspondence to the shared 3-tier scale General/Logs/Compose
        # use — it always uses its own value directly, never the master toggle.
        return get_feature_severity(feature)
    if get_feature_uses_master_severity(feature):
        return get_severity_master()
    return get_feature_severity(feature)


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


CONTAINER_SORT_COLUMNS = {
    "container": "container_name COLLATE NOCASE",
    "image": "image_repo COLLATE NOCASE",
    "lastchecked": "last_checked_at",
}


def all_container_states(sort: str = "container", direction: str = "asc") -> list[sqlite3.Row]:
    sort_expr = CONTAINER_SORT_COLUMNS.get(sort, CONTAINER_SORT_COLUMNS["container"])
    dir_sql = "DESC" if direction == "desc" else "ASC"
    order_clause = f"{sort_expr} {dir_sql}"
    if sort != "container":
        order_clause += ", container_name COLLATE NOCASE ASC"
    with get_conn() as conn:
        cur = conn.execute(f"SELECT * FROM container_state ORDER BY {order_clause}")
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
    severity: str = "feature",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO updates
                (container_name, image_repo, tag, old_digest, new_digest, summary_markdown, source_url, error, severity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (container_name, image_repo, tag, old_digest, new_digest, summary_markdown, source_url, error, severity, now_iso()),
        )
        return cur.lastrowid


UPDATE_SORT_COLUMNS = {
    "container": "container_name COLLATE NOCASE",
    "image": "image_repo COLLATE NOCASE",
    "detected": "created_at",
    "importance": (
        "CASE severity "
        "WHEN 'bugfix' THEN 0 WHEN 'feature' THEN 1 WHEN 'action_needed' THEN 2 WHEN 'breaking' THEN 3 "
        "ELSE 4 END"
    ),
    "status": "CASE WHEN error IS NOT NULL THEN 'needs manual check' WHEN status = 'unread' THEN 'new' ELSE 'read' END",
}


def list_recent_updates(limit: int = 100, sort: str = "importance", direction: str = "asc") -> list[sqlite3.Row]:
    sort_expr = UPDATE_SORT_COLUMNS.get(sort, UPDATE_SORT_COLUMNS["importance"])
    dir_sql = "DESC" if direction == "desc" else "ASC"

    if sort in ("container", "image"):
        # Sorting by the column itself is already a full ordering — no separate alpha
        # tiebreak needed, just a stable secondary sort by recency.
        order_clause = f"{sort_expr} {dir_sql}, created_at DESC"
    elif sort == "detected":
        order_clause = f"{sort_expr} {dir_sql}"
    else:
        # importance / status: the clicked direction flips the tier/label order, but the
        # within-tier tiebreak always stays alphabetical ascending, per how this was asked for.
        order_clause = f"{sort_expr} {dir_sql}, container_name COLLATE NOCASE ASC"

    with get_conn() as conn:
        cur = conn.execute(
            f"""
            SELECT * FROM (
                SELECT * FROM updates ORDER BY created_at DESC LIMIT ?
            )
            ORDER BY {order_clause}
            """,
            (limit,),
        )
        return cur.fetchall()


def reset_updates_data() -> None:
    """TEMPORARY — testing tool for migrating to the new 4-tier Updates severity system.
    Wipes all Updates history and the digest-tracking baseline, so the next check treats
    every currently-installed container as fresh and regenerates its summary (and severity
    tag) using the current AI prompt. Remove this once the new system has settled in."""
    with get_conn() as conn:
        conn.execute("DELETE FROM updates")
        conn.execute("DELETE FROM container_state")


def get_update(update_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM updates WHERE id = ?", (update_id,))
        return cur.fetchone()


def get_latest_update_for_container(container_name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM updates WHERE container_name = ? ORDER BY created_at DESC LIMIT 1",
            (container_name,),
        )
        return cur.fetchone()


def update_existing_update(update_id: int, summary_markdown: str | None, severity: str,
                           error: str | None, source_url: str | None) -> None:
    """Regenerates an existing update record in place (used by the manual Retry button) —
    keeps the same id, container, tag, and digests, just refreshes the AI-generated content
    and clears/resets status back to unread so the fresh content gets seen."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE updates
            SET summary_markdown = ?, severity = ?, error = ?, source_url = ?, status = 'unread'
            WHERE id = ?
            """,
            (summary_markdown, severity, error, source_url, update_id),
        )


def list_updates_for_stack_containers(container_names: list[str]) -> list[sqlite3.Row]:
    if not container_names:
        return []
    with get_conn() as conn:
        placeholders = ",".join("?" * len(container_names))
        cur = conn.execute(
            f"SELECT * FROM updates WHERE container_name IN ({placeholders})",
            container_names,
        )
        return cur.fetchall()


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
    suggested_fix: str | None = None,
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
                    description_markdown = ?,
                    suggested_fix = ?
                WHERE id = ?
                """,
                (now, description_markdown, suggested_fix, existing["id"]),
            )
            return existing["id"], False

        cur = conn.execute(
            """
            INSERT INTO findings
                (source, subject, fingerprint, title, category, severity, description_markdown,
                 suggested_fix, occurrence_count, status, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)
            """,
            (source, subject, fingerprint, title, category, severity, description_markdown,
             suggested_fix, now, now),
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


def list_findings_for_subject(source: str, subject: str, include_silenced: bool = False) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if include_silenced:
            cur = conn.execute(
                "SELECT * FROM findings WHERE source = ? AND subject = ? ORDER BY last_seen_at DESC",
                (source, subject),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM findings WHERE source = ? AND subject = ? AND status = 'active' ORDER BY last_seen_at DESC",
                (source, subject),
            )
        return cur.fetchall()


def list_subjects_with_findings(source: str, include_silenced: bool = False) -> list[dict]:
    """One row per subject (container or compose file) that has at least one finding, with
    aggregate counts and the highest severity present — used for the grouped 'Issues' list
    at the top of the Logs/Compose tabs, so you see one line per container rather than one
    line per individual finding."""
    status_filter = "" if include_silenced else "AND status = 'active'"
    with get_conn() as conn:
        cur = conn.execute(
            f"""
            SELECT subject,
                   COUNT(*) AS finding_count,
                   MAX(last_seen_at) AS last_seen_at,
                   SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical_count,
                   SUM(CASE WHEN severity = 'warning' THEN 1 ELSE 0 END) AS warning_count,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_count,
                   SUM(CASE WHEN status = 'silenced' THEN 1 ELSE 0 END) AS silenced_count
            FROM findings
            WHERE source = ? {status_filter}
            GROUP BY subject
            ORDER BY last_seen_at DESC
            """,
            (source,),
        )
        rows = []
        for r in cur.fetchall():
            row = dict(r)
            if row["critical_count"]:
                row["top_severity"] = "critical"
            elif row["warning_count"]:
                row["top_severity"] = "warning"
            else:
                row["top_severity"] = "suggestion"
            rows.append(row)
        return rows


def all_log_watch_states_with_status() -> list[dict]:
    """Every container the log watcher has ever checked, with a healthy/issue status —
    used for the 'All containers' list at the bottom of the Logs tab."""
    with get_conn() as conn:
        cur = conn.execute("SELECT container_name, last_checked_at FROM log_watch_state ORDER BY container_name")
        rows = cur.fetchall()
        result = []
        for r in rows:
            cur2 = conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE source = 'logs' AND subject = ? AND status = 'active'",
                (r["container_name"],),
            )
            active = cur2.fetchone()["n"]
            result.append({
                "name": r["container_name"],
                "last_at": r["last_checked_at"],
                "status": "issue" if active else "healthy",
            })
        return result


def all_compose_file_states_with_status() -> list[dict]:
    """Every compose file the reviewer has ever checked, with a healthy/issue status —
    used for the 'All files' list at the bottom of the Compose tab."""
    with get_conn() as conn:
        cur = conn.execute("SELECT file_path, last_reviewed_at FROM compose_file_state ORDER BY file_path")
        rows = cur.fetchall()
        result = []
        for r in rows:
            cur2 = conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE source = 'compose' AND subject = ? AND status = 'active'",
                (r["file_path"],),
            )
            active = cur2.fetchone()["n"]
            result.append({
                "name": r["file_path"],
                "last_at": r["last_reviewed_at"],
                "status": "issue" if active else "healthy",
            })
        return result


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


# ---------------------------------------------------------------------------
# Cached combined findings overview (per subject)
# ---------------------------------------------------------------------------

def get_subject_summary(source: str, subject: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM subject_summaries WHERE source = ? AND subject = ?", (source, subject)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def set_subject_summary(source: str, subject: str, findings_hash: str, summary_markdown: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO subject_summaries (source, subject, findings_hash, summary_markdown, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, subject) DO UPDATE SET
                findings_hash = excluded.findings_hash,
                summary_markdown = excluded.summary_markdown,
                created_at = excluded.created_at
            """,
            (source, subject, findings_hash, summary_markdown, now_iso()),
        )


# ---------------------------------------------------------------------------
# Deep Analysis — opt-in, per-feature (Logs and Compose only), off by default.
# When on, findings get an AI-suggested fix in addition to the problem report,
# which costs more tokens per finding.
# ---------------------------------------------------------------------------

def get_deep_analysis_enabled(feature: str) -> bool:
    return _get_setting(f"deep_analysis_{feature}_enabled", "false") == "true"


def set_deep_analysis_enabled(feature: str, enabled: bool) -> None:
    _set_setting(f"deep_analysis_{feature}_enabled", "true" if enabled else "false")


# ---------------------------------------------------------------------------
# Stacks — grouping containers that share a compose file. Names are AI-generated
# by default but can be manually overridden; a manual name never gets auto-regenerated.
# ---------------------------------------------------------------------------

def get_stack(stack_id: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM stacks WHERE stack_id = ?", (stack_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_stack_name(stack_id: str, display_name: str, name_source: str, services_hash: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO stacks (stack_id, display_name, name_source, services_hash, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(stack_id) DO UPDATE SET
                display_name = excluded.display_name,
                name_source = excluded.name_source,
                services_hash = excluded.services_hash,
                updated_at = excluded.updated_at
            """,
            (stack_id, display_name, name_source, services_hash, now_iso()),
        )


def reset_stack_name(stack_id: str) -> None:
    """Clears a manual override so the next check regenerates an AI name from scratch."""
    with get_conn() as conn:
        conn.execute("DELETE FROM stacks WHERE stack_id = ?", (stack_id,))


# ---------------------------------------------------------------------------
# Cached stack-wide cross-service analysis
# ---------------------------------------------------------------------------

def get_stack_analysis(stack_id: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM stack_analyses WHERE stack_id = ?", (stack_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_stack_analysis(stack_id: str, content_hash: str, analysis_markdown: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO stack_analyses (stack_id, content_hash, analysis_markdown, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(stack_id) DO UPDATE SET
                content_hash = excluded.content_hash,
                analysis_markdown = excluded.analysis_markdown,
                created_at = excluded.created_at
            """,
            (stack_id, content_hash, analysis_markdown, now_iso()),
        )


# ---------------------------------------------------------------------------
# Release notes source cache — remembers where we successfully found real
# release notes for an image last time, so future checks try that exact
# location first instead of re-discovering it from scratch (guessing, then
# web search) every single time. Only updated on genuine success; a stale
# cached location that stops working just falls through to full discovery.
# ---------------------------------------------------------------------------

def get_release_notes_source(image_repo: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM release_notes_cache WHERE image_repo = ?", (image_repo,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def set_release_notes_source(image_repo: str, method: str, location: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO release_notes_cache (image_repo, method, location, last_success_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(image_repo) DO UPDATE SET
                method = excluded.method,
                location = excluded.location,
                last_success_at = excluded.last_success_at
            """,
            (image_repo, method, location, now_iso()),
        )


# ---------------------------------------------------------------------------
# Last check result — persisted so "last checked" survives a container restart,
# not just kept in memory (which was resetting to "no check has run yet" on
# every restart even though checks had genuinely run before).
# ---------------------------------------------------------------------------

def get_last_check_result(feature: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (f"last_check_result_{feature}",)
        )
        row = cur.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def set_last_check_result(feature: str, result: dict, at_iso: str) -> None:
    payload = {"result": result, "at": at_iso}
    _set_setting(f"last_check_result_{feature}", json.dumps(payload))

import hashlib
import json
import os
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
    last_checked_at TEXT,
    silenced INTEGER NOT NULL DEFAULT 0  -- an EOL/always-flagged container the operator muted
);

CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    image_repo TEXT NOT NULL,
    tag TEXT NOT NULL,
    old_digest TEXT,
    new_digest TEXT,
    release_notes_raw TEXT,
    summary_markdown TEXT,
    source_url TEXT,
    status TEXT NOT NULL DEFAULT 'unread',
    error TEXT,
    severity TEXT NOT NULL DEFAULT '',
    upgrade_guidance TEXT,              -- only populated when Deep Analysis is on for updates
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
    read_status TEXT NOT NULL DEFAULT 'unread',  -- unread or read -- independent of active/silenced
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(source, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_findings_source ON findings(source);
-- The subject pages, stack-page member loops, and bulk read/silence toggles all filter by
-- (source, subject); the source-only index above leaves those scanning every row of that
-- source. UNIQUE(source, fingerprint) already covers the upsert path's lookup.
CREATE INDEX IF NOT EXISTS idx_findings_source_subject ON findings(source, subject);

CREATE TABLE IF NOT EXISTS log_watch_state (
    container_name TEXT PRIMARY KEY,
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log_check_errors (
    -- Deliberately a separate table from log_watch_state rather than a nullable column on it:
    -- log_watch_state.last_checked_at is the checkpoint get_container_logs_since() reads as
    -- "since" on the next check, and it must never advance on a failed attempt (an errored
    -- fetch must keep retrying from the same cutoff -- or the full lookback window, for a
    -- container that's never once succeeded -- not silently narrow to "since the failed
    -- attempt"). Keeping errors here means a container that has NEVER had a single successful
    -- check (so has no log_watch_state row at all) still shows up as needing attention, instead
    -- of being invisible everywhere just because it's never once succeeded.
    container_name TEXT PRIMARY KEY,
    error TEXT NOT NULL,
    last_error_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compose_file_state (
    file_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    last_reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compose_check_errors (
    -- Compose's counterpart to log_check_errors -- same reasoning: kept separate from
    -- compose_file_state rather than a nullable column on it, since a failed check must never
    -- advance that file's stored content_hash (the "skip if unchanged" checkpoint the next
    -- check compares against), and a file that has NEVER once succeeded (unreadable from the
    -- very first check) still needs to show up as needing attention despite having no
    -- compose_file_state row at all.
    file_path TEXT PRIMARY KEY,
    error TEXT NOT NULL,
    last_error_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS compose_files (
    -- Compose's counterpart to stacks above, minus the AI-generation/services_hash machinery --
    -- a compose file's display name is just its own services: keys joined together (see
    -- compose_lookup.subject_display_name), computed fresh on every lookup rather than cached,
    -- so there's nothing to invalidate the way an AI-generated stack name needs services_hash
    -- for. This table exists purely to hold a manual override once an operator sets one.
    file_path TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    name_source TEXT NOT NULL DEFAULT 'computed',  -- 'computed' or 'manual'
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS container_names (
    -- A container's own display-name override -- an explicit ask: stacks and compose files
    -- were already renameable, a bare container name (Updates' detail.html, Logs' subject
    -- page) was the one place left that wasn't. Keyed by container_name directly (unlike
    -- compose_files' file_path), and independent of both container_state (Updates-only) and
    -- log_watch_state (Logs-only) -- a container can appear in either, both, or neither, so
    -- this can't live as a column on either of those without losing overrides for the other
    -- feature's own view of the same name. Always 'manual' once a row exists at all -- there's
    -- no AI-generated name to protect here the way stacks/compose files have (the raw Docker
    -- container name IS the computed default, nothing to invalidate).
    container_name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stack_analyses (
    -- stack_id is a PRIMARY KEY column holding a "{stack_id}:{source}" compound value (see
    -- _stack_analysis_key below) rather than a real composite PRIMARY KEY -- SQLite can't ALTER
    -- a PRIMARY KEY on an existing table without a full rebuild, so this lets an existing
    -- install's stack_analyses rows (all originally Updates') get migrated forward with a plain
    -- ALTER TABLE + UPDATE instead. "updates" and "logs" cross-service analyses for the same
    -- physical stack_id (a compose file's own path, shared identity across features) are
    -- otherwise indistinguishable rows that would silently overwrite each other.
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


def _get_setting(key: str, default: str, conn: sqlite3.Connection | None = None) -> str:
    with get_conn(conn) as c:
        cur = c.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
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
    # Migration: pre-rebrand installs (release-radar) stored the database under the old
    # filename. Move it forward under the new name on first startup after the upgrade so an
    # existing install's history isn't orphaned.
    _legacy_db_path = settings.data_dir / "release_radar.db"
    if not settings.db_path.exists() and _legacy_db_path.exists():
        _legacy_db_path.rename(settings.db_path)
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
        # Same pattern for the findings table's read_status column (per-finding Read/Unread,
        # mirroring the updates table's own status column).
        try:
            conn.execute("ALTER TABLE findings ADD COLUMN read_status TEXT NOT NULL DEFAULT 'unread'")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        # Same pattern for the updates table's release_notes_raw column (Stage 6).
        try:
            conn.execute("ALTER TABLE updates ADD COLUMN release_notes_raw TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        # Same pattern for container_state's silenced column (an EOL container that will
        # always show an update -- muted at the container level, independent of whatever
        # pending update row exists/gets recreated as digests keep changing).
        try:
            conn.execute("ALTER TABLE container_state ADD COLUMN silenced INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        # Same pattern for the updates table's upgrade_guidance column (Deep Analysis for
        # Updates, added alongside the per-finding suggested_fix Logs/Compose already had).
        try:
            conn.execute("ALTER TABLE updates ADD COLUMN upgrade_guidance TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

        # Migration: deep_analysis_updates_enabled used to be the ONLY Updates-related Deep
        # Analysis toggle, meaning "stack-wide cross-service analysis" -- the same concept Deep
        # Analysis means for Logs/Compose (an opt-in extra AI pass) got its own genuinely
        # per-item toggle added later (upgrade guidance on an individual update, mirroring Logs/
        # Compose's per-finding suggested fix), so the key names needed to stop meaning two
        # different things. Renamed to cross_service_analysis_updates_enabled, preserving
        # whatever value an existing install already had; deep_analysis_updates_enabled is reset
        # to 'false' so it starts fresh as the new per-item toggle's key. Guarded by a marker row
        # so this runs exactly once ever -- without the guard, every subsequent boot would reset
        # deep_analysis_updates_enabled back to 'false' even after a user legitimately turns the
        # new per-item feature on.
        migrated = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'migrated_cross_service_updates_rename'"
        ).fetchone()
        if migrated is None:
            old = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'deep_analysis_updates_enabled'"
            ).fetchone()
            if old is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('cross_service_analysis_updates_enabled', ?)",
                    (old["value"],),
                )
                conn.execute(
                    "UPDATE app_settings SET value = 'false' WHERE key = 'deep_analysis_updates_enabled'"
                )
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES ('migrated_cross_service_updates_rename', 'true')"
            )

        # Migration: stack_analyses rows created before Cross-Service Analysis existed for Logs
        # are all implicitly Updates' -- rewrite their stack_id to the "{stack_id}:updates"
        # compound form get_stack_analysis/set_stack_analysis now use (see the table's own
        # comment above), so they keep matching instead of silently becoming orphaned rows a
        # fresh Updates analysis would just re-create. Guarded the same way: a bare (no ':')
        # stack_id is exactly what identifies a not-yet-migrated row, so this is naturally
        # idempotent even without a separate marker.
        conn.execute(
            "UPDATE stack_analyses SET stack_id = stack_id || ':updates' WHERE stack_id NOT LIKE '%:updates' AND stack_id NOT LIKE '%:logs'"
        )

        # Explicitly seed defaults rather than relying only on read-time fallbacks — this is
        # the same belt-and-suspenders approach as the feature toggles above, and avoids any
        # ambiguity in what a fresh install's severity pickers show on first load.
        # Updates uses its own 4-tier scale (bugfix/feature/action_needed/breaking), separate
        # from the 3-tier scale (suggestion/warning/critical) Logs and Compose still use.
        default_settings = {
            "notify_severity_updates": "bugfix",
            "notify_severity_logs": DEFAULT_SEVERITY,
            "notify_severity_compose": DEFAULT_SEVERITY,
            "deep_analysis_logs_enabled": "false",
            "deep_analysis_compose_enabled": "false",
            "ai_provider": "anthropic",
            "anthropic_model": "claude-sonnet-5",
            "gemini_model": "gemini-2.5-flash",
        }
        for key, value in default_settings.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, value)
            )

        # Migration: the AI provider key/model used to live only in the compose file's
        # ANTHROPIC_API_KEY/CLAUDE_MODEL env vars (see app/ai_provider.py) — one-time carry
        # those into the database on an install that still has them set, so upgrading doesn't
        # silently lose a working key. INSERT OR IGNORE means this only ever seeds an empty
        # slot; it never overwrites a key already saved from the Settings page.
        env_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if env_key:
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                ("anthropic_api_key", env_key),
            )
        env_model = os.environ.get("CLAUDE_MODEL", "")
        if env_model:
            conn.execute(
                "UPDATE app_settings SET value = ? WHERE key = 'anthropic_model' AND value = 'claude-sonnet-5'",
                (env_model,),
            )

        # Migration: GITHUB_TOKEN used to be compose-file-only too -- same one-time carry-over,
        # never overwriting a token already saved from the Settings page.
        env_github_token = os.environ.get("GITHUB_TOKEN", "")
        if env_github_token:
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                ("github_token", env_github_token),
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
def get_conn(existing: sqlite3.Connection | None = None):
    # WAL mode lets readers and a writer proceed concurrently without blocking each other,
    # which matters now that release-notes fetching and summarization can write from several
    # threads at once. The explicit timeout is a generous but still-bounded wait for the rare
    # case two writers do collide, so a moment of real contention fails gracefully (an
    # exception the caller can catch) rather than either racing incorrectly or hanging.
    #
    # Accepts an already-open connection to reuse instead of opening/committing/closing a new
    # one — every one of those steps is a real syscall (and a WAL commit means an fsync), so a
    # loop calling several db.py functions per iteration (see app/persist.py) was paying for
    # hundreds of connect+commit+close cycles on what's conceptually one batch of writes. When
    # reusing an existing connection, the caller who opened it owns committing/closing it.
    if existing is not None:
        yield existing
        return
    conn = sqlite3.connect(settings.db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def open_conn() -> sqlite3.Connection:
    """Opens a connection with the same setup get_conn() uses, for a caller that wants to
    hold it across several db.py calls as one transaction (pass it as `conn=` to each) instead
    of every call opening/committing/closing its own — see app/persist.py. The caller owns
    calling commit() and close() when done."""
    conn = sqlite3.connect(settings.db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


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
# Timezone — one global setting (there's only one scheduler/system clock, so unlike
# schedules there's no per-feature override concept here). Seeded from the TZ env var on
# first boot so existing deployments keep behaving the same until someone changes it from
# the Settings page; from then on the database is authoritative.
# ---------------------------------------------------------------------------

def get_timezone() -> str:
    return _get_setting("timezone", settings.tz)


def set_timezone(tz: str) -> None:
    _set_setting("timezone", tz)


# ---------------------------------------------------------------------------
# Release notes lookback — how far back Updates will compile missed releases from when a
# container's digest has moved past more than one release since the last check. Always
# bounded by the container's own last-checked time first (see persist._release_notes_since);
# this setting only ever tightens that further, as a ceiling on prompt size/AI cost for a
# container that's gone unchecked for a very long time. "since_check" means no additional
# ceiling at all.
# ---------------------------------------------------------------------------

RELEASE_NOTES_LOOKBACK_DAYS = {
    "since_check": None,
    "7": 7,
    "30": 30,
    "90": 90,
    "180": 180,
    "365": 365,
}


def get_release_notes_lookback(conn: sqlite3.Connection | None = None) -> str:
    return _get_setting("release_notes_lookback", "since_check", conn=conn)


def set_release_notes_lookback(value: str) -> None:
    _set_setting("release_notes_lookback", value)


def get_release_notes_lookback_days(conn: sqlite3.Connection | None = None) -> int | None:
    return RELEASE_NOTES_LOOKBACK_DAYS.get(get_release_notes_lookback(conn=conn))


# ---------------------------------------------------------------------------
# Logs lookback — how far back a container's log fetch reaches when it isn't using its
# checkpoint (see get_logs_use_checkpoint below) for that fetch. Always a concrete hour count on
# purpose -- an earlier "no limit, since last reset" option was removed after a real-world
# concern: a container with weeks of verbose logs and no recent checkpoint could make Docker
# seek through all of that just to serve the fetch. Default is 6 hours.
#
# get_logs_use_checkpoint/set_logs_use_checkpoint is the separate on/off switch this pairs
# with. ON (the default) is today's normal behavior: a container with a checkpoint fetches
# strictly since it (incremental, ignoring this hour setting entirely), and this hour window
# only ever applies as the fallback for one with none (never checked, or just reset -- see
# reset_logs_data, which always clears the checkpoint on reset). OFF makes every check, for
# every container, always use this fixed hour window regardless of any stored checkpoint --
# checkpoints still get recorded either way (other things display "last checked" from them),
# just never consulted for the fetch bound while this is off, so flipping it back on later
# picks up incrementally right away rather than losing that history.
# ---------------------------------------------------------------------------

LOGS_LOOKBACK_HOURS = {
    "1": 1,
    "3": 3,
    "6": 6,
    "12": 12,
    "24": 24,
    "72": 72,
    "168": 168,
}


def get_logs_lookback(conn: sqlite3.Connection | None = None) -> str:
    return _get_setting("logs_lookback", "6", conn=conn)


def set_logs_lookback(value: str) -> None:
    _set_setting("logs_lookback", value)


def get_logs_lookback_hours(conn: sqlite3.Connection | None = None) -> int:
    # .get(..., 6) rather than a direct index -- an upgrade from a version that still had the
    # now-removed "since_reset" option could have that stale value sitting in the DB.
    return LOGS_LOOKBACK_HOURS.get(get_logs_lookback(conn=conn), 6)


def get_logs_use_checkpoint(conn: sqlite3.Connection | None = None) -> bool:
    return _get_setting("logs_use_checkpoint", "true", conn=conn) == "true"


def set_logs_use_checkpoint(value: bool) -> None:
    _set_setting("logs_use_checkpoint", "true" if value else "false")


# ---------------------------------------------------------------------------
# Schedules — a "master" schedule everything uses by default, with an optional
# per-feature override. Stored as small JSON specs (see schedule_spec.py) rather
# than raw cron strings, so the UI can offer a plain Hourly/Daily/Weekly/Monthly
# frequency picker with no cron entry anywhere.
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
# a per-feature on/off, and each feature's own severity threshold (no shared/general severity
# to fall back to — every feature always uses its own value directly, same as Updates already
# did; a general severity with per-feature overrides used to exist here but added a layer of
# indirection that mostly just meant "the button that looks selected right now is the wrong
# one" whenever a feature was quietly still following the general value).
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


def get_notify_updates_include_errors() -> bool:
    # Defaults to off: a registry check failure (network blip, temporary rate limit) is a
    # different, noisier kind of event than a real update, and Updates' own severity
    # threshold doesn't apply to it -- opt-in only, unlike the feature-level toggle above.
    return _get_setting("notify_updates_include_errors", "false") == "true"


def set_notify_updates_include_errors(enabled: bool) -> None:
    _set_setting("notify_updates_include_errors", "true" if enabled else "false")


def get_notify_logs_include_errors() -> bool:
    # Same opt-in-only reasoning as get_notify_updates_include_errors -- a container whose logs
    # couldn't be fetched (Docker socket blip, container removed mid-check) is a noisier,
    # different kind of event than a real finding, and doesn't have a severity to threshold on.
    return _get_setting("notify_logs_include_errors", "false") == "true"


def set_notify_logs_include_errors(enabled: bool) -> None:
    _set_setting("notify_logs_include_errors", "true" if enabled else "false")


def get_notify_compose_include_errors() -> bool:
    # Same opt-in-only reasoning as get_notify_logs_include_errors -- a compose file that
    # couldn't be read or reviewed is a noisier, different kind of event than a real finding,
    # and doesn't have a severity to threshold on.
    return _get_setting("notify_compose_include_errors", "false") == "true"


def set_notify_compose_include_errors(enabled: bool) -> None:
    _set_setting("notify_compose_include_errors", "true" if enabled else "false")


def get_feature_severity(feature: str) -> str:
    # Updates uses its own 4-tier scale (bugfix/feature/action_needed/breaking); Logs and
    # Compose share a 3-tier scale (suggestion/warning/critical) — each feature's default is
    # its own scale's lowest tier, so a fresh install always has a real, valid value selected
    # rather than falling back to a default from the wrong scale (which would match none of
    # that feature's buttons and look like nothing was ever chosen).
    default = "bugfix" if feature == "updates" else DEFAULT_SEVERITY
    return _get_setting(f"notify_severity_{feature}", default)


def set_feature_severity(feature: str, value: str) -> None:
    _set_setting(f"notify_severity_{feature}", value)


def get_effective_severity(feature: str) -> str:
    return get_feature_severity(feature)


# ---------------------------------------------------------------------------
# Container digest tracking (updates feature)
# ---------------------------------------------------------------------------

def get_container_state(container_name: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    with get_conn(conn) as c:
        cur = c.execute(
            "SELECT * FROM container_state WHERE container_name = ?", (container_name,)
        )
        return cur.fetchone()


def upsert_container_state(container_name: str, image_repo: str, tag: str, digest: str | None,
                            conn: sqlite3.Connection | None = None) -> None:
    with get_conn(conn) as c:
        c.execute(
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


def set_container_silenced(container_name: str, silenced: bool) -> None:
    """Mutes/unmutes a container at the container level -- independent of any single pending
    update row, which persist.py deletes and recreates as digests keep changing. An EOL
    container that will always show a new tag needs this to stick across every future check,
    not just the update that happened to exist when Silence was clicked."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE container_state SET silenced = ? WHERE container_name = ?",
            (1 if silenced else 0, container_name),
        )


def set_containers_silenced(container_names: list[str], silenced: bool) -> None:
    """Bulk counterpart to set_container_silenced -- one connection for the whole list rather
    than one per container, same discipline as silence_all_findings_for_subjects. Used by
    Updates' stack-level Silence/Unsilence (see main.py), which used to not exist at all -- only
    the per-container route did -- so a real-world ask to bulk-mute a whole retired stack had no
    single-click way to do it."""
    if not container_names:
        return
    with get_conn() as conn:
        qs = ",".join("?" * len(container_names))
        conn.execute(
            f"UPDATE container_state SET silenced = ? WHERE container_name IN ({qs})",
            (1 if silenced else 0, *container_names),
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


def prune_removed_containers(seen_container_names: list[str], conn: sqlite3.Connection | None = None) -> None:
    """Deletes container_state/updates rows for containers that no longer exist (removed or
    renamed since the last check) — keeps persisted state in sync with what's actually
    running rather than accumulating stale entries forever. Only ever called with a non-empty
    list (see app/persist.py) — a check that found zero containers almost always means the
    Docker socket itself was unreachable, not that everything was genuinely decommissioned,
    so callers deliberately never prune off an empty result."""
    if not seen_container_names:
        return
    with get_conn(conn) as c:
        placeholders = ",".join("?" * len(seen_container_names))
        c.execute(f"DELETE FROM container_state WHERE container_name NOT IN ({placeholders})", seen_container_names)
        c.execute(f"DELETE FROM updates WHERE container_name NOT IN ({placeholders})", seen_container_names)


def list_tracked_containers_with_status() -> list[dict]:
    """The persisted equivalent of a fresh reconcile.run_check() outcome's "containers" list —
    every tracked container, each annotated with its current status ("update_available",
    "error", or "up_to_date") and, when relevant, the real database id of its pending update
    record. Backed by a single LEFT JOIN rather than N+1 queries, and safe to call on every
    page load/poll since it's a plain indexed read, not a live Docker/registry call."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT
                cs.container_name AS container_name,
                cs.image_repo AS image_repo,
                cs.tag AS tag,
                cs.last_checked_at AS last_checked_at,
                cs.silenced AS silenced,
                u.id AS id,
                u.error AS error,
                u.severity AS severity,
                u.summary_markdown AS summary_markdown,
                u.source_url AS source_url,
                u.status AS read_status,
                u.created_at AS update_created_at,
                u.release_notes_raw AS release_notes_raw
            FROM container_state cs
            LEFT JOIN updates u ON u.container_name = cs.container_name
            ORDER BY cs.container_name COLLATE NOCASE ASC
            """
        )
        rows = cur.fetchall()

    result = []
    for r in rows:
        status = "error" if r["error"] else ("update_available" if r["id"] is not None else "up_to_date")
        result.append({
            "container_name": r["container_name"],
            "image_repo": r["image_repo"],
            "tag": r["tag"],
            "status": status,
            "silenced": bool(r["silenced"]),
            "id": r["id"],
            "severity": r["severity"] or None,
            "error": r["error"],
            "summary_markdown": r["summary_markdown"],
            "source_url": r["source_url"],
            "release_notes_raw": r["release_notes_raw"],
            # "unread"/"read" -- only meaningful when an update row exists (id is not None);
            # None for an up_to_date container with no row at all.
            "read_status": r["read_status"],
            "last_checked_at": r["last_checked_at"],
            "created_at": r["update_created_at"] or r["last_checked_at"],
        })
    return result


def list_containers_for_stack_analysis() -> list[dict]:
    """The check-outcome-shaped "containers" list stacks.run_stack_analysis_pass() expects
    (container_name, image_repo, tag, current_digest, latest_digest), built from whatever's
    currently persisted rather than a fresh reconcile.py outcome -- used by the Updates page's
    bulk Regenerate AI Response (persist.run_claimed_bulk_regenerate), which has no fresh check
    outcome of its own to draw from and covers every tracked container at once, so (unlike
    stacks.members_for_analysis, scoped to one stack) this must be one query for the whole
    fleet rather than one db.get_container_state()/get_latest_update_for_container() call per
    container -- see test_persist.py's own connection-count regression tests for why that
    matters here specifically."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT
                cs.container_name AS container_name,
                cs.image_repo AS image_repo,
                cs.tag AS tag,
                cs.last_seen_digest AS current_digest,
                u.new_digest AS latest_digest
            FROM container_state cs
            LEFT JOIN updates u ON u.container_name = cs.container_name
            ORDER BY cs.container_name COLLATE NOCASE ASC
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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
    severity: str = "",
    release_notes_raw: str | None = None,
    upgrade_guidance: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    with get_conn(conn) as c:
        cur = c.execute(
            """
            INSERT INTO updates
                (container_name, image_repo, tag, old_digest, new_digest, release_notes_raw, summary_markdown, source_url, error, severity, upgrade_guidance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (container_name, image_repo, tag, old_digest, new_digest, release_notes_raw, summary_markdown, source_url, error, severity, upgrade_guidance, now_iso()),
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
    """Backs the global "Reset & re-check" button on the Updates page: wipes all persisted
    Updates history and the tracked-container digest baseline, so the next check treats every
    currently-installed container as fresh. This is also what cleans up any stale rows left
    over from before Stage 3 introduced real persistence (e.g. old severity values from a
    since-replaced classification scheme) — a container with a mismatched or unrecognized
    prior state gets its old row replaced on the very next check regardless (see
    app/persist.py), so this button isn't strictly required for that, but it's the fastest
    way to force a fully clean slate on demand."""
    with get_conn() as conn:
        conn.execute("DELETE FROM updates")
        conn.execute("DELETE FROM container_state")


def get_update(update_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM updates WHERE id = ?", (update_id,))
        return cur.fetchone()


def get_latest_update_for_container(container_name: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    with get_conn(conn) as c:
        cur = c.execute(
            "SELECT * FROM updates WHERE container_name = ? ORDER BY created_at DESC LIMIT 1",
            (container_name,),
        )
        return cur.fetchone()


def update_existing_update(update_id: int, summary_markdown: str | None, severity: str,
                           error: str | None, source_url: str | None,
                           upgrade_guidance: str | None = None) -> None:
    """Regenerates an existing update record in place (used by the manual Retry button) —
    keeps the same id, container, tag, and digests, just refreshes the AI-generated content
    and clears/resets status back to unread so the fresh content gets seen. upgrade_guidance
    defaults to None (cleared) rather than preserving whatever was there before -- a
    regenerate call always reflects the current Deep Analysis toggle state, same as every
    other regenerated field here, not a stale guidance blob from whenever it was last on."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE updates
            SET summary_markdown = ?, severity = ?, error = ?, source_url = ?, status = 'unread',
                upgrade_guidance = ?
            WHERE id = ?
            """,
            (summary_markdown, severity, error, source_url, upgrade_guidance, update_id),
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


def delete_update(update_id: int, conn: sqlite3.Connection | None = None) -> None:
    """Removes one update record outright — there's no separate "resolved" flag. An update
    row existing at all means that container currently needs attention (a pending update or a
    check error); once it's resolved (digest catches up) or gets superseded by a newer
    transition, app/persist.py deletes it rather than marking it done, so at most one row per
    container ever exists and every read path can treat "a row exists" as "still pending"."""
    with get_conn(conn) as c:
        c.execute("DELETE FROM updates WHERE id = ?", (update_id,))


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


def set_finding_read_status(finding_id: int, read_status: str) -> None:
    """Independent of set_finding_status (active/silenced) -- a finding's read/unread state,
    mirroring db.mark_update_status for the updates table."""
    with get_conn() as conn:
        conn.execute("UPDATE findings SET read_status = ? WHERE id = ?", (read_status, finding_id))


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


def get_feature_health_streak(feature: str) -> dict:
    """The state ("healthy"/"unhealthy") a feature's Overview hero has read continuously since
    a given timestamp -- backs the "Healthy for N days"/"Issues for N days" line. None/None the
    very first time a feature is ever observed (see update_feature_health_streak)."""
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (f"health_streak_{feature}_state",))
        state_row = cur.fetchone()
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (f"health_streak_{feature}_since",))
        since_row = cur.fetchone()
    return {
        "healthy": (state_row["value"] == "healthy") if state_row else None,
        "since": since_row["value"] if since_row else None,
    }


def update_feature_health_streak(feature: str, healthy_now: bool) -> str:
    """Called on every fresh Overview count read (see main._build_card) rather than requiring
    every check pipeline (persist.py, log_watch, compose_review) to separately remember to
    report a transition here -- cheap (two point reads, and a write only on an actual flip) and
    accurate to well within a day, which is all a day-granularity streak display needs. Returns
    the (possibly just-reset) timestamp the current state began."""
    current = get_feature_health_streak(feature)
    if current["healthy"] is not None and current["healthy"] == healthy_now:
        return current["since"]
    now = now_iso()
    state = "healthy" if healthy_now else "unhealthy"
    with get_conn() as conn:
        for key, value in ((f"health_streak_{feature}_state", state), (f"health_streak_{feature}_since", now)):
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    return now


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


def get_active_findings_by_subject(source: str, subjects: list[str]) -> dict[str, list[dict]]:
    """Batched -- one connection for the whole list, same discipline as
    get_log_watch_checkpoints -- rather than one query per subject. Only active (not silenced)
    findings are included: silencing is an operator's own "I know, ignore it" call, not
    something the system should be trying to auto-resolve out from under them. Feeds Logs'
    AI-driven resolution check (log_watcher.run_log_check_for / summarizer.analyze_logs_batch),
    which gets each subject's currently open findings alongside its newly fetched logs and
    judges whether any are no longer happening."""
    if not subjects:
        return {}
    with get_conn() as conn:
        qs = ",".join("?" * len(subjects))
        cur = conn.execute(
            f"SELECT subject, id, title, description_markdown FROM findings "
            f"WHERE source = ? AND status = 'active' AND subject IN ({qs})",
            [source] + subjects,
        )
        result: dict[str, list[dict]] = {}
        for row in cur.fetchall():
            result.setdefault(row["subject"], []).append(
                {"id": row["id"], "title": row["title"], "description": row["description_markdown"]}
            )
        return result


def resolve_finding(source: str, subject: str, title: str) -> bool:
    """Deletes an active finding once the AI has judged it resolved (see
    get_active_findings_by_subject's own docstring) -- fully removed, not just marked, so the
    subject reads as healthy again the moment its last open finding clears, same as if the issue
    had simply never recurred. Matches by fingerprint (the same source+subject+title identity
    upsert_finding already keys on) rather than title alone, so a title that happens to collide
    with a different subject's finding can never cross-resolve it. Returns whether anything was
    actually deleted -- a title the AI got slightly wrong, or one already resolved/silenced since
    the check started, quietly matches nothing rather than erroring."""
    fingerprint = make_fingerprint(source, subject, title)
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM findings WHERE source = ? AND subject = ? AND fingerprint = ? AND status = 'active'",
            (source, subject, fingerprint),
        )
        return cur.rowcount > 0


def list_subjects_with_findings(source: str, include_silenced: bool = False) -> list[dict]:
    """One row per subject (container or compose file) with aggregate counts and the highest
    severity present — used for the grouped 'Issues' list at the top of the Logs/Compose tabs,
    so you see one line per container rather than one line per individual finding.

    include_silenced is a swap, not an additive reveal: False (default) shows only subjects
    with at least one active finding -- something is currently actionable, regardless of
    whether it also has older silenced ones sitting alongside it. True shows exclusively
    subjects that have findings but NONE of them are active anymore (fully silenced) --
    a genuinely different list, not a superset. Severity is computed from whichever set of
    findings is actually being shown (active_* columns for the default view, silenced_* for
    the include_silenced view) so a subject's badge never reflects findings the row isn't
    counting toward it."""
    having = "active_count = 0 AND silenced_count > 0" if include_silenced else "active_count > 0"
    with get_conn() as conn:
        cur = conn.execute(
            f"""
            SELECT subject,
                   COUNT(*) AS finding_count,
                   MAX(last_seen_at) AS last_seen_at,
                   SUM(CASE WHEN status = 'active' AND severity = 'critical' THEN 1 ELSE 0 END) AS active_critical_count,
                   SUM(CASE WHEN status = 'active' AND severity = 'warning' THEN 1 ELSE 0 END) AS active_warning_count,
                   SUM(CASE WHEN status = 'silenced' AND severity = 'critical' THEN 1 ELSE 0 END) AS silenced_critical_count,
                   SUM(CASE WHEN status = 'silenced' AND severity = 'warning' THEN 1 ELSE 0 END) AS silenced_warning_count,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_count,
                   SUM(CASE WHEN status = 'silenced' THEN 1 ELSE 0 END) AS silenced_count,
                   SUM(CASE WHEN status = 'active' AND read_status = 'unread' THEN 1 ELSE 0 END) AS unread_count
            FROM findings
            WHERE source = ?
            GROUP BY subject
            HAVING {having}
            ORDER BY last_seen_at DESC
            """,
            (source,),
        )
        rows = []
        for r in cur.fetchall():
            row = dict(r)
            critical = row["silenced_critical_count"] if include_silenced else row["active_critical_count"]
            warning = row["silenced_warning_count"] if include_silenced else row["active_warning_count"]
            if critical:
                row["top_severity"] = "critical"
            elif warning:
                row["top_severity"] = "warning"
            else:
                row["top_severity"] = "suggestion"
            rows.append(row)
        return rows


_UPDATE_ATTENTION_TIER = {"breaking": "critical", "action_needed": "warning"}
_FINDING_ATTENTION_TIER = {"critical": "critical", "warning": "warning"}
_ATTENTION_TIER_RANK = {"critical": 2, "warning": 1}


def list_attention_items_for_feature(feature: str, limit: int = 3) -> list[dict]:
    """Top `limit` most-severe currently-actionable items for a single module, shown inside
    that module's own Overview row (see main._build_card, which calls this once per module on
    every card render/poll -- scoped to one feature rather than computing all three every time).
    Updates' own 4-tier severity (bugfix/feature/action_needed/breaking) and Logs/Compose's
    3-tier one (suggestion/warning/critical) don't share a vocabulary, so each maps onto the
    same critical/warning scale here -- a plain bugfix/feature update or a suggestion-level
    finding never appears, since neither is something that actually needs "attention", just
    something to know about. A container whose check itself failed always counts as critical
    regardless of its stored severity (which is meaningless once the check didn't complete).

    Ranked by tier first (critical above warning), most-recently-seen within a tier second --
    same actionable-and-not-silenced set _updates_pending_count/list_findings already use, so
    this never surfaces something silenced or already resolved elsewhere on the page."""
    items: list[dict] = []
    if feature == "updates":
        for row in list_tracked_containers_with_status():
            if row["status"] not in ("update_available", "error") or row.get("silenced"):
                continue
            error = row["status"] == "error"
            tier = "critical" if error else _UPDATE_ATTENTION_TIER.get(row["severity"])
            if tier is None:
                continue
            items.append({
                "source": "updates", "tier": tier, "error": error,
                "name": row["container_name"], "severity": row["severity"],
                "blurb": "Check failed" if error else "New version available",
                "url": f"/updates/{row['id']}" if row.get("id") else "/updates",
                "at": row.get("created_at") or "",
            })
    else:
        for f in list_findings(feature):
            tier = _FINDING_ATTENTION_TIER.get(f["severity"])
            if tier is None:
                continue
            items.append({
                "source": feature, "tier": tier, "error": False,
                "name": f["subject"], "severity": f["severity"], "blurb": f["title"],
                "url": f"/findings/{f['id']}",
                "at": f["last_seen_at"] or "",
            })
    items.sort(key=lambda i: (_ATTENTION_TIER_RANK[i["tier"]], i["at"]), reverse=True)
    return items[:limit]


def _row_silence_state(active: int, total: int) -> str | None:
    """None / "partially_silenced" / "silenced" -- same 3-state model as main._silence_state,
    duplicated here (rather than imported) since db.py has no dependency on main.py. "silenced"
    only when there's at least one finding and every one of them is silenced; "partially_
    silenced" when some but not all are (e.g. one finding silenced individually while another
    stays active)."""
    silenced = total - active
    if total == 0 or silenced == 0:
        return None
    return "silenced" if active == 0 else "partially_silenced"


def record_log_check_errors(errors: dict[str, str]) -> None:
    """errors: {container_name: error_message}. Upserts one row per failed container in a
    single connection -- this is what makes a persistently-failing container visible at all
    (the 'All containers' table, the check's own error count, the opt-in notify-on-error
    toggle) instead of silently vanishing. A container that has never once succeeded never gets
    a log_watch_state row (see set_log_watch_checkpoints), so without this table it would never
    appear anywhere at all, no matter how many times its check has failed."""
    if not errors:
        return
    now = now_iso()
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO log_check_errors (container_name, error, last_error_at) VALUES (?, ?, ?)
            ON CONFLICT(container_name) DO UPDATE SET error = excluded.error, last_error_at = excluded.last_error_at
            """,
            [(name, error, now) for name, error in errors.items()],
        )


def clear_log_check_errors(container_names: list[str]) -> None:
    """Clears a previously-recorded check error the moment a container's logs are fetched
    successfully again -- called for every container that DID succeed this pass, batched."""
    if not container_names:
        return
    with get_conn() as conn:
        qs = ",".join("?" * len(container_names))
        conn.execute(f"DELETE FROM log_check_errors WHERE container_name IN ({qs})", container_names)


def all_log_watch_states_with_status() -> list[dict]:
    """Every container the log watcher has ever checked OR ever failed to check, with a
    healthy/issue/error status — used for the 'All containers' list at the bottom of the Logs
    tab. "silence_state" is derived from the findings themselves (see _row_silence_state) rather
    than an explicit per-container toggle like Updates has -- Logs/Compose silence at the
    finding, service (subject), or stack level, all ultimately just bulk actions over these same
    rows. "error" wins over "issue"/"healthy" regardless of finding count -- a container whose
    logs can't even be fetched needs attention just as much as one with active findings, same
    tiering Updates gives its own check-error rows."""
    with get_conn() as conn:
        cur = conn.execute("SELECT container_name, last_checked_at FROM log_watch_state")
        checked = {r["container_name"]: r["last_checked_at"] for r in cur.fetchall()}
        cur = conn.execute("SELECT container_name, error, last_error_at FROM log_check_errors")
        errors = {r["container_name"]: dict(r) for r in cur.fetchall()}

        result = []
        for name in sorted(set(checked) | set(errors)):
            cur2 = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active, "
                "COUNT(*) AS total "
                "FROM findings WHERE source = 'logs' AND subject = ?",
                (name,),
            )
            counts = cur2.fetchone()
            active, total = counts["active"] or 0, counts["total"] or 0
            err = errors.get(name)
            result.append({
                "name": name,
                "last_at": checked.get(name) or (err["last_error_at"] if err else None),
                "status": "error" if err else ("issue" if active else "healthy"),
                "error": err["error"] if err else None,
                "silence_state": _row_silence_state(active, total),
            })
        return result


def record_compose_check_errors(errors: dict[str, str]) -> None:
    """errors: {file_path: error_message}. Compose's counterpart to record_log_check_errors --
    upserts one row per file that failed this check in a single connection."""
    if not errors:
        return
    now = now_iso()
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO compose_check_errors (file_path, error, last_error_at) VALUES (?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET error = excluded.error, last_error_at = excluded.last_error_at
            """,
            [(path, error, now) for path, error in errors.items()],
        )


def clear_compose_check_errors(file_paths: list[str]) -> None:
    """Clears a previously-recorded check error the moment a file is read successfully again --
    called for every file that DID succeed this pass, batched."""
    if not file_paths:
        return
    with get_conn() as conn:
        qs = ",".join("?" * len(file_paths))
        conn.execute(f"DELETE FROM compose_check_errors WHERE file_path IN ({qs})", file_paths)


def all_compose_file_states_with_status() -> list[dict]:
    """Every compose file the reviewer has ever checked OR ever failed to check, with a
    healthy/issue/error status — used for the 'All files' list at the bottom of the Compose
    tab. See all_log_watch_states_with_status above for why "silence_state" is derived rather
    than an explicit toggle, and why "error" wins over "issue"/"healthy" regardless of finding
    count -- a file that can't even be read needs attention just as much as one with active
    findings."""
    with get_conn() as conn:
        cur = conn.execute("SELECT file_path, last_reviewed_at FROM compose_file_state")
        checked = {r["file_path"]: r["last_reviewed_at"] for r in cur.fetchall()}
        cur = conn.execute("SELECT file_path, error, last_error_at FROM compose_check_errors")
        errors = {r["file_path"]: dict(r) for r in cur.fetchall()}

        result = []
        for path in sorted(set(checked) | set(errors)):
            cur2 = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active, "
                "COUNT(*) AS total "
                "FROM findings WHERE source = 'compose' AND subject = ?",
                (path,),
            )
            counts = cur2.fetchone()
            active, total = counts["active"] or 0, counts["total"] or 0
            err = errors.get(path)
            result.append({
                "name": path,
                "last_at": checked.get(path) or (err["last_error_at"] if err else None),
                "status": "error" if err else ("issue" if active else "healthy"),
                "error": err["error"] if err else None,
                "silence_state": _row_silence_state(active, total),
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


def get_log_watch_checkpoints(container_names: list[str]) -> dict[str, str]:
    """Batched equivalent of get_log_watch_checkpoint -- one connection for the whole list
    instead of one per container, same "read once into a dict, thread it through" shape as
    persist.py's container_state_by_name batching for Updates."""
    if not container_names:
        return {}
    with get_conn() as conn:
        qs = ",".join("?" * len(container_names))
        cur = conn.execute(
            f"SELECT container_name, last_checked_at FROM log_watch_state WHERE container_name IN ({qs})",
            container_names,
        )
        return {r["container_name"]: r["last_checked_at"] for r in cur.fetchall()}


def set_log_watch_checkpoints(container_names: list[str]) -> None:
    """Batched equivalent of set_log_watch_checkpoint -- stamps every given container's
    checkpoint to "now" in a single connection/transaction."""
    if not container_names:
        return
    now = now_iso()
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO log_watch_state (container_name, last_checked_at) VALUES (?, ?)
            ON CONFLICT(container_name) DO UPDATE SET last_checked_at = excluded.last_checked_at
            """,
            [(name, now) for name in container_names],
        )


def reset_logs_data(subjects: list[str] | None = None) -> None:
    """Logs' equivalent of reset_updates_data(): wipes persisted findings, the per-container
    checkpoint, and any cached overview blurb, so the next check re-scans that container fresh
    -- as if seeing it for the first time -- rather than "since last checkpoint" (which is what
    an ordinary Check now always does when checkpoints are in use, see get_logs_use_checkpoint).
    subjects=None (the global Reset & re-check button) wipes every Logs subject; a given list
    scopes the wipe to just those container names (stack- or service-level Reset & re-check)."""
    with get_conn() as conn:
        if subjects is None:
            conn.execute("DELETE FROM findings WHERE source = 'logs'")
            conn.execute("DELETE FROM log_watch_state")
            conn.execute("DELETE FROM subject_summaries WHERE source = 'logs'")
            conn.execute("DELETE FROM log_check_errors")
        elif subjects:
            qs = ",".join("?" * len(subjects))
            conn.execute(f"DELETE FROM findings WHERE source = 'logs' AND subject IN ({qs})", subjects)
            conn.execute(f"DELETE FROM log_watch_state WHERE container_name IN ({qs})", subjects)
            conn.execute(f"DELETE FROM subject_summaries WHERE source = 'logs' AND subject IN ({qs})", subjects)
            conn.execute(f"DELETE FROM log_check_errors WHERE container_name IN ({qs})", subjects)


def reset_compose_data(subjects: list[str] | None = None) -> None:
    """Compose's equivalent of reset_logs_data(): wipes persisted findings, the per-file
    content-hash checkpoint, and any cached overview blurb, so the next check reviews that
    file fresh regardless of whether its content actually changed (which is what an ordinary
    Check now would otherwise skip via the hash-unchanged short-circuit). subjects=None (the
    global Reset & re-check button) wipes every Compose subject; a given list scopes the wipe
    to just those file paths (service-level Reset & re-check -- Compose has no stack concept,
    see the Cross-Service Analysis docstring above, so there's no stack-level scope here)."""
    with get_conn() as conn:
        if subjects is None:
            conn.execute("DELETE FROM findings WHERE source = 'compose'")
            conn.execute("DELETE FROM compose_file_state")
            conn.execute("DELETE FROM subject_summaries WHERE source = 'compose'")
            conn.execute("DELETE FROM compose_check_errors")
        elif subjects:
            qs = ",".join("?" * len(subjects))
            conn.execute(f"DELETE FROM findings WHERE source = 'compose' AND subject IN ({qs})", subjects)
            conn.execute(f"DELETE FROM compose_file_state WHERE file_path IN ({qs})", subjects)
            conn.execute(f"DELETE FROM subject_summaries WHERE source = 'compose' AND subject IN ({qs})", subjects)
            conn.execute(f"DELETE FROM compose_check_errors WHERE file_path IN ({qs})", subjects)


def silence_all_findings_for_subjects(source: str, subjects: list[str]) -> None:
    """Bulk silence -- every currently active finding for the given subjects becomes silenced.
    A finding that appears later starts active again regardless, which is what correctly
    demotes a fully-"Silenced" service/stack back to "Partially Silenced" once something new
    shows up -- this is an action applied to today's active rows, not a persistent mute flag."""
    if not subjects:
        return
    with get_conn() as conn:
        qs = ",".join("?" * len(subjects))
        conn.execute(
            f"UPDATE findings SET status = 'silenced' WHERE source = ? AND subject IN ({qs}) AND status = 'active'",
            (source, *subjects),
        )


def unsilence_all_findings_for_subjects(source: str, subjects: list[str]) -> None:
    """Bulk unsilence -- the reverse of silence_all_findings_for_subjects above."""
    if not subjects:
        return
    with get_conn() as conn:
        qs = ",".join("?" * len(subjects))
        conn.execute(
            f"UPDATE findings SET status = 'active' WHERE source = ? AND subject IN ({qs}) AND status = 'silenced'",
            (source, *subjects),
        )


def set_findings_read_status_for_subject(source: str, subject: str, read_status: str) -> None:
    """Bulk read/unread -- flips every currently active finding for one subject at once, the
    service-level counterpart to set_finding_read_status's per-finding toggle. Silenced findings
    are left alone: they're not part of what the unread badge counts (see main._findings_summary),
    so there's nothing meaningful to mark read/unread on them."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE findings SET read_status = ? WHERE source = ? AND subject = ? AND status = 'active'",
            (read_status, source, subject),
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


def get_compose_file_hashes(file_paths: list[str]) -> dict[str, str]:
    """Batched equivalent of get_compose_file_hash -- one connection for the whole list instead
    of one per file, same "read once into a dict, thread it through" shape as db.get_log_watch_
    checkpoints. Used by compose_reviewer.run_compose_check_for's fast sequential pass, which
    otherwise opened one connection per file in that loop even though nothing in that phase
    needs a fresh read mid-loop -- every path is known upfront."""
    if not file_paths:
        return {}
    with get_conn() as conn:
        qs = ",".join("?" * len(file_paths))
        cur = conn.execute(
            f"SELECT file_path, content_hash FROM compose_file_state WHERE file_path IN ({qs})",
            file_paths,
        )
        return {r["file_path"]: r["content_hash"] for r in cur.fetchall()}


def get_compose_file_checkpoint(file_path: str) -> str | None:
    """Compose's equivalent of get_log_watch_checkpoint -- when this file was last actually
    reviewed, for the subject page's "Last checked ..." empty-state line."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT last_reviewed_at FROM compose_file_state WHERE file_path = ?", (file_path,)
        )
        row = cur.fetchone()
        return row["last_reviewed_at"] if row else None


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


def set_compose_file_hashes(hashes_by_path: dict[str, str]) -> None:
    """Batched equivalent of set_compose_file_hash -- one connection for the whole dict instead
    of one per file. Used for the files stamped during compose_reviewer.run_compose_check_for's
    fast sequential pass (unreadable-as-redacted files that get skipped without a real AI
    review); files that go through an actual AI review still stamp their own hash individually
    right after that review completes (see _review_one), same as Logs' per-finding writes --
    those happen during genuinely concurrent, network-bound work, not a tight local-I/O loop."""
    if not hashes_by_path:
        return
    now = now_iso()
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO compose_file_state (file_path, content_hash, last_reviewed_at) VALUES (?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET content_hash = excluded.content_hash, last_reviewed_at = excluded.last_reviewed_at
            """,
            [(path, content_hash, now) for path, content_hash in hashes_by_path.items()],
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
# Cross-Service Analysis — a distinct opt-in toggle from Deep Analysis above, off by
# default, for "updates" and "logs" only ("compose" doesn't need it -- a compose file's
# services are already grouped together in the same file, there's no separate cross-file
# stack concept for Compose the way there is for Updates/Logs, which key off container
# names matched against compose_lookup's stack index instead of a file boundary). When on,
# a stack-wide AI blurb gets generated: "could this update/finding in one service affect
# others in the same compose stack."
# ---------------------------------------------------------------------------

def get_cross_service_analysis_enabled(feature: str) -> bool:
    return _get_setting(f"cross_service_analysis_{feature}_enabled", "false") == "true"


def set_cross_service_analysis_enabled(feature: str, enabled: bool) -> None:
    _set_setting(f"cross_service_analysis_{feature}_enabled", "true" if enabled else "false")


# ---------------------------------------------------------------------------
# AI provider (moved off compose-file env vars and into Settings so a provider/model/key can
# be changed without a redeploy — the whole point being able to switch away from a provider
# that's temporarily out of credits without touching the compose file at all). Only one
# provider is ever active at a time; see app/ai_provider.py for how everything that used to
# call anthropic.Anthropic() directly now goes through this instead.
# ---------------------------------------------------------------------------

def get_ai_provider() -> str:
    return _get_setting("ai_provider", "anthropic")


def set_ai_provider(provider: str) -> None:
    _set_setting("ai_provider", provider)


def get_anthropic_api_key() -> str:
    return _get_setting("anthropic_api_key", "")


def set_anthropic_api_key(key: str) -> None:
    _set_setting("anthropic_api_key", key)


def get_anthropic_model() -> str:
    return _get_setting("anthropic_model", "claude-sonnet-5")


def set_anthropic_model(model: str) -> None:
    _set_setting("anthropic_model", model)


def get_gemini_api_key() -> str:
    return _get_setting("gemini_api_key", "")


def set_gemini_api_key(key: str) -> None:
    _set_setting("gemini_api_key", key)


def get_gemini_model() -> str:
    return _get_setting("gemini_model", "gemini-2.5-flash")


def set_gemini_model(model: str) -> None:
    _set_setting("gemini_model", model)


# How many AI calls persist.py's fan-out phases run at once for each provider -- previously a
# single value shared by both (see AI_SUMMARIZE_CONCURRENCY in config.py), now per-provider and
# UI-editable since the right number genuinely differs by provider (and by tier within a
# provider) rather than being one global constant. Clamped to 1-10 by the routes in main.py that
# write these; read paths trust the stored value since nothing else can write it.
AI_CONCURRENCY_MIN = 1
AI_CONCURRENCY_MAX = 10
AI_CONCURRENCY_DEFAULT = 4


def get_anthropic_concurrency() -> int:
    return int(_get_setting("anthropic_concurrency", str(AI_CONCURRENCY_DEFAULT)))


def set_anthropic_concurrency(value: int) -> None:
    _set_setting("anthropic_concurrency", str(value))


def get_gemini_concurrency() -> int:
    return int(_get_setting("gemini_concurrency", str(AI_CONCURRENCY_DEFAULT)))


def set_gemini_concurrency(value: int) -> None:
    _set_setting("gemini_concurrency", str(value))


def get_github_token() -> str:
    return _get_setting("github_token", "")


def set_github_token(token: str) -> None:
    _set_setting("github_token", token)


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


def get_compose_file_name(file_path: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM compose_files WHERE file_path = ?", (file_path,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_compose_file_name(file_path: str, display_name: str, name_source: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO compose_files (file_path, display_name, name_source, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                display_name = excluded.display_name,
                name_source = excluded.name_source,
                updated_at = excluded.updated_at
            """,
            (file_path, display_name, name_source, now_iso()),
        )


def reset_compose_file_name(file_path: str) -> None:
    """Clears a manual override so the display name goes back to being computed fresh from
    the file's own services: keys on every lookup."""
    with get_conn() as conn:
        conn.execute("DELETE FROM compose_files WHERE file_path = ?", (file_path,))


def get_container_display_name(container_name: str) -> str | None:
    """None means no override -- callers fall back to the raw container_name itself, same
    "computed default, manual override optional" shape as compose_files' own get."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT display_name FROM container_names WHERE container_name = ?", (container_name,)
        )
        row = cur.fetchone()
        return row["display_name"] if row else None


def get_container_display_names(container_names: list[str]) -> dict[str, str]:
    """Batched equivalent of get_container_display_name -- one connection for the whole list
    instead of one per row, for table listings that need every visible container's override (if
    any) at once rather than looking each one up individually."""
    if not container_names:
        return {}
    with get_conn() as conn:
        qs = ",".join("?" * len(container_names))
        cur = conn.execute(
            f"SELECT container_name, display_name FROM container_names WHERE container_name IN ({qs})",
            container_names,
        )
        return {r["container_name"]: r["display_name"] for r in cur.fetchall()}


def set_container_display_name(container_name: str, display_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO container_names (container_name, display_name, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(container_name) DO UPDATE SET
                display_name = excluded.display_name, updated_at = excluded.updated_at
            """,
            (container_name, display_name, now_iso()),
        )


def reset_container_display_name(container_name: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM container_names WHERE container_name = ?", (container_name,))


# ---------------------------------------------------------------------------
# Cached stack-wide cross-service analysis
# ---------------------------------------------------------------------------

def _stack_analysis_key(stack_id: str, source: str) -> str:
    """See stack_analyses' own schema comment -- "updates" and "logs" cross-service analyses
    for the same physical stack_id need independent rows, and SQLite can't add a real
    composite PRIMARY KEY to an existing table without a full rebuild."""
    return f"{stack_id}:{source}"


def get_stack_analysis(stack_id: str, source: str = "updates") -> dict | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM stack_analyses WHERE stack_id = ?", (_stack_analysis_key(stack_id, source),)
        )
        row = cur.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["stack_id"] = stack_id  # strip the ":source" suffix back off for callers
        return result


def set_stack_analysis(stack_id: str, content_hash: str, analysis_markdown: str, source: str = "updates") -> None:
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
            (_stack_analysis_key(stack_id, source), content_hash, analysis_markdown, now_iso()),
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

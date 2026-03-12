"""SQLite database with thread-safe connections, schema management, and CRUD operations."""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from bot import config

logger = logging.getLogger(__name__)

_local = threading.local()

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_config (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK(id = 1),
    channel_policy TEXT NOT NULL DEFAULT 'channel',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gitlab_project_path TEXT NOT NULL,
    gitlab_project_id INTEGER,
    slack_channel_id TEXT NOT NULL,
    slack_user_id TEXT NOT NULL,
    delivery_target TEXT NOT NULL,
    delivery_channel_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'digest',
    schedule TEXT NOT NULL DEFAULT '0 9 * * 1-5',
    poll_interval TEXT NOT NULL DEFAULT '*/5 * * * *',
    include_drafts INTEGER NOT NULL DEFAULT 0,
    filter_labels TEXT,
    filter_branch TEXT,
    lifecycle_enabled INTEGER NOT NULL DEFAULT 1,
    notify_approvals INTEGER NOT NULL DEFAULT 1,
    suppress_empty INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    pause_reason TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    mr_iid INTEGER NOT NULL,
    mr_state TEXT NOT NULL,
    mr_updated_at TEXT NOT NULL,
    last_notified_at TEXT NOT NULL,
    resolved_at TEXT,
    last_approval_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(subscription_id, mr_iid)
);

CREATE TABLE IF NOT EXISTS project_cache (
    gitlab_project_path TEXT PRIMARY KEY,
    gitlab_project_id INTEGER NOT NULL,
    cached_at TEXT NOT NULL
);
"""

_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_sub_channel ON subscriptions(slack_channel_id, status);
CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(slack_user_id, status);
CREATE INDEX IF NOT EXISTS idx_sub_status ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_sub_project ON subscriptions(gitlab_project_path);
CREATE INDEX IF NOT EXISTS idx_ns_sub ON notification_state(subscription_id);
"""

# Partial unique indexes for dedup
_PARTIAL_INDEXES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_channel_dedup
    ON subscriptions(gitlab_project_path, slack_channel_id)
    WHERE delivery_target = 'channel';
CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_user_dedup
    ON subscriptions(gitlab_project_path, slack_user_id)
    WHERE delivery_target = 'user_dm';
CREATE INDEX IF NOT EXISTS idx_ns_resolved
    ON notification_state(resolved_at)
    WHERE resolved_at IS NOT NULL;
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    """Get a thread-local SQLite connection with WAL mode and foreign keys."""
    conn = getattr(_local, "connection", None)
    if conn is None:
        import os
        os.makedirs(os.path.dirname(config.DATABASE_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.connection = conn
    return conn


def init_db() -> None:
    """Create all tables, indexes, and seed data."""
    conn = get_connection()
    conn.executescript(_SCHEMA_SQL)
    conn.executescript(_INDEXES_SQL)
    conn.executescript(_PARTIAL_INDEXES_SQL)
    _ensure_bot_config(conn)
    _ensure_schema_version(conn)
    conn.commit()
    logger.info("Database initialized", extra={"schema_version": _SCHEMA_VERSION})


def _ensure_bot_config(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT id FROM bot_config WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO bot_config (id, channel_policy, updated_at) VALUES (1, 'channel', ?)",
            (_now_iso(),),
        )


def _ensure_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (_SCHEMA_VERSION, _now_iso()),
        )


# --- Bot Config ---


def get_channel_policy() -> str:
    """Return the current channel policy ('channel' or 'app')."""
    conn = get_connection()
    row = conn.execute("SELECT channel_policy FROM bot_config WHERE id = 1").fetchone()
    return row["channel_policy"] if row else "channel"


def update_channel_policy(policy: str) -> None:
    """Update the global channel policy."""
    conn = get_connection()
    conn.execute(
        "UPDATE bot_config SET channel_policy = ?, updated_at = ? WHERE id = 1",
        (policy, _now_iso()),
    )
    conn.commit()


# --- Project Cache ---


def get_cached_project(path: str) -> int | None:
    """Return cached project ID if fresh (< 24h), else None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT gitlab_project_id, cached_at FROM project_cache WHERE gitlab_project_path = ?",
        (path,),
    ).fetchone()
    if row is None:
        return None
    cached_at = datetime.fromisoformat(row["cached_at"])
    if datetime.now(timezone.utc) - cached_at > timedelta(hours=24):
        return None
    return row["gitlab_project_id"]


def set_cached_project(path: str, project_id: int) -> None:
    """Insert or update a project cache entry."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO project_cache (gitlab_project_path, gitlab_project_id, cached_at) "
        "VALUES (?, ?, ?)",
        (path, project_id, _now_iso()),
    )
    conn.commit()


def invalidate_cached_project(path: str) -> None:
    """Remove a project cache entry."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM project_cache WHERE gitlab_project_path = ?", (path,)
    )
    conn.commit()


# --- Subscriptions ---


def create_subscription(
    project_path: str,
    project_id: int | None,
    channel_id: str,
    user_id: str,
    delivery_target: str,
    delivery_channel_id: str,
) -> dict:
    """Create a new subscription with defaults. Returns the subscription as a dict."""
    conn = get_connection()
    now = _now_iso()
    cursor = conn.execute(
        """INSERT INTO subscriptions (
            gitlab_project_path, gitlab_project_id, slack_channel_id, slack_user_id,
            delivery_target, delivery_channel_id, mode, schedule, poll_interval,
            include_drafts, lifecycle_enabled, suppress_empty,
            status, consecutive_failures, created_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 0, 'active', 0, ?, ?, ?)""",
        (
            project_path, project_id, channel_id, user_id,
            delivery_target, delivery_channel_id,
            config.DEFAULT_MODE, config.DEFAULT_SCHEDULE, config.DEFAULT_POLL_INTERVAL,
            user_id, now, now,
        ),
    )
    conn.commit()
    return get_subscription_by_id(cursor.lastrowid)


def get_subscription_by_id(subscription_id: int) -> dict | None:
    """Get a subscription by its ID."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
    ).fetchone()
    return dict(row) if row else None


def get_subscription(
    project_path: str, channel_id: str, user_id: str, delivery_target: str
) -> dict | None:
    """Find a subscription for dedup checking.

    Under channel policy: match by project + channel.
    Under app policy: match by project + user.
    """
    conn = get_connection()
    if delivery_target == "channel":
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE gitlab_project_path = ? "
            "AND slack_channel_id = ? AND delivery_target = 'channel'",
            (project_path, channel_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE gitlab_project_path = ? "
            "AND slack_user_id = ? AND delivery_target = 'user_dm'",
            (project_path, user_id),
        ).fetchone()
    return dict(row) if row else None


def get_subscriptions_for_context(
    channel_id: str, user_id: str, channel_policy: str
) -> list[dict]:
    """Get all subscriptions for a given context (channel or user)."""
    conn = get_connection()
    if channel_policy == "channel":
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE slack_channel_id = ? ORDER BY created_at",
            (channel_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE slack_user_id = ? AND delivery_target = 'user_dm' "
            "ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_subscription_by_project_in_context(
    project_path: str, channel_id: str, user_id: str, channel_policy: str
) -> dict | None:
    """Find a specific subscription in the current context."""
    conn = get_connection()
    if channel_policy == "channel":
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE gitlab_project_path = ? "
            "AND slack_channel_id = ? AND delivery_target = 'channel'",
            (project_path, channel_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE gitlab_project_path = ? "
            "AND slack_user_id = ? AND delivery_target = 'user_dm'",
            (project_path, user_id),
        ).fetchone()
    return dict(row) if row else None


def get_active_subscriptions() -> list[dict]:
    """Get all active subscriptions (for scheduler rebuild on startup)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE status = 'active'"
    ).fetchall()
    return [dict(r) for r in rows]


def update_subscription(subscription_id: int, **fields) -> dict | None:
    """Update specified fields on a subscription."""
    allowed = {
        "mode", "schedule", "include_drafts", "filter_labels", "filter_branch",
        "lifecycle_enabled", "suppress_empty", "status", "pause_reason",
        "consecutive_failures", "gitlab_project_id", "poll_interval",
        "notify_approvals", "delivery_target", "delivery_channel_id",
    }
    to_update = {k: v for k, v in fields.items() if k in allowed}
    if not to_update:
        return get_subscription_by_id(subscription_id)

    to_update["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in to_update)
    values = list(to_update.values()) + [subscription_id]

    conn = get_connection()
    conn.execute(
        f"UPDATE subscriptions SET {set_clause} WHERE id = ?",
        values,
    )
    conn.commit()
    return get_subscription_by_id(subscription_id)


def delete_subscription(subscription_id: int) -> None:
    """Delete a subscription (CASCADE deletes notification_state)."""
    conn = get_connection()
    conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
    conn.commit()


def increment_consecutive_failures(subscription_id: int) -> int:
    """Increment and return the new consecutive_failures count."""
    conn = get_connection()
    conn.execute(
        "UPDATE subscriptions SET consecutive_failures = consecutive_failures + 1, "
        "updated_at = ? WHERE id = ?",
        (_now_iso(), subscription_id),
    )
    conn.commit()
    sub = get_subscription_by_id(subscription_id)
    return sub["consecutive_failures"] if sub else 0


def pause_subscription(subscription_id: int, reason: str) -> None:
    """Pause a subscription with a reason."""
    conn = get_connection()
    conn.execute(
        "UPDATE subscriptions SET status = 'paused', pause_reason = ?, updated_at = ? "
        "WHERE id = ?",
        (reason, _now_iso(), subscription_id),
    )
    conn.commit()


def resume_subscription(subscription_id: int) -> None:
    """Resume a paused subscription."""
    conn = get_connection()
    conn.execute(
        "UPDATE subscriptions SET status = 'active', pause_reason = NULL, "
        "consecutive_failures = 0, updated_at = ? WHERE id = ?",
        (_now_iso(), subscription_id),
    )
    conn.commit()


def get_subscription_counts() -> dict:
    """Return counts of active and paused subscriptions."""
    conn = get_connection()
    active = conn.execute(
        "SELECT COUNT(*) as c FROM subscriptions WHERE status = 'active'"
    ).fetchone()["c"]
    paused = conn.execute(
        "SELECT COUNT(*) as c FROM subscriptions WHERE status = 'paused'"
    ).fetchone()["c"]
    return {"active_count": active, "paused_count": paused}


# --- Notification State ---


def upsert_notification_state(
    subscription_id: int, mr_iid: int, mr_state: str, mr_updated_at: str,
    approval_count: int = 0,
) -> None:
    """Insert or update notification state for an MR."""
    conn = get_connection()
    now = _now_iso()
    conn.execute(
        """INSERT INTO notification_state
            (subscription_id, mr_iid, mr_state, mr_updated_at, last_notified_at, last_approval_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(subscription_id, mr_iid) DO UPDATE SET
            mr_state = excluded.mr_state,
            mr_updated_at = excluded.mr_updated_at,
            last_notified_at = excluded.last_notified_at,
            last_approval_count = excluded.last_approval_count""",
        (subscription_id, mr_iid, mr_state, mr_updated_at, now, approval_count),
    )
    conn.commit()


def get_notification_states(subscription_id: int) -> dict[int, dict]:
    """Return notification states keyed by mr_iid."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM notification_state WHERE subscription_id = ?",
        (subscription_id,),
    ).fetchall()
    return {row["mr_iid"]: dict(row) for row in rows}


def update_notification_state_resolved(
    subscription_id: int, mr_iid: int, new_state: str
) -> None:
    """Mark a notification state as resolved (merged/closed)."""
    conn = get_connection()
    now = _now_iso()
    conn.execute(
        "UPDATE notification_state SET mr_state = ?, resolved_at = ?, last_notified_at = ? "
        "WHERE subscription_id = ? AND mr_iid = ?",
        (new_state, now, now, subscription_id, mr_iid),
    )
    conn.commit()


def prune_resolved_states(days: int = 30) -> int:
    """Delete notification states resolved more than `days` ago. Returns count deleted."""
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cursor = conn.execute(
        "DELETE FROM notification_state WHERE resolved_at IS NOT NULL AND resolved_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount

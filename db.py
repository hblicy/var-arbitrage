"""SQLite database layer for multi-user position management.

Tables:
  - users: id, username, password_hash, display_name, role, wecom_webhook, enabled, created_at
  - positions: id, symbol, direction, long_exchange, short_exchange,
              entry_funding_long, entry_funding_short, entry_qty,
              entry_price_long, entry_price_short, entry_time,
              owner_id, health_status, reversal_reason, last_signal_at, last_notified_at,
              cumulative_funding, funding_history (JSON), current_funding_long,
              current_funding_short, current_price_long, current_price_short,
              last_settlement_long, last_settlement_short,
              pre_settlement_rate_long, pre_settlement_rate_short,
              snapshot_time_long, snapshot_time_short,
              snapshot_interval_long, snapshot_interval_short,
              status, created_at, updated_at
  - alert_logs: id, position_id, owner_id, trigger_type, message,
                sent_at, cooldown_key
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional, Any

import sqlite3

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logger = logging.getLogger(__name__)

# Default database path — can be overridden via env
DB_PATH = os.getenv("ARBITRAGE_DB_PATH", "data/arbitrage.db")

# Module-level lock for thread-safe writes
_write_lock = threading.RLock()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


@contextmanager
def _get_conn():
    """Get a thread-local SQLite connection with dict factory."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist. Called at startup."""
    with _get_conn() as conn:
        cur = conn.cursor()

        # ── users table ──────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                username    TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                role        TEXT NOT NULL DEFAULT 'trader',
                wecom_webhook TEXT,
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

        # ── positions table ───────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id                      TEXT PRIMARY KEY,
                symbol                  TEXT NOT NULL,
                direction               TEXT NOT NULL,
                long_exchange           TEXT NOT NULL,
                short_exchange          TEXT NOT NULL,
                entry_funding_long      REAL NOT NULL DEFAULT 0,
                entry_funding_short     REAL NOT NULL DEFAULT 0,
                entry_qty               REAL NOT NULL DEFAULT 0,
                entry_price_long        REAL NOT NULL DEFAULT 0,
                entry_price_short       REAL NOT NULL DEFAULT 0,
                entry_time              TEXT NOT NULL,

                owner_id                TEXT NOT NULL,
                health_status           TEXT NOT NULL DEFAULT 'healthy',
                reversal_reason         TEXT,
                last_signal_at          TEXT,
                last_notified_at        REAL,

                cumulative_funding      REAL NOT NULL DEFAULT 0,
                funding_history         TEXT NOT NULL DEFAULT '[]',

                current_funding_long   REAL,
                current_funding_short   REAL,
                current_price_long     REAL,
                current_price_short    REAL,

                last_settlement_long    TEXT,
                last_settlement_short   TEXT,
                pre_settlement_rate_long REAL,
                pre_settlement_rate_short REAL,
                snapshot_time_long      TEXT,
                snapshot_time_short     TEXT,
                snapshot_interval_long  INTEGER,
                snapshot_interval_short INTEGER,

                status                  TEXT NOT NULL DEFAULT 'open',
                created_at              TEXT NOT NULL,
                updated_at              TEXT NOT NULL,

                FOREIGN KEY (owner_id) REFERENCES users(id)
            )
        """)

        # Indexes for common queries
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_owner ON positions(owner_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_owner_status ON positions(owner_id, status)")

        # ── alert_logs table ──────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS alert_logs (
                id           TEXT PRIMARY KEY,
                position_id  TEXT NOT NULL,
                owner_id     TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                message      TEXT,
                sent_at      TEXT NOT NULL,
                cooldown_key TEXT NOT NULL
            )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_cooldown ON alert_logs(cooldown_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_position ON alert_logs(position_id)")

        conn.commit()
        logger.info("Database initialized at %s", DB_PATH)


# ══════════════════════════════════════════════════════════════════
# User Management
# ══════════════════════════════════════════════════════════════════

def create_user(
    username: str,
    password_hash: str,
    display_name: str = "",
    role: str = "trader",
    wecom_webhook: str = "",
    enabled: bool = True,
) -> Dict[str, Any]:
    """Create a new user. Returns the user dict (without password_hash)."""
    now = datetime.now().isoformat()
    user_id = str(uuid.uuid4())
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO users (id, username, password_hash, display_name, role,
                                       wecom_webhook, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, username, password_hash, display_name, role,
                      wecom_webhook, int(enabled), now, now))
                conn.commit()
            except sqlite3.IntegrityError as e:
                logger.error("Failed to create user %s: %s", username, e)
                raise ValueError(f"Username '{username}' already exists")

    return {
        "id": user_id, "username": username, "display_name": display_name,
        "role": role, "wecom_webhook": wecom_webhook, "enabled": enabled,
        "created_at": now, "updated_at": now,
    }


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Fetch a user by username (includes password_hash for auth)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a user by ID (excludes password_hash)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, username, display_name, role, wecom_webhook, enabled,
                   created_at, updated_at
            FROM users WHERE id = ?
        """, (user_id,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def get_all_users(include_disabled: bool = False) -> List[Dict[str, Any]]:
    """List all users (excludes password_hash)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        if include_disabled:
            cur.execute("""
                SELECT id, username, display_name, role, wecom_webhook, enabled,
                       created_at, updated_at FROM users ORDER BY created_at
            """)
        else:
            cur.execute("""
                SELECT id, username, display_name, role, wecom_webhook, enabled,
                       created_at, updated_at FROM users WHERE enabled = 1 ORDER BY created_at
            """)
        return [_row_to_dict(r) for r in cur.fetchall()]


def update_user(
    user_id: str,
    display_name: Optional[str] = None,
    wecom_webhook: Optional[str] = None,
    role: Optional[str] = None,
    enabled: Optional[bool] = None,
    password_hash: Optional[str] = None,
) -> bool:
    """Update user fields. Returns True if updated."""
    now = datetime.now().isoformat()
    fields = ["updated_at = ?"]
    values = [now]
    if display_name is not None:
        fields.append("display_name = ?")
        values.append(display_name)
    if wecom_webhook is not None:
        fields.append("wecom_webhook = ?")
        values.append(wecom_webhook)
    if role is not None:
        fields.append("role = ?")
        values.append(role)
    if enabled is not None:
        fields.append("enabled = ?")
        values.append(int(enabled))
    if password_hash is not None:
        fields.append("password_hash = ?")
        values.append(password_hash)
    values.append(user_id)
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
            return cur.rowcount > 0


def delete_user(user_id: str) -> bool:
    """Delete a user (cascades to positions)."""
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM positions WHERE owner_id = ?", (user_id,))
            cur.execute("DELETE FROM alert_logs WHERE owner_id = ?", (user_id,))
            cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            return cur.rowcount > 0


# ══════════════════════════════════════════════════════════════════
# Position Management
# ══════════════════════════════════════════════════════════════════

def create_position(
    symbol: str,
    direction: str,
    long_exchange: str,
    short_exchange: str,
    entry_funding_long: float,
    entry_funding_short: float,
    entry_qty: float,
    entry_price_long: float,
    entry_price_short: float,
    owner_id: str,
) -> Dict[str, Any]:
    """Create a new position belonging to owner_id (auto-generates ID)."""
    now_dt = datetime.now()
    now = now_dt.isoformat()
    ts_suffix = now_dt.strftime("%m%d%H%M%S")
    position_id = f"{symbol.upper()}_{long_exchange.lower()}_{short_exchange.lower()}_{ts_suffix}".upper()

    position = {
        "id": position_id,
        "symbol": symbol.upper(),
        "direction": direction,
        "long_exchange": long_exchange.lower(),
        "short_exchange": short_exchange.lower(),
        "entry_funding_long": entry_funding_long,
        "entry_funding_short": entry_funding_short,
        "entry_qty": entry_qty,
        "entry_price_long": entry_price_long,
        "entry_price_short": entry_price_short,
        "entry_time": now,
        "owner_id": owner_id,
        "health_status": "healthy",
        "reversal_reason": None,
        "last_signal_at": None,
        "last_notified_at": None,
        "cumulative_funding": 0.0,
        "funding_history": "[]",
        "current_funding_long": None,
        "current_funding_short": None,
        "current_price_long": None,
        "current_price_short": None,
        "last_settlement_long": None,
        "last_settlement_short": None,
        "pre_settlement_rate_long": None,
        "pre_settlement_rate_short": None,
        "snapshot_time_long": None,
        "snapshot_time_short": None,
        "snapshot_interval_long": None,
        "snapshot_interval_short": None,
        "status": "open",
        "created_at": now,
        "updated_at": now,
    }

    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO positions (
                    id, symbol, direction, long_exchange, short_exchange,
                    entry_funding_long, entry_funding_short, entry_qty,
                    entry_price_long, entry_price_short, entry_time,
                    owner_id, health_status, reversal_reason, last_signal_at, last_notified_at,
                    cumulative_funding, funding_history,
                    current_funding_long, current_funding_short,
                    current_price_long, current_price_short,
                    last_settlement_long, last_settlement_short,
                    pre_settlement_rate_long, pre_settlement_rate_short,
                    snapshot_time_long, snapshot_time_short,
                    snapshot_interval_long, snapshot_interval_short,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(position.values()))
            conn.commit()

    logger.info("Created position %s for owner %s", position_id, owner_id)
    return position


def create_position_from_migration(
    legacy_id: str,
    symbol: str,
    direction: str,
    long_exchange: str,
    short_exchange: str,
    entry_funding_long: float,
    entry_funding_short: float,
    entry_qty: float,
    entry_price_long: float,
    entry_price_short: float,
    entry_time: str,
    owner_id: str,
    cumulative_funding: float,
    funding_history: List[Dict],
    current_funding_long: Optional[float] = None,
    current_funding_short: Optional[float] = None,
    current_price_long: Optional[float] = None,
    current_price_short: Optional[float] = None,
    last_settlement_long: Optional[str] = None,
    last_settlement_short: Optional[str] = None,
    health_status: str = "healthy",
    reversal_reason: Optional[str] = None,
    last_signal_at: Optional[str] = None,
    last_notified_at: Optional[float] = None,
    status: str = "open",
) -> Dict[str, Any]:
    """Insert a position preserving the legacy ID and all accumulated state.

    Used by migrate_to_sqlite.py to migrate existing positions without
    losing their funding history, cumulative funding balance, or health state.
    """
    now = datetime.now().isoformat()

    # Resolve entry_funding from legacy nested dict or direct fields
    if isinstance(entry_funding_long, dict):
        entry_funding_long = entry_funding_long.get("long", 0) or 0
    if isinstance(entry_funding_short, dict):
        entry_funding_short = entry_funding_short.get("short", 0) or 0

    # Serialize funding_history
    if isinstance(funding_history, list):
        funding_history_str = json.dumps(funding_history, ensure_ascii=False)
    elif isinstance(funding_history, str):
        funding_history_str = funding_history
    else:
        funding_history_str = "[]"

    # Normalise health_status to known values
    valid_health = {"healthy", "watch", "reversal"}
    health_status = health_status if health_status in valid_health else "healthy"
    # Normalise status
    status = status if status in ("open", "closed") else "open"

    position = {
        "id": legacy_id.upper(),
        "symbol": symbol.upper() if symbol else legacy_id.split("_")[0].upper(),
        "direction": direction,
        "long_exchange": long_exchange.lower() if long_exchange else "unknown",
        "short_exchange": short_exchange.lower() if short_exchange else "unknown",
        "entry_funding_long": entry_funding_long,
        "entry_funding_short": entry_funding_short,
        "entry_qty": entry_qty,
        "entry_price_long": entry_price_long,
        "entry_price_short": entry_price_short,
        "entry_time": entry_time or now,
        "owner_id": owner_id,
        "health_status": health_status,
        "reversal_reason": reversal_reason,
        "last_signal_at": last_signal_at,
        "last_notified_at": last_notified_at,
        "cumulative_funding": cumulative_funding or 0.0,
        "funding_history": funding_history_str,
        "current_funding_long": current_funding_long,
        "current_funding_short": current_funding_short,
        "current_price_long": current_price_long,
        "current_price_short": current_price_short,
        "last_settlement_long": last_settlement_long,
        "last_settlement_short": last_settlement_short,
        "pre_settlement_rate_long": None,
        "pre_settlement_rate_short": None,
        "snapshot_time_long": None,
        "snapshot_time_short": None,
        "snapshot_interval_long": None,
        "snapshot_interval_short": None,
        "status": status,
        "created_at": now,
        "updated_at": now,
    }

    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO positions (
                    id, symbol, direction, long_exchange, short_exchange,
                    entry_funding_long, entry_funding_short, entry_qty,
                    entry_price_long, entry_price_short, entry_time,
                    owner_id, health_status, reversal_reason, last_signal_at, last_notified_at,
                    cumulative_funding, funding_history,
                    current_funding_long, current_funding_short,
                    current_price_long, current_price_short,
                    last_settlement_long, last_settlement_short,
                    pre_settlement_rate_long, pre_settlement_rate_short,
                    snapshot_time_long, snapshot_time_short,
                    snapshot_interval_long, snapshot_interval_short,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(position.values()))
            conn.commit()

    logger.info(
        "Migrated position %s (cumulative_funding=%.4f, history=%d records) for owner %s",
        legacy_id, cumulative_funding or 0, len(funding_history) if isinstance(funding_history, list) else 0, owner_id,
    )
    return position


def get_position_by_id(position_id: str) -> Optional[Dict[str, Any]]:
    """Get a single position by ID."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def get_positions(
    owner_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get positions filtered by owner and/or status.

    Args:
        owner_id: If None, returns positions for all owners (admin).
        status: Exact status to filter by. None = all statuses (open + closed).

    Returns:
        List of position dicts ordered by created_at DESC.
    """
    with _get_conn() as conn:
        cur = conn.cursor()
        conditions = []
        values = []
        if owner_id is not None:
            conditions.append("owner_id = ?")
            values.append(owner_id)
        if status is not None:
            conditions.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur.execute(f"SELECT * FROM positions {where} ORDER BY created_at DESC", values)
        rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]


def get_all_positions_raw() -> Dict[str, Dict[str, Any]]:
    """Get ALL open positions as dict keyed by id (for backward compat)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM positions WHERE status = 'open' ORDER BY created_at DESC")
        rows = cur.fetchall()
        return {_row_to_dict(r)["id"]: _row_to_dict(r) for r in rows}


def get_all_position_ids() -> set:
    """Get IDs of ALL positions regardless of status (for migration dedup)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM positions")
        return {row[0] for row in cur.fetchall()}


def update_position(position_id: str, **fields) -> bool:
    """Update arbitrary fields on a position."""
    if not fields:
        return False
    fields["updated_at"] = datetime.now().isoformat()
    # JSON-encode any list/dict fields
    for k, v in list(fields.items()):
        if isinstance(v, (list, dict)):
            fields[k] = json.dumps(v, ensure_ascii=False)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [position_id]
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE positions SET {set_clause} WHERE id = ?", values)
            conn.commit()
            return cur.rowcount > 0


def update_position_health(
    position_id: str,
    health_status: str,
    reversal_reason: Optional[str] = None,
    last_signal_at: Optional[str] = None,
) -> bool:
    """Update only the health tracking fields of a position."""
    now = datetime.now().isoformat()
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE positions
                SET health_status = ?, reversal_reason = ?, last_signal_at = ?,
                    updated_at = ?
                WHERE id = ?
            """, (health_status, reversal_reason, last_signal_at, now, position_id))
            conn.commit()
            return cur.rowcount > 0


def mark_position_notified(position_id: str) -> bool:
    """Record notification timestamp."""
    now = datetime.now().timestamp()
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE positions SET last_notified_at = ?, updated_at = ? WHERE id = ?
            """, (now, datetime.now().isoformat(), position_id))
            conn.commit()
            return cur.rowcount > 0


def delete_position(position_id: str) -> bool:
    """Mark a position as closed (soft delete)."""
    now = datetime.now().isoformat()
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE positions SET status = 'closed', updated_at = ? WHERE id = ?
            """, (now, position_id))
            conn.commit()
            return cur.rowcount > 0


def hard_delete_position(position_id: str) -> bool:
    """Permanently delete a position."""
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM alert_logs WHERE position_id = ?", (position_id,))
            cur.execute("DELETE FROM positions WHERE id = ?", (position_id,))
            conn.commit()
            return cur.rowcount > 0


def get_positions_by_owner_ids(owner_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Get positions grouped by owner_id. Used by notifier to dispatch per-user alerts."""
    if not owner_ids:
        return {}
    placeholders = ",".join("?" * len(owner_ids))
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT * FROM positions
            WHERE owner_id IN ({placeholders}) AND status = 'open'
            ORDER BY owner_id, created_at
        """, owner_ids)
        rows = cur.fetchall()
        result: Dict[str, List[Dict[str, Any]]] = {oid: [] for oid in owner_ids}
        for r in rows:
            d = _row_to_dict(r)
            result.setdefault(d["owner_id"], []).append(d)
        return result


# ══════════════════════════════════════════════════════════════════
# Alert Logging
# ══════════════════════════════════════════════════════════════════

def log_alert(
    position_id: str,
    owner_id: str,
    trigger_type: str,
    message: str,
    cooldown_key: str,
) -> None:
    """Record an alert that was sent."""
    now = datetime.now().isoformat()
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO alert_logs (id, position_id, owner_id, trigger_type, message, sent_at, cooldown_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (str(uuid.uuid4()), position_id, owner_id, trigger_type, message, now, cooldown_key))
            conn.commit()


def get_last_alert_time(cooldown_key: str) -> Optional[float]:
    """Get the Unix timestamp of the last alert for a cooldown key."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT sent_at FROM alert_logs
            WHERE cooldown_key = ? ORDER BY sent_at DESC LIMIT 1
        """, (cooldown_key,))
        row = cur.fetchone()
        if row:
            try:
                return datetime.fromisoformat(row["sent_at"]).timestamp()
            except Exception:
                return None
        return None

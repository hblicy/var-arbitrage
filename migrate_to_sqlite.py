#!/usr/bin/env python3
"""One-time migration script: positions.json  →  SQLite.

Preserves original position IDs and full accumulated state
(cumulative_funding, funding_history, etc.) during migration.

Usage:
    python migrate_to_sqlite.py [--owner USERNAME] [--dry-run] [--force]

    --owner USERNAME  : Assign all migrated positions to this user (default: admin)
    --dry-run         : Show what would be imported without writing to SQLite
    --force           : Re-import even if positions already exist in DB
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

POSITIONS_FILE = "positions.json"


def _read_existing_state(owner_username: str):
    """Read existing DB state without creating or modifying the database."""
    from db import DB_PATH
    import sqlite3

    if not os.path.exists(DB_PATH):
        return None, set()

    db_abs = os.path.abspath(DB_PATH).replace("\\", "/")
    try:
        conn = sqlite3.connect(f"file:{db_abs}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        try:
            cur.execute("SELECT * FROM users WHERE username = ?", (owner_username,))
            row = cur.fetchone()
            user = dict(row) if row else None
        except sqlite3.OperationalError:
            user = None

        try:
            cur.execute("SELECT id FROM positions")
            existing_ids = {row[0] for row in cur.fetchall()}
        except sqlite3.OperationalError:
            existing_ids = set()

        conn.close()
        return user, existing_ids
    except sqlite3.OperationalError:
        return None, set()


def _dry_run(owner_username: str, force: bool = False):
    user, existing_ids = _read_existing_state(owner_username)
    if user:
        print(f"[OK] Owner: {user['username']} (id={user['id']}, role={user['role']})")
    else:
        print(f"[DRY-RUN] User '{owner_username}' not found. It would be created during real migration.")

    if not os.path.exists(POSITIONS_FILE):
        print(f"[SKIP] {POSITIONS_FILE} not found - nothing to migrate.")
        return

    with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
        legacy = json.load(f)

    if not legacy:
        print("[SKIP] positions.json is empty.")
        return

    print(f"\n[INFO] Found {len(legacy)} positions in positions.json")
    print(f"[INFO] {len(existing_ids)} positions already in DB (all statuses, preserved IDs)")

    to_import = {}
    skipped = 0
    for p_id, pos in legacy.items():
        legacy_id = p_id.upper()
        if legacy_id in existing_ids and not force:
            skipped += 1
        else:
            to_import[legacy_id] = pos

    if skipped:
        print(f"[SKIP] {skipped} positions already migrated (ID matched, use --force to re-import)")

    if not to_import:
        print("[DONE] Nothing new to import.")
        return

    print(f"\n[DRY-RUN] {len(to_import)} positions -> owner '{owner_username}'...")
    for legacy_id, pos in to_import.items():
        symbol = pos.get("symbol") or legacy_id.split("_")[0]
        long_ex = pos.get("long_exchange", "unknown")
        short_ex = pos.get("short_exchange", "unknown")
        cumulative = float(pos.get("cumulative_funding", 0) or 0)
        funding_history = pos.get("funding_history", []) or []
        if isinstance(funding_history, str):
            try:
                funding_history = json.loads(funding_history)
            except Exception:
                funding_history = []
        health = pos.get("health_status", "healthy")
        print(
            f"  [DRY] {legacy_id}: {symbol} {long_ex}->{short_ex}  "
            f"cumulative={cumulative:.4f}  history={len(funding_history)} records  "
            f"health={health}"
        )

    print(f"\n[DONE] Dry-run checked {len(to_import)} positions, 0 writes.")


def migrate(owner_username: str, dry_run: bool = False, force: bool = False):
    if dry_run:
        _dry_run(owner_username, force=force)
        return

    from db import (
        init_db,
        create_user,
        get_user_by_username,
        create_position_from_migration,
        get_all_position_ids,
    )

    init_db()

    # ── Resolve owner ───────────────────────────────────────────
    user = get_user_by_username(owner_username)
    if not user:
        print(f"[ERROR] User '{owner_username}' not found in DB. Creating...")
        import bcrypt
        default_password = os.getenv("POSITION_PASS", "admin123").encode()
        hashed = bcrypt.hashpw(default_password, bcrypt.gensalt()).decode()
        user = create_user(
            username=owner_username,
            password_hash=hashed,
            display_name=owner_username.title(),
            role="admin" if owner_username == "admin" else "trader",
        )
        print(f"[OK] Created user '{owner_username}' with id={user['id']}")
    else:
        print(f"[OK] Owner: {user['username']} (id={user['id']}, role={user['role']})")

    owner_id = user["id"]

    # ── Load legacy positions ────────────────────────────────────
    if not os.path.exists(POSITIONS_FILE):
        print(f"[SKIP] {POSITIONS_FILE} not found — nothing to migrate.")
        return

    with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
        legacy = json.load(f)

    if not legacy:
        print("[SKIP] positions.json is empty.")
        return

    print(f"\n[INFO] Found {len(legacy)} positions in positions.json")

    # ── Deduplication: check by original position ID (all statuses) ──
    existing_ids = get_all_position_ids()
    print(f"[INFO] {len(existing_ids)} positions already in DB (all statuses, preserved IDs)")

    to_import = {}
    skipped = 0
    for p_id, pos in legacy.items():
        # Use the original position ID from positions.json as the DB primary key
        legacy_id = p_id.upper()
        if legacy_id in existing_ids and not force:
            skipped += 1
        else:
            to_import[legacy_id] = pos

    if skipped:
        print(f"[SKIP] {skipped} positions already migrated (ID matched, use --force to re-import)")

    if not to_import:
        print("[DONE] Nothing new to import.")
        return

    print(f"\n[{'DRY-RUN' if dry_run else 'MIGRATING'}] {len(to_import)} positions "
          f"→ owner '{owner_username}'...")

    imported = 0
    errors = 0

    for legacy_id, pos in to_import.items():
        try:
            # Extract fields — handle both nested (legacy) and flat schemas
            entry_funding = pos.get("entry_funding", {})
            if isinstance(entry_funding, dict):
                ef_long = entry_funding.get("long", 0) or 0
                ef_short = entry_funding.get("short", 0) or 0
            else:
                ef_long = ef_short = 0

            # symbol: try legacy field first, then derive from ID
            symbol = pos.get("symbol") or legacy_id.split("_")[0]

            # direction: try legacy field, construct if missing
            direction = pos.get("direction") or (
                f"{pos.get('long_exchange', '').lower()}_long_"
                f"{pos.get('short_exchange', '').lower()}_short"
            )

            # Exchanges
            long_ex = pos.get("long_exchange", "unknown")
            short_ex = pos.get("short_exchange", "unknown")

            # Timestamps
            entry_time = pos.get("entry_time", "")
            if not entry_time:
                # Try to parse timestamp suffix from legacy ID: BTCUSDT_BINANCE_HYPERLIQUID_03141705
                parts = legacy_id.split("_")
                if len(parts) >= 4:
                    ts_part = parts[-1]  # e.g. "03141705"
                    if len(ts_part) == 8 and ts_part.isdigit():
                        try:
                            entry_time = f"2026-{ts_part[:2]}-{ts_part[2:4]}T{ts_part[4:6]}:{ts_part[6:8]}:00"
                        except Exception:
                            entry_time = ""
                    else:
                        entry_time = ""
                else:
                    entry_time = ""

            # Funding state — this is the key fix for P1
            cumulative = float(pos.get("cumulative_funding", 0) or 0)
            fh_raw = pos.get("funding_history", [])
            if isinstance(fh_raw, str):
                try:
                    funding_history = json.loads(fh_raw)
                except Exception:
                    funding_history = []
            else:
                funding_history = fh_raw or []

            # Live state fields
            cur_fl = pos.get("current_funding_long")
            cur_fs = pos.get("current_funding_short")
            cur_pl = pos.get("current_price_long")
            cur_ps = pos.get("current_price_short")
            last_settle_l = pos.get("last_settlement_long")
            last_settle_s = pos.get("last_settlement_short")

            # Health / reversal state — preserve from legacy data
            health = pos.get("health_status", "healthy")
            reversal_reason = pos.get("reversal_reason")
            last_signal_at = pos.get("last_signal_at")
            last_notified_at = pos.get("last_notified_at")
            pos_status = pos.get("status", "open")

            if dry_run:
                print(
                    f"  [DRY] {legacy_id}: {symbol} {long_ex}→{short_ex}  "
                    f"cumulative={cumulative:.4f}  history={len(funding_history)} records  "
                    f"health={health}"
                )
            else:
                create_position_from_migration(
                    legacy_id=legacy_id,
                    symbol=symbol,
                    direction=direction,
                    long_exchange=long_ex,
                    short_exchange=short_ex,
                    entry_funding_long=ef_long,
                    entry_funding_short=ef_short,
                    entry_qty=float(pos.get("entry_qty", 0) or 0),
                    entry_price_long=float(pos.get("entry_price_long", 0) or 0),
                    entry_price_short=float(pos.get("entry_price_short", 0) or 0),
                    entry_time=entry_time,
                    owner_id=owner_id,
                    cumulative_funding=cumulative,
                    funding_history=funding_history,
                    current_funding_long=cur_fl,
                    current_funding_short=cur_fs,
                    current_price_long=cur_pl,
                    current_price_short=cur_ps,
                    last_settlement_long=last_settle_l,
                    last_settlement_short=last_settle_s,
                    health_status=health,
                    reversal_reason=reversal_reason,
                    last_signal_at=last_signal_at,
                    last_notified_at=last_notified_at,
                    status=pos_status,
                )
                print(f"  [OK] {legacy_id}")

            imported += 1
        except Exception as e:
            print(f"  [ERROR] {legacy_id}: {e}")
            errors += 1

    print(f"\n[DONE] Migrated {imported} positions, {errors} errors.")
    if dry_run:
        print("This was a dry-run. Run without --dry-run to actually import.")
    else:
        print(f"positions.json is preserved — delete it after verifying the import.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate positions.json to SQLite")
    parser.add_argument("--owner", default="admin",
                        help="Owner username (default: admin)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be imported")
    parser.add_argument("--force", action="store_true",
                        help="Re-import even if already in DB")
    args = parser.parse_args()
    migrate(args.owner, dry_run=args.dry_run, force=args.force)

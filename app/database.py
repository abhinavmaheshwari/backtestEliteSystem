# =====================================================================================
# app/database.py
#
# KEY DESIGN DECISIONS:
#
# 1. ONE-TIME INIT:  init_db() is guarded by a module-level lock + flag.
#    No matter how many scanners call it simultaneously, the CREATE TABLE
#    SQL runs exactly once per process lifetime. After that, every call
#    returns immediately — zero DB round trips, zero race conditions.
#
# 2. WHY STILL CALL init_db() IN EACH SCANNER?
#    On a fresh Railway deploy the table doesn't exist yet. We can't remove
#    the call entirely. But with the lock it's safe for all scanners to call
#    it — the second caller just sees _DB_INITIALIZED=True and returns.
#
# 3. RACE CONDITION FIX:
#    The old crash was:
#      psycopg2.errors.UniqueViolation: duplicate key value violates
#      unique constraint "pg_type_typname_nsp_index"
#    This happens when Postgres processes two simultaneous CREATE TABLE
#    statements for the same table name even with IF NOT EXISTS — it's a
#    known Postgres internal type-registry bug under concurrency.
#    The lock below makes it impossible for two threads to reach that
#    SQL at the same time.
# =====================================================================================

import os
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# ── Connection pool ───────────────────────────────────────────────────────────────────
_pool: Optional[pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()

def _get_pool() -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:          # double-checked locking
            return _pool
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError(
                "DATABASE_URL env var is not set. "
                "Add the Railway Postgres addon and it will be injected automatically."
            )
        _pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=db_url)
        logger.info("✅ Postgres connection pool created")
        return _pool


@contextmanager
def get_connection():
    p = _get_pool()
    conn = p.getconn()
    try:
        yield conn
    finally:
        p.putconn(conn)


# ── One-time init guard ───────────────────────────────────────────────────────────────
_DB_INITIALIZED = False
_INIT_LOCK = threading.Lock()


def init_db():
    global _DB_INITIALIZED

    if _DB_INITIALIZED:
        return

    with _INIT_LOCK:
        if _DB_INITIALIZED:
            return

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS alerts (
                        id            SERIAL PRIMARY KEY,
                        symbol        TEXT    NOT NULL,
                        breakout_type TEXT    NOT NULL,
                        alert_time    TEXT    NOT NULL,
                        alert_date    TEXT    NOT NULL DEFAULT (CURRENT_DATE::TEXT),
                        scanner       TEXT,
                        category      TEXT,
                        entry_price   REAL,
                        stop_loss     REAL,
                        signals       TEXT,
                        score         INTEGER,
                        rsi           REAL,
                        volume_ratio  REAL,
                        UNIQUE (symbol, breakout_type, alert_date)
                    )
                """)
                # ── MIGRATIONS: safe to run every deploy ─────────────────────────────
                for col_sql in [
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS stop_loss    REAL",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS target_price REAL",
                    # Performance tracker write-back columns
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS status       TEXT    DEFAULT 'OPEN'",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS exit_price   REAL",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS pnl_pct      REAL",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS closed_at    TEXT",
                ]:
                    cur.execute(col_sql)

                # ── Scanner health table — source of truth for dashboard ───────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scanner_health (
                        scanner_name  TEXT PRIMARY KEY,
                        status        TEXT    NOT NULL DEFAULT 'IDLE',
                        last_success  TEXT,
                        today_alerts  INTEGER NOT NULL DEFAULT 0,
                        error_msg     TEXT,
                        updated_at    TEXT    NOT NULL
                    )
                """)

                # Clean up legacy mismatch names ending with 'Scanner' or 'Tracker'
                cur.execute("""
                    DELETE FROM scanner_health
                    WHERE scanner_name LIKE '%%Scanner'
                       OR scanner_name LIKE '%%Tracker'
                       OR scanner_name NOT IN ('INTRADAY', '1H', 'EOD', 'REVERSAL')
                """)

                # ── System state table for dashboard metrics / state caching ───────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS system_state (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)

                conn.commit()

        _DB_INITIALIZED = True
        logger.info("✅ Database ready (Postgres) — all columns ensured")
        logger.info("ℹ️  Data Retention Active: preserving all alerts for historical analysis.")


# ── Public API ────────────────────────────────────────────────────────────────────────

def save_alert_if_new(
    symbol: str,
    breakout_type: str,
    alert_time: str,
    scanner: str = None,
    category: str = None,
    entry_price: float = None,
    stop_loss: float = None,
    target_price: float = None,
    signals: str = None,
    score: int = None,
    rsi: float = None,
    volume_ratio: float = None,
) -> bool:
    """
    Insert a new alert.  Returns True if inserted, False if it already existed
    (duplicate on symbol + breakout_type + alert_date).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO alerts
                        (symbol, breakout_type, alert_time, scanner, category,
                         entry_price, stop_loss, target_price, signals, score,
                         rsi, volume_ratio, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
                    ON CONFLICT (symbol, breakout_type, alert_date) DO NOTHING
                """, (symbol, breakout_type, alert_time, scanner, category,
                      entry_price, stop_loss, target_price, signals, score,
                      rsi, volume_ratio))
                conn.commit()
                return cur.rowcount > 0
            except Exception:
                conn.rollback()
                logger.exception(f"❌ save_alert_if_new failed for {symbol}")
                return False


def update_alert_outcome(
    alert_id: int,
    status: str,          # "WIN" | "LOSS"
    exit_price: float,
    pnl_pct: float,
) -> None:
    """
    Lock in the final outcome of a trade once SL or Target is hit.
    Called by performance_tracker — writes back so future runs skip bar downloads
    for already-closed positions.
    """
    closed_at = datetime.now(IST).isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    UPDATE alerts
                    SET status     = %s,
                        exit_price = %s,
                        pnl_pct    = %s,
                        closed_at  = %s
                    WHERE id = %s
                      AND status = 'OPEN'   -- never overwrite an already-closed row
                """, (status, exit_price, pnl_pct, closed_at, alert_id))
                conn.commit()
                if cur.rowcount:
                    logger.info(f"🔒 Alert {alert_id} locked as {status} | exit={exit_price} pnl={pnl_pct}%")
            except Exception:
                conn.rollback()
                logger.exception(f"❌ update_alert_outcome failed for alert_id={alert_id}")


def get_all_alerts() -> list[dict]:
    """Return every alert, newest first — including outcome columns.

    Calls init_db() first to ensure all migration columns exist regardless
    of whether a scanner has started yet (performance tracker runs independently).
    """
    init_db()   # no-op if already initialised; ensures columns exist before SELECT
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    id, symbol, breakout_type, alert_time, alert_date,
                    scanner, category, entry_price, stop_loss, target_price,
                    signals, score, rsi, volume_ratio,
                    status, exit_price, pnl_pct, closed_at
                FROM alerts
                ORDER BY alert_time DESC
            """)
            return [dict(row) for row in cur.fetchall()]


# ── Scanner Health API ────────────────────────────────────────────────────────────────

def upsert_scanner_health(
    scanner_name: str,
    status: str,                  # "OK" | "DOWN" | "IDLE"
    last_success: str = None,     # ISO timestamp of last successful scan
    today_alerts: int = None,     # number of alerts fired today (None = keep existing)
    error_msg: str = None,        # error message when status=DOWN, else None
) -> None:
    """
    Insert or update a scanner's health record in the scanner_health table.
    Called by:
      • performance_tracker  — status=OK, last_success, today_alerts (every 5 min)
      • watchdog / _run      — status=DOWN, error_msg
      • recovery (_clear_down) — status=OK, clears error_msg
    """
    init_db()
    now_str = datetime.now(IST).isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                if today_alerts is not None:
                    cur.execute("""
                        INSERT INTO scanner_health
                            (scanner_name, status, last_success, today_alerts, error_msg, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (scanner_name) DO UPDATE
                            SET status       = EXCLUDED.status,
                                last_success = COALESCE(EXCLUDED.last_success, scanner_health.last_success),
                                today_alerts = EXCLUDED.today_alerts,
                                error_msg    = EXCLUDED.error_msg,
                                updated_at   = EXCLUDED.updated_at
                    """, (scanner_name, status, last_success, today_alerts, error_msg, now_str))
                else:
                    # Don't overwrite today_alerts when just updating status/error
                    cur.execute("""
                        INSERT INTO scanner_health
                            (scanner_name, status, last_success, today_alerts, error_msg, updated_at)
                        VALUES (%s, %s, %s, 0, %s, %s)
                        ON CONFLICT (scanner_name) DO UPDATE
                            SET status       = EXCLUDED.status,
                                last_success = COALESCE(EXCLUDED.last_success, scanner_health.last_success),
                                error_msg    = EXCLUDED.error_msg,
                                updated_at   = EXCLUDED.updated_at
                    """, (scanner_name, status, last_success, error_msg, now_str))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"❌ upsert_scanner_health failed for {scanner_name}")


def get_all_scanner_health() -> list[dict]:
    """Return all scanner health rows from the scanner_health table."""
    init_db()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT scanner_name, status, last_success, today_alerts, error_msg, updated_at
                    FROM scanner_health
                    ORDER BY scanner_name
                """)
                return [dict(row) for row in cur.fetchall()]
            except Exception:
                logger.exception("❌ get_all_scanner_health failed")
                return []


def get_scanner_today_trades(scanner_name: str, today_str: str) -> list[dict]:
    """
    Return today's alerts for a specific scanner — used by the dashboard API
    to build hover/drill-down trade list directly from the DB.
    """
    init_db()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT
                        symbol, category, signals, entry_price, alert_time,
                        stop_loss, target_price, pnl_pct, status, score,
                        exit_price, closed_at
                    FROM alerts
                    WHERE scanner    = %s
                      AND alert_date = %s
                    ORDER BY alert_time DESC
                """, (scanner_name, today_str))
                return [dict(row) for row in cur.fetchall()]
            except Exception:
                logger.exception(f"❌ get_scanner_today_trades failed for {scanner_name}")
                return []


def cleanup_old_alerts(days: int = None) -> None:
    """
    NO-OP — deletion permanently disabled for performance tracking integrity.

    All alerts are retained indefinitely so win/loss/P&L history is never lost.
    The `days` parameter is accepted for backward compatibility with scanner
    call sites but has no effect.

    To manually purge data if storage becomes a concern, run directly in Postgres:
        DELETE FROM alerts WHERE alert_date < 'YYYY-MM-DD';
    """
    logger.debug("🗑️  cleanup_old_alerts called — deletion disabled, all data retained.")


def save_system_state(key: str, value_str: str) -> None:
    """Save/update a string value (like JSON payload) for a specific key."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO system_state (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value
                """, (key, value_str))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"❌ save_system_state failed for key={key}")


def get_system_state(key: str) -> Optional[str]:
    """Retrieve system state value for a specific key."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT value FROM system_state WHERE key = %s", (key,))
                row = cur.fetchone()
                return row[0] if row else None
            except Exception:
                logger.exception(f"❌ get_system_state failed for key={key}")
                return None

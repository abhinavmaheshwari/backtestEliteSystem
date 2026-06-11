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

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# ── Connection pool ───────────────────────────────────────────────────────────────────
_pool: pool.ThreadedConnectionPool | None = None
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


def cleanup_old_alerts(days: int = None) -> None:
    """
    Delete alerts older than the retention window.

    IMPORTANT: The `days` parameter is IGNORED — it exists only for
    backward compatibility with scanner callers that pass DEDUP_DAYS.
    DEDUP_DAYS is a deduplication window (typically 1-3 days), NOT a
    retention window. Passing it here was silently deleting performance
    history needed for win/loss tracking.

    Actual retention is controlled by ALERT_RETENTION_DAYS env var (default 90).
    To change retention, set ALERT_RETENTION_DAYS in Railway environment variables.
    """
    retention_days = int(os.getenv("ALERT_RETENTION_DAYS", 90))
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    DELETE FROM alerts
                    WHERE alert_date < (CURRENT_DATE - INTERVAL '%s days')::TEXT
                """, (retention_days,))
                deleted = cur.rowcount
                conn.commit()
                if deleted:
                    logger.info(f"🗑️  Cleaned up {deleted} alerts older than {retention_days} days")
                else:
                    logger.debug(f"🗑️  No alerts older than {retention_days} days to clean up")
            except Exception:
                conn.rollback()
                logger.exception("❌ cleanup_old_alerts failed")

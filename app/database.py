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

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

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
# This is the core fix.  Multiple scanner threads all call init_db() at startup.
# Without this guard they race to CREATE TABLE and Postgres throws a duplicate-key
# error on its internal type registry.  With the guard:
#   - Thread A acquires the lock, runs CREATE TABLE, sets flag, releases lock.
#   - Threads B, C, D ... acquire the lock, see flag=True, return immediately.
# On every subsequent scan cycle the flag is already True so init_db() is a no-op.

_DB_INITIALIZED = False
_INIT_LOCK = threading.Lock()


def init_db():
    global _DB_INITIALIZED

    # Fast path — already initialized, don't even acquire the lock.
    if _DB_INITIALIZED:
        return

    with _INIT_LOCK:
        # Re-check inside the lock (another thread may have finished while
        # we were waiting to acquire it).
        if _DB_INITIALIZED:
            return

        # ── Only ONE thread ever reaches this point ───────────────────────
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
                        signals       TEXT,
                        score         INTEGER,
                        rsi           REAL,
                        volume_ratio  REAL,
                        UNIQUE (symbol, breakout_type, alert_date)
                    )
                """)
                conn.commit()

        _DB_INITIALIZED = True
        logger.info("✅ Database ready (Postgres)")
        logger.info("ℹ️  Data Retention Active: preserving all alerts for historical analysis.")


# ── Public API ────────────────────────────────────────────────────────────────────────

def save_alert_if_new(
    symbol: str,
    breakout_type: str,
    alert_time: str,
    scanner: str = None,
    category: str = None,
    entry_price: float = None,
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
                         entry_price, signals, score, rsi, volume_ratio)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, breakout_type, alert_date) DO NOTHING
                """, (symbol, breakout_type, alert_time, scanner, category,
                      entry_price, signals, score, rsi, volume_ratio))
                conn.commit()
                return cur.rowcount > 0
            except Exception:
                conn.rollback()
                logger.exception(f"❌ save_alert_if_new failed for {symbol}")
                return False


def get_all_alerts() -> list[dict]:
    """Return every alert, newest first."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    id, symbol, breakout_type, alert_time, alert_date,
                    scanner, category, entry_price, signals, score,
                    rsi, volume_ratio
                FROM alerts
                ORDER BY alert_time DESC
            """)
            return [dict(row) for row in cur.fetchall()]


def cleanup_old_alerts(days: int = 7) -> None:
    """
    Delete alerts older than `days` days.
    Called once at scanner startup — safe to call from multiple scanners
    because each DELETE is idempotent and the DB handles concurrent deletes fine.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    DELETE FROM alerts
                    WHERE alert_date < (CURRENT_DATE - INTERVAL '%s days')::TEXT
                """, (days,))
                deleted = cur.rowcount
                conn.commit()
                if deleted:
                    logger.info(f"🗑️  Cleaned up {deleted} alerts older than {days} days")
            except Exception:
                conn.rollback()
                logger.exception("❌ cleanup_old_alerts failed")

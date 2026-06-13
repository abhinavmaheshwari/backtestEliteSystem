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
import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional


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
                    # Portfolio tracking columns
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS capital_allocated REAL DEFAULT 0.0",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS shares_bought     INTEGER DEFAULT 0",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS pnl_rs            REAL",
                    # Diagnostic parameters context JSONB
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS context      JSONB",
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


                # ── System state table for dashboard metrics / state caching ───────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS system_state (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)

                # ── AI Concall Cache table ─────────────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ai_concall_cache_v3 (
                        id            SERIAL PRIMARY KEY,
                        symbol        TEXT NOT NULL,
                        pdf_url       TEXT UNIQUE NOT NULL,
                        analysis_data JSONB NOT NULL,
                        created_at    TEXT NOT NULL DEFAULT (now()::TEXT)
                    )
                """)

                # ── Trade analytics view mapping JSONB context to columns ───────────
                cur.execute("""
                    CREATE OR REPLACE VIEW v_trade_analytics AS
                    SELECT 
                        id,
                        symbol,
                        alert_time,
                        alert_date,
                        scanner,
                        category,
                        entry_price,
                        stop_loss,
                        target_price,
                        status,
                        exit_price,
                        pnl_pct,
                        closed_at,
                        -- Technical indicators
                        (context->'technicals'->>'above_ema20')::boolean AS above_ema20,
                        (context->'technicals'->>'above_sma50')::boolean AS above_sma50,
                        (context->'technicals'->>'golden_cross')::boolean AS golden_cross,
                        (context->'technicals'->>'body_ratio')::float AS body_ratio,
                        (context->'technicals'->>'delivery_pct')::float AS delivery_pct,
                        (context->'technicals'->>'rsi')::float AS rsi,
                        (context->'technicals'->>'volume_ratio')::float AS volume_ratio,
                        -- Session prices
                        (context->'session'->>'open')::float AS session_open,
                        (context->'session'->>'day_high')::float AS session_day_high,
                        (context->'session'->>'day_low')::float AS session_day_low,
                        -- Fundamentals
                        (context->'fundamentals'->>'peg')::float AS peg,
                        (context->'fundamentals'->>'yoy_rev')::float AS yoy_rev,
                        (context->'fundamentals'->>'yoy_profit')::float AS yoy_profit,
                        (context->'fundamentals'->>'roe')::float AS roe,
                        -- Execution strategies
                        context->'execution'->>'sl_method' AS sl_method,
                        context->'execution'->>'t_method' AS t_method,
                        context->'execution'->>'trail_note' AS trail_note
                    FROM alerts;
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
    context: dict = None,
    **kwargs
) -> tuple[bool, float, int]:
    """
    Insert a new alert.  Returns (inserted, capital_allocated, shares_bought).
    """
    context_str = json.dumps(context) if context is not None else None
    
    # Calculate portfolio allocation dynamically if not provided
    from app.portfolio_engine import calculate_trade_allocation
    capital_allocated = kwargs.get('capital_allocated')
    shares_bought = kwargs.get('shares_bought')
    
    if capital_allocated is None or shares_bought is None:
        if entry_price and stop_loss:
            capital_allocated, shares_bought = calculate_trade_allocation(entry_price, stop_loss, score or 80)
        else:
            capital_allocated, shares_bought = 0.0, 0
            
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO alerts
                        (symbol, breakout_type, alert_time, scanner, category,
                         entry_price, stop_loss, target_price, signals, score,
                         rsi, volume_ratio, status, context, capital_allocated, shares_bought)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s)
                    ON CONFLICT (symbol, breakout_type, alert_date) DO NOTHING
                """, (symbol, breakout_type, alert_time, scanner, category,
                      entry_price, stop_loss, target_price, signals, score,
                      rsi, volume_ratio, context_str, capital_allocated, shares_bought))
                conn.commit()
                return cur.rowcount > 0, capital_allocated, shares_bought
            except Exception:
                conn.rollback()
                logger.exception(f"❌ save_alert_if_new failed for {symbol}")
                return False, 0.0, 0


def update_alert_outcome(
    alert_id: int,
    status: str,          # "WIN" | "LOSS"
    exit_price: float,
    pnl_pct: float,
    pnl_rs: float = None,
    closed_at: Optional[str] = None,
) -> None:
    """
    Lock in the final outcome of a trade once SL or Target is hit.
    Called by performance_tracker — writes back so future runs skip bar downloads
    for already-closed positions.
    """
    if closed_at is None:
        closed_at = datetime.now(IST).isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    UPDATE alerts
                    SET status     = %s,
                        exit_price = %s,
                        pnl_pct    = %s,
                        pnl_rs     = %s,
                        closed_at  = %s
                    WHERE id = %s
                      AND status = 'OPEN'   -- never overwrite an already-closed row
                """, (status, exit_price, pnl_pct, pnl_rs, closed_at, alert_id))
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
                    status, exit_price, pnl_pct, closed_at,
                    capital_allocated, shares_bought, pnl_rs, context
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
                    WHERE scanner_name IN ('DAILY_BUILDER', 'INTRADAY', '1H', 'EOD', 'REVERSAL')
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

# ── AI CONCALL CACHE ────────────────────────────────────────────────────────
def get_cached_concall_analysis(symbol: str, pdf_url: str):
    """Retrieves cached AI analysis for a specific PDF url."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT analysis_data
                FROM ai_concall_cache_v3
                WHERE symbol = %s AND pdf_url = %s
            """, (symbol, pdf_url))
            row = cur.fetchone()
            if row:
                return row[0]
            return None

def save_concall_analysis(symbol: str, pdf_url: str, analysis_data: dict):
    """Saves AI analysis to the cache for a specific PDF url."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                import json
                cur.execute("""
                    INSERT INTO ai_concall_cache_v3 (symbol, pdf_url, analysis_data)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (pdf_url) DO UPDATE
                    SET analysis_data = EXCLUDED.analysis_data,
                        created_at = now()::TEXT
                """, (symbol, pdf_url, json.dumps(analysis_data)))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save concall cache for {symbol}: {e}")

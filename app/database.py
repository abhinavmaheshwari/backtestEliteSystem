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

import pandas as pd


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
        _pool = pool.ThreadedConnectionPool(
            minconn=2, 
            maxconn=30, 
            dsn=db_url,
            connect_timeout=5  # Add 5s timeout instead of hanging indefinitely
        )
        logger.info("✅ Postgres connection pool created (5s timeout)")
        return _pool


@contextmanager
def get_connection():
    """Get DB connection with circuit breaker pattern."""
    from psycopg2 import OperationalError
    
    p = _get_pool()
    conn = None
    try:
        conn = p.getconn()
        # Test connection is alive before returning
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        yield conn
    except OperationalError as e:
        # Circuit breaker: log and fail fast instead of hanging
        logger.error(f"🔴 DB connection failed (circuit breaker): {e}")
        if conn:
            try:
                p.putconn(conn, close=True)  # Return broken connection to pool
            except Exception:
                pass
        raise
    except Exception as e:
        logger.error(f"🔴 DB operation failed: {e}")
        if conn:
            try:
                p.putconn(conn, close=True)
            except Exception:
                pass
        raise
    finally:
        if conn:
            try:
                p.putconn(conn)
            except Exception:
                pass  # Connection already broken, ignore


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
                    # Bayesian Tracker Columns
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS model_version  TEXT DEFAULT 'v1'",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS bayesian_regime TEXT DEFAULT 'BULL'",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS bayesian_weights JSONB",
                    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS data_partition TEXT DEFAULT 'TRAIN'",
                ]:
                    cur.execute(col_sql)
                cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS seen_by_user BOOLEAN DEFAULT FALSE")
                cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS seen_by_admin BOOLEAN DEFAULT FALSE")

                # ── Score Weight Log (Bayesian Versioning) ─────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS score_weight_log (
                        id SERIAL PRIMARY KEY,
                        model_version TEXT NOT NULL,
                        regime TEXT NOT NULL,
                        weights JSONB NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (now()::TEXT)
                    )
                """)

                # ── Bayesian Model Updates (Pending Admin Approval) ──────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bayesian_model_updates (
                        id SERIAL PRIMARY KEY,
                        regime TEXT NOT NULL,
                        proposed_version TEXT NOT NULL,
                        current_version TEXT NOT NULL,
                        current_weights JSONB NOT NULL,
                        proposed_weights JSONB NOT NULL,
                        trades_analyzed INTEGER NOT NULL,
                        win_rate REAL NOT NULL,
                        reason TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'PENDING',
                        admin_comment TEXT,
                        approved_by TEXT,
                        approved_at TEXT,
                        rejected_at TEXT,
                        applied_at TEXT,
                        created_at TEXT NOT NULL DEFAULT (now()::TEXT),
                        expires_at TEXT
                    )
                """)

                # ── Scanner health table — source of truth for dashboard ───────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scanner_health (
                        scanner_name  TEXT PRIMARY KEY,
                        status        TEXT    NOT NULL DEFAULT 'IDLE',
                        last_success  TEXT,
                        today_alerts  INTEGER NOT NULL DEFAULT 0,
                        error_msg     TEXT,
                        is_acknowledged BOOLEAN DEFAULT TRUE,
                        updated_at    TEXT    NOT NULL,
                        error_severity TEXT DEFAULT NULL,
                        error_count    INTEGER DEFAULT 0,
                        first_error_at TEXT DEFAULT NULL,
                        retry_count    INTEGER DEFAULT 0,
                        scheduled_for  TEXT DEFAULT NULL
                    )
                """)
                cur.execute("ALTER TABLE scanner_health ADD COLUMN IF NOT EXISTS is_acknowledged BOOLEAN DEFAULT TRUE")
                cur.execute("ALTER TABLE scanner_health ADD COLUMN IF NOT EXISTS error_severity TEXT DEFAULT NULL")
                cur.execute("ALTER TABLE scanner_health ADD COLUMN IF NOT EXISTS error_count INTEGER DEFAULT 0")
                cur.execute("ALTER TABLE scanner_health ADD COLUMN IF NOT EXISTS first_error_at TEXT DEFAULT NULL")
                cur.execute("ALTER TABLE scanner_health ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0")
                cur.execute("ALTER TABLE scanner_health ADD COLUMN IF NOT EXISTS scheduled_for TEXT DEFAULT NULL")


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

                # ── Promoter Pledge Cache table ────────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS promoter_pledge_cache (
                        symbol        TEXT PRIMARY KEY,
                        pledge_pct    REAL NOT NULL,
                        updated_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)

                # ── Fetch error aggregation table (skipped records / fetch failures) ──
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS fetch_errors (
                        id SERIAL PRIMARY KEY,
                        source_name TEXT NOT NULL,
                        scanner_name TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        interval TEXT,
                        category TEXT NOT NULL,
                        occurrences INTEGER NOT NULL DEFAULT 1,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        last_error_msg TEXT,
                        is_acknowledged BOOLEAN DEFAULT FALSE
                    )
                """)
                # Ensure a uniqueness constraint for upsert logic
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fetch_errors_uni ON fetch_errors (source_name, scanner_name, symbol, interval, category)")
                
                # Add missing indexes for frequently queried columns
                cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_date ON alerts(alert_date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_symbol_date ON alerts(symbol, alert_date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_scanner_health_name ON scanner_health(scanner_name)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_data_fetch_health_source ON data_fetch_health(source_name)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_data_cache_metadata_key ON data_cache_metadata(key)")

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

                # ── Data cache metadata table (cache keys, last fetched, cadence) ──
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS data_cache_metadata (
                        key TEXT PRIMARY KEY,
                        last_fetched TEXT NOT NULL,
                        cadence_seconds INTEGER NOT NULL,
                        rows INTEGER,
                        etag TEXT,
                        source TEXT,
                        updated_at TEXT NOT NULL
                    )
                """)

                # ── Data fetch health table for external systems (monitoring) ─────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS data_fetch_health (
                        source_name TEXT PRIMARY KEY,
                        last_success TEXT,
                        last_failure TEXT,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        error_msg TEXT,
                        is_acknowledged BOOLEAN DEFAULT TRUE,
                        updated_at TEXT NOT NULL
                    )
                """)
                cur.execute("ALTER TABLE data_fetch_health ADD COLUMN IF NOT EXISTS is_acknowledged BOOLEAN DEFAULT TRUE")

                # ── Manual Portfolio Tracker ──────────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS manual_portfolio (
                        id SERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        entry_date DATE NOT NULL,
                        entry_price REAL NOT NULL,
                        quantity INTEGER NOT NULL,
                        added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)

                # ── Parquet Binary Cache ──────────────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS parquet_cache (
                        name TEXT,
                        date TEXT,
                        data BYTEA,
                        PRIMARY KEY (name, date)
                    )
                """)

                # ── System checkpoints table (persistent audit trail) ─────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS system_checkpoints (
                        id SERIAL PRIMARY KEY,
                        checkpoint_name TEXT UNIQUE NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (now()::TEXT),
                        updated_at TEXT NOT NULL DEFAULT (now()::TEXT),
                        content TEXT NOT NULL,
                        reason TEXT DEFAULT ''
                    )
                """)

                # ── Telegram Queue table (persistent alert queue with rate limiting) ──
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_queue (
                        id SERIAL PRIMARY KEY,
                        alert_id INTEGER REFERENCES alerts(id),
                        symbol TEXT NOT NULL,
                        message_text TEXT NOT NULL,
                        status TEXT DEFAULT 'pending',
                        retry_count INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT (now()::TEXT),
                        sent_at TEXT DEFAULT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_telegram_queue_status ON telegram_queue(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_telegram_queue_created ON telegram_queue(created_at)")

                # ── Wealth Buy Alerts table (historical tracking of buy signals) ──
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS wealth_buy_alert (
                        id SERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        alert_price REAL NOT NULL,
                        alert_date TEXT NOT NULL DEFAULT (CURRENT_DATE::TEXT),
                        alert_time TEXT NOT NULL DEFAULT (now()::TEXT),
                        breakout_type TEXT,
                        fm_score REAL,
                        status TEXT DEFAULT 'ACTIVE',
                        current_price REAL,
                        status_updated_at TEXT DEFAULT (now()::TEXT),
                        notes TEXT,
                        created_at TEXT NOT NULL DEFAULT (now()::TEXT)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_wealth_alert_symbol ON wealth_buy_alert(symbol)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_wealth_alert_date ON wealth_buy_alert(alert_date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_wealth_alert_status ON wealth_buy_alert(status)")

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
    model_version: str = "v1",
    data_partition: str = "TRAIN",
    bayesian_regime: str = "BULL",
    bayesian_weights: dict = None,
    **kwargs
) -> tuple[bool, float, int]:
    """
    Insert a new alert.  Returns (inserted, capital_allocated, shares_bought).
    
    Captures:
    - model_version: Bayesian model version (v1, v2, etc)
    - bayesian_regime: Market regime (BULL, BEAR, SIDEWAYS)
    - bayesian_weights: Actual weights used for scoring
    """
    context_str = json.dumps(context) if context is not None else None
    weights_str = json.dumps(bayesian_weights) if bayesian_weights is not None else None
    
    # Calculate portfolio allocation dynamically if not provided
    from portfolio_engine import calculate_trade_allocation
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
                         rsi, volume_ratio, status, context, capital_allocated, shares_bought,
                         model_version, bayesian_regime, bayesian_weights, data_partition)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, breakout_type, alert_date) DO NOTHING
                """, (symbol, breakout_type, alert_time, scanner, category,
                      entry_price, stop_loss, target_price, signals, score,
                      rsi, volume_ratio, context_str, capital_allocated, shares_bought,
                      model_version, bayesian_regime, weights_str, data_partition))
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
                    capital_allocated, shares_bought, pnl_rs, context,
                    model_version, data_partition
                FROM alerts
                ORDER BY alert_time DESC
            """)
            return [dict(row) for row in cur.fetchall()]


# ── Scanner Health API ────────────────────────────────────────────────────────────────

def classify_error_severity(error_msg: str) -> str:
    """
    Classify an error as CRITICAL or IGNORABLE.
    
    CRITICAL: Code failures, missing config files, compilation errors
    IGNORABLE: API failures for individual/multiple stocks - scanner rejects them and continues
    
    Returns: 'CRITICAL' | 'IGNORABLE'
    
    Key principle: If scanner can handle it by rejecting/skipping the stock and continuing,
    it's IGNORABLE (keeps scanner GREEN). If scanner crashes entirely, it's CRITICAL.
    
    Example: BAJAJ AUTO yfinance timeout
      → Stock rejected, scan continues with 49 other stocks
      → Scanner shows GREEN with alerts from successful stocks
      → Not critical because scanner completed successfully
    """
    if not error_msg:
        return None
    
    error_lower = error_msg.lower()
    
    # IGNORABLE patterns: missing stock data, API timeouts for specific/all stocks
    # Scanner handles these gracefully by rejecting the stock(s) and continuing
    ignorable_patterns = [
        'yfinance',
        'timeout',
        'connection refused',
        'no data found',
        'stock not found',
        'not available',
        'api rate limit',
        'temporarily unavailable',
        'data not available',
        'failed to get data for',
        'returned 0 data',  # Stock(s) rejected, others continue
    ]
    
    # CRITICAL patterns: code/infrastructure issues that crash the scanner
    critical_patterns = [
        'syntax error',
        'import error',
        'indentation error',
        'nameerror',
        'typeerror',
        'attributeerror',
        'keyerror',
        'file not found',
        'no such file',
        'cannot open',
        'permission denied',
        'assert',
        'index error',
        'value error',
        'runtime error',
        'null pointer',
        'undefined',
        'not defined',
        'could not import',
    ]
    
    # Check for critical patterns first
    for pattern in critical_patterns:
        if pattern in error_lower:
            return 'CRITICAL'
    
    # Check for ignorable patterns
    for pattern in ignorable_patterns:
        if pattern in error_lower:
            return 'IGNORABLE'
    
    # Default to CRITICAL for unknown errors (safety first)
    return 'CRITICAL'


def upsert_scanner_health(
    scanner_name: str,
    status: str = None,           # "OK" | "DOWN" | "IDLE" | None (keep existing)
    last_success: str = None,     # ISO timestamp of last successful scan
    today_alerts: int = None,     # number of alerts fired today (None = keep existing)
    error_msg: str = None,        # error message when status=DOWN, else None
    scheduled_for: str = None,    # When this scanner is scheduled to run (e.g., "01:00 IST")
) -> None:
    """
    Insert or update a scanner's health record in the scanner_health table.
    
    Auto-recovery logic:
      • When status='OK': Auto-clear error fields + set is_acknowledged=TRUE (recovery)
      • When status='DOWN': Classify error severity + set is_acknowledged=FALSE
      • When status='DOWN' with IGNORABLE error: Still set DOWN but error_severity=IGNORABLE
    """
    init_db()
    now_str = datetime.now(IST).isoformat()
    
    error_severity = None
    is_ack = None
    
    # Classify error severity and set acknowledgement status
    if status == 'DOWN' and error_msg:
        error_severity = classify_error_severity(error_msg)
        is_ack = False  # NEW ERROR: mark unacknowledged
    elif status == 'OK':
        # AUTO-RECOVERY: Clear errors and mark as acknowledged
        error_msg = None
        error_severity = None
        is_ack = True

    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                # Build the update/insert query
                set_clauses = []
                params = []
                
                if status is not None:
                    set_clauses.append("status = %s")
                    params.append(status)
                if last_success is not None:
                    set_clauses.append("last_success = %s")
                    params.append(last_success)
                if today_alerts is not None:
                    set_clauses.append("today_alerts = %s")
                    params.append(today_alerts)
                if error_msg is not None:
                    set_clauses.append("error_msg = %s")
                    params.append(error_msg)
                elif error_msg is None and status == 'OK':
                    # Explicitly clear error_msg on recovery
                    set_clauses.append("error_msg = NULL")
                if error_severity is not None:
                    set_clauses.append("error_severity = %s")
                    params.append(error_severity)
                elif status == 'OK':
                    # Clear error_severity on recovery
                    set_clauses.append("error_severity = NULL")
                if is_ack is not None:
                    set_clauses.append("is_acknowledged = %s")
                    params.append(is_ack)
                if scheduled_for is not None:
                    set_clauses.append("scheduled_for = %s")
                    params.append(scheduled_for)
                
                set_clauses.append("updated_at = %s")
                params.append(now_str)
                
                # Always include scanner_name for conflict/insert
                params.insert(0, scanner_name)
                if status is None:
                    status = 'IDLE'
                params.insert(1, status)
                params.insert(2, now_str)
                
                set_sql = ", ".join(set_clauses)
                cur.execute(f"""
                    INSERT INTO scanner_health
                        (scanner_name, status, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (scanner_name) DO UPDATE
                        SET {set_sql}
                """, params)
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
                    SELECT scanner_name, status, last_success, today_alerts, error_msg, is_acknowledged, updated_at, error_severity, error_count, first_error_at, retry_count, scheduled_for
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

    logger.debug("🗑️  cleanup_old_alerts called — deletion disabled, all data retained.")


def get_todays_alerts(today_str: str) -> list[dict]:
    """Return all alerts for the provided alert_date (YYYY-MM-DD)."""
    init_db()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT id, symbol, breakout_type, alert_time, scanner, category, entry_price,
                           stop_loss, target_price, signals, score, status, seen_by_user, seen_by_admin
                    FROM alerts
                    WHERE alert_date = %s
                    ORDER BY alert_time DESC
                """, (today_str,))
                return [dict(row) for row in cur.fetchall()]
            except Exception:
                logger.exception("❌ get_todays_alerts failed")
                return []


def mark_alert_seen(alert_id: int, role: str = "user") -> bool:
    """Mark an alert as seen by 'user' or 'admin'. Returns True if updated."""
    init_db()
    # Validate column name to prevent SQL injection
    allowed_cols = {'user': 'seen_by_user', 'admin': 'seen_by_admin'}
    col = allowed_cols.get(role)
    if not col:
        logger.warning(f"Invalid role '{role}' for mark_alert_seen")
        return False
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                # Use parameterized query with validated column name
                cur.execute(f"UPDATE alerts SET {col} = TRUE WHERE id = %s", (alert_id,))
                conn.commit()
                return cur.rowcount > 0
            except Exception:
                conn.rollback()
                logger.exception(f"❌ mark_alert_seen failed for id={alert_id}")
                return False


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

def get_ai_cache_count() -> int:
    """Returns the total number of cached AI analyses."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(DISTINCT symbol) FROM ai_concall_cache_v3")
                row = cur.fetchone()
                if row:
                    return int(row[0])
    except Exception:
        pass
    return 0


def get_total_cached_concalls() -> int:
    """Returns the total number of distinct stocks that have cached concall data."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT COUNT(DISTINCT symbol) FROM ai_concall_cache_v3")
                row = cur.fetchone()
                return row[0] if row else 0
            except Exception as e:
                logger.error(f"Error getting total cached concalls: {e}")
                return 0


def get_ai_concall_stats() -> dict:
    """Return stats for AI concall cache: total distinct symbols, last processed symbol and timestamp."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT COUNT(DISTINCT symbol) FROM ai_concall_cache_v3")
                total_row = cur.fetchone()
                total = total_row[0] if total_row else 0
                cur.execute("SELECT symbol, created_at FROM ai_concall_cache_v3 ORDER BY created_at DESC LIMIT 1")
                last = cur.fetchone()
                if last:
                    return {"total_cached": int(total), "last_symbol": last[0], "last_updated": last[1]}
                return {"total_cached": int(total), "last_symbol": None, "last_updated": None}
            except Exception as e:
                logger.error(f"Error getting ai concall stats: {e}")
                return {"total_cached": 0, "last_symbol": None, "last_updated": None}


def get_promoter_pledge_stats() -> dict:
    """Return stats for promoter_pledge_cache: total symbols cached, last processed symbol and timestamp."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT COUNT(*) FROM promoter_pledge_cache")
                total_row = cur.fetchone()
                total = total_row[0] if total_row else 0
                cur.execute("SELECT symbol, updated_at FROM promoter_pledge_cache ORDER BY updated_at DESC LIMIT 1")
                last = cur.fetchone()
                if last:
                    return {"total_cached": int(total), "last_symbol": last[0], "last_updated": last[1]}
                return {"total_cached": int(total), "last_symbol": None, "last_updated": None}
            except Exception as e:
                logger.error(f"Error getting pledge stats: {e}")
                return {"total_cached": 0, "last_symbol": None, "last_updated": None}

def get_recent_concall_analysis(symbol: str, max_age_days: int = 60):
    """Retrieves cached AI analysis for a symbol if it is less than max_age_days old."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT analysis_data
                FROM ai_concall_cache_v3
                WHERE symbol = %s AND created_at::TIMESTAMP WITH TIME ZONE >= NOW() - INTERVAL '1 day' * %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (symbol, max_age_days))
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


def get_cache_metadata(key: str):
    """Return metadata for a cache key from data_cache_metadata or None if missing."""
    init_db()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute("SELECT key, last_fetched, cadence_seconds, rows, etag, source, updated_at FROM data_cache_metadata WHERE key = %s", (key,))
                row = cur.fetchone()
                return dict(row) if row else None
            except Exception:
                logger.exception(f"❌ get_cache_metadata failed for key={key}")
                return None


def get_latest_weights(regime: str) -> dict:
    """Get the latest JSON weights for a given regime."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT model_version, weights 
                    FROM score_weight_log 
                    WHERE regime = %s 
                    ORDER BY id DESC LIMIT 1
                """, (regime,))
                row = cur.fetchone()
                if row:
                    return {"version": row[0], "weights": row[1]}
                return None
            except Exception:
                logger.exception(f"❌ get_latest_weights failed for regime={regime}")
                return None

def save_new_weights(model_version: str, regime: str, weights: dict):
    """Save a new version of weights for a given regime."""
    init_db()
    import json
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO score_weight_log (model_version, regime, weights)
                    VALUES (%s, %s, %s)
                """, (model_version, regime, json.dumps(weights)))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"❌ save_new_weights failed for regime={regime}")


def upsert_cache_metadata(key: str, last_fetched: str, cadence_seconds: int, rows: int = None, etag: str = None, source: str = None):
    """Insert or update cache metadata for a given key."""
    init_db()
    now = datetime.now(IST).isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO data_cache_metadata (key, last_fetched, cadence_seconds, rows, etag, source, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET last_fetched = EXCLUDED.last_fetched,
                            cadence_seconds = EXCLUDED.cadence_seconds,
                            rows = COALESCE(EXCLUDED.rows, data_cache_metadata.rows),
                            etag = COALESCE(EXCLUDED.etag, data_cache_metadata.etag),
                            source = COALESCE(EXCLUDED.source, data_cache_metadata.source),
                            updated_at = EXCLUDED.updated_at
                """, (key, last_fetched, cadence_seconds, rows, etag, source, now))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"❌ upsert_cache_metadata failed for key={key}")


def upsert_data_fetch_health(source_name: str, last_success: str = None, last_failure: str = None, consecutive_failures: int = None, error_msg: str = None):
    """Insert/update health row for an external data provider (yfinance, nse, etc.)."""
    init_db()
    now = datetime.now(IST).isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                # If consecutive_failures is None, don't overwrite the existing value.
                if consecutive_failures == 0:
                    # Success for API: Reset consecutive failures, but keep is_acknowledged as-is (requires admin dismissal)
                    cur.execute("""
                        INSERT INTO data_fetch_health (source_name, last_success, consecutive_failures, is_acknowledged, updated_at)
                        VALUES (%s, %s, 0, TRUE, %s)
                        ON CONFLICT (source_name) DO UPDATE
                            SET last_success = COALESCE(EXCLUDED.last_success, data_fetch_health.last_success),
                                consecutive_failures = 0,
                                updated_at = EXCLUDED.updated_at
                    """, (source_name, last_success, now))
                elif consecutive_failures is not None:
                    # Specific consecutive_failures provided (uncommon pathway)
                    cur.execute("""
                        INSERT INTO data_fetch_health (source_name, last_success, last_failure, consecutive_failures, error_msg, is_acknowledged, updated_at)
                        VALUES (%s, %s, %s, %s, %s, FALSE, %s)
                        ON CONFLICT (source_name) DO UPDATE
                            SET last_success = COALESCE(EXCLUDED.last_success, data_fetch_health.last_success),
                                last_failure = COALESCE(EXCLUDED.last_failure, data_fetch_health.last_failure),
                                consecutive_failures = EXCLUDED.consecutive_failures,
                                error_msg = COALESCE(EXCLUDED.error_msg, data_fetch_health.error_msg),
                                is_acknowledged = FALSE,
                                updated_at = EXCLUDED.updated_at
                    """, (source_name, last_success, last_failure, consecutive_failures, error_msg, now))
                else:
                    # Standard failure reporting
                    cur.execute("""
                        INSERT INTO data_fetch_health 
                          (source_name, last_success, last_failure, consecutive_failures, error_msg, is_acknowledged, updated_at)
                        VALUES (%s, %s, %s, 1, %s, FALSE, %s)
                        ON CONFLICT (source_name) DO UPDATE
                          SET last_failure = COALESCE(EXCLUDED.last_failure, data_fetch_health.last_failure),
                              consecutive_failures = COALESCE(data_fetch_health.consecutive_failures, 0) + 1,
                              is_acknowledged = CASE WHEN EXCLUDED.error_msg IS DISTINCT FROM data_fetch_health.error_msg THEN FALSE ELSE data_fetch_health.is_acknowledged END,
                              error_msg = COALESCE(EXCLUDED.error_msg, data_fetch_health.error_msg),
                              updated_at = EXCLUDED.updated_at
                    """, (source_name, last_success, last_failure, error_msg, now))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"❌ upsert_data_fetch_health failed for {source_name}")

def acknowledge_data_fetch_health(source_name: str):
    """Admin acknowledgment to clear persistent UI warnings.

    Also clear corresponding scanner_health rows (External:<source> and impacted scanners)
    so the UI immediately reflects the dismissal.
    """
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    UPDATE data_fetch_health 
                    SET is_acknowledged = TRUE, error_msg = NULL, consecutive_failures = 0
                    WHERE source_name = %s
                """, (source_name,))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"❌ acknowledge_data_fetch_health failed for {source_name}")
    # Also attempt to clear any scanner_health rows that were set due to this external source
    try:
        # Split base and scope if present
        base = source_name.split(':', 1)[0] if ':' in source_name else source_name
        scope = source_name.split(':', 1)[1] if ':' in source_name else None
        cleared = []
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Clear the generic External:<source_name> row (exact)
                cur.execute("UPDATE scanner_health SET is_acknowledged = TRUE, error_msg = NULL, status = 'OK' WHERE scanner_name = %s", (f'External:{source_name}',))
                if cur.rowcount:
                    cleared.append(f'External:{source_name}')
                # Clear the External:<base> row as well
                cur.execute("UPDATE scanner_health SET is_acknowledged = TRUE, error_msg = NULL, status = 'OK' WHERE scanner_name = %s", (f'External:{base}',))
                if cur.rowcount:
                    cleared.append(f'External:{base}')

                # Try to import mapping from data_fetch_status to know impacted scanners
                try:
                    from data_fetch_status import SOURCE_IMPACT_MAP, INTERVAL_TO_SCANNER
                    impacted = SOURCE_IMPACT_MAP.get(base, [])
                    targeted = []
                    if scope:
                        mapped = INTERVAL_TO_SCANNER.get(scope.lower()) if hasattr(INTERVAL_TO_SCANNER, 'get') else INTERVAL_TO_SCANNER.get(scope.lower())
                        if mapped:
                            targeted = [sc for sc in impacted if sc == mapped]
                        else:
                            targeted = [sc for sc in impacted if sc.upper() == scope.upper()]
                    else:
                        targeted = impacted
                    for sc in targeted:
                        cur.execute("UPDATE scanner_health SET is_acknowledged = TRUE, error_msg = NULL, status = 'OK' WHERE scanner_name = %s", (sc,))
                        if cur.rowcount:
                            cleared.append(sc)
                    conn.commit()
                except Exception:
                    # If we can't import the mapping, still attempt a best-effort clear of External:base
                    conn.rollback()
    except Exception:
        logger.exception(f"❌ Failed to clear scanner_health rows after acknowledging {source_name}")

def acknowledge_scanner_health(scanner_name: str):
    """Admin acknowledgment to clear persistent UI warnings for scanners."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    UPDATE scanner_health 
                    SET is_acknowledged = TRUE, error_msg = NULL, status = 'OK'
                    WHERE scanner_name = %s
                """, (scanner_name,))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"❌ acknowledge_scanner_health failed for {scanner_name}")


def upsert_fetch_error(source_name: str, scanner_name: str, symbol: str, interval: str, category: str, error_msg: str = None):
    """Insert or update a fetch_errors aggregation row.

    If the combination (source, scanner, symbol, interval, category) exists, increment occurrences
    and update last_seen/last_error_msg. Otherwise create a new row with occurrences=1.
    """
    init_db()
    now = datetime.now(IST).isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO fetch_errors (source_name, scanner_name, symbol, interval, category, occurrences, first_seen, last_seen, last_error_msg, is_acknowledged)
                    VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, FALSE)
                    ON CONFLICT (source_name, scanner_name, symbol, interval, category) DO UPDATE
                      SET occurrences = fetch_errors.occurrences + 1,
                          last_seen = EXCLUDED.last_seen,
                          last_error_msg = COALESCE(EXCLUDED.last_error_msg, fetch_errors.last_error_msg),
                          is_acknowledged = FALSE
                """, (source_name, scanner_name, symbol, interval, category, now, now, error_msg))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"❌ upsert_fetch_error failed for {source_name}/{symbol}")


def get_all_fetch_errors(limit: int = 100) -> list:
    """Return all non-hidden fetch errors (excluding acknowledged with 0 occurrences).
    
    Hide errors where is_acknowledged=TRUE AND occurrences=0.
    """
    init_db()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT id, source_name, scanner_name, symbol, interval, category, occurrences, first_seen, last_seen, last_error_msg, is_acknowledged
                    FROM fetch_errors
                    WHERE NOT (is_acknowledged = TRUE AND occurrences = 0)
                    ORDER BY occurrences DESC, last_seen DESC
                    LIMIT %s
                """, (limit,))
                return [dict(r) for r in cur.fetchall()]
            except Exception:
                logger.exception("❌ get_all_fetch_errors failed")
                return []


def get_fetch_errors_for_scanner(scanner_name: str) -> list:
    """Return all non-acknowledged fetch_errors for a specific scanner.
    
    Hide errors where is_acknowledged=TRUE AND occurrences=0.
    """
    init_db()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT id, source_name, scanner_name, symbol, interval, category, occurrences, first_seen, last_seen, last_error_msg, is_acknowledged
                    FROM fetch_errors
                    WHERE scanner_name = %s 
                      AND NOT (is_acknowledged = TRUE AND occurrences = 0)
                    ORDER BY occurrences DESC, last_seen DESC
                """, (scanner_name,))
                return [dict(r) for r in cur.fetchall()]
            except Exception:
                logger.exception(f"❌ get_fetch_errors_for_scanner failed for {scanner_name}")
                return []


def has_unacknowledged_errors(scanner_name: str) -> bool:
    """Check if a scanner has ANY unacknowledged fetch_errors."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT 1 FROM fetch_errors
                    WHERE scanner_name = %s AND is_acknowledged = FALSE
                    LIMIT 1
                """, (scanner_name,))
                return cur.fetchone() is not None
            except Exception:
                logger.exception(f"❌ has_unacknowledged_errors failed for {scanner_name}")
                return False


def acknowledge_fetch_error(error_id: int) -> bool:
    """Mark a fetch_errors row as acknowledged and reset counter to 0.
    
    When user clicks 'Ignore', this resets occurrences to 0 and sets is_acknowledged=TRUE.
    If error reoccurs, upsert_fetch_error will set occurrences=1 and is_acknowledged=FALSE.
    """
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                # Mark the fetch error as acknowledged AND reset counter to 0
                cur.execute("""
                    UPDATE fetch_errors 
                    SET is_acknowledged = TRUE, occurrences = 0
                    WHERE id = %s
                """, (error_id,))
                if cur.rowcount == 0:
                    return False
                
                # Get the scanner_name from this error
                cur.execute("SELECT scanner_name FROM fetch_errors WHERE id = %s", (error_id,))
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return True
                
                scanner_name = row[0]
                
                # Check if this scanner has ANY remaining unacknowledged errors
                cur.execute("""
                    SELECT 1 FROM fetch_errors
                    WHERE scanner_name = %s AND is_acknowledged = FALSE
                    LIMIT 1
                """, (scanner_name,))
                has_more_errors = cur.fetchone() is not None
                
                # If no more errors, clear the scanner_health record (turn green)
                if not has_more_errors:
                    cur.execute("""
                        UPDATE scanner_health
                        SET status = 'OK', is_acknowledged = TRUE, error_msg = NULL, updated_at = %s
                        WHERE scanner_name = %s
                    """, (datetime.now(IST).isoformat(), scanner_name))
                    logger.info(f"✓ Cleared scanner_health for {scanner_name} (all errors acknowledged)")
                
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                logger.exception(f"❌ acknowledge_fetch_error failed for id={error_id}")
                return False

def get_all_data_fetch_health() -> list:
    """Return all rows from data_fetch_health as list of dicts."""
    init_db()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute("SELECT source_name, last_success, last_failure, consecutive_failures, error_msg, is_acknowledged, updated_at FROM data_fetch_health ORDER BY source_name")
                return [dict(r) for r in cur.fetchall()]
            except Exception:
                logger.exception("❌ get_all_data_fetch_health failed")
                return []

# ── Manual Portfolio Tracker ──────────────────────────────────────────────────

def get_manual_portfolio():
    """Retrieve all manual portfolio entries."""
    init_db()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, symbol, entry_date::TEXT, entry_price, quantity
                FROM manual_portfolio
                ORDER BY added_at DESC
            """)
            return cur.fetchall()

def add_portfolio_entry(symbol: str, entry_date: str, entry_price: float, quantity: int):
    """Add a new stock to the manual portfolio."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO manual_portfolio (symbol, entry_date, entry_price, quantity)
                VALUES (%s, %s, %s, %s)
            """, (symbol.upper(), entry_date, entry_price, quantity))
        conn.commit()

def remove_portfolio_entry(entry_id: int):
    """Remove a stock from the manual portfolio by ID."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM manual_portfolio WHERE id = %s", (entry_id,))
        conn.commit()

# ── Parquet Binary Cache ──────────────────────────────────────────────────────

def upload_parquet_to_db(name: str, file_path: str):
    """Upload a binary parquet file to the database for today."""
    if not os.path.exists(file_path):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    init_db()
    try:
        with open(file_path, "rb") as f:
            binary_data = f.read()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO parquet_cache (name, date, data)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (name, date) DO UPDATE SET data = EXCLUDED.data
                """, (name, today, binary_data))
            conn.commit()
        logger.info(f"💾 Uploaded {name} to DB parquet_cache for {today}")
    except Exception as e:
        logger.error(f"❌ Failed to upload {name} to DB: {e}")

def download_parquet_from_db(name: str, file_path: str) -> bool:
    """Download the latest binary parquet file from the database."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data, date FROM parquet_cache WHERE name = %s ORDER BY date DESC LIMIT 1", (name,))
                row = cur.fetchone()
                if row and row[0]:
                    import os
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, "wb") as f:
                        f.write(row[0])
                    logger.info(f"⚡ Downloaded {name} from DB parquet_cache (from date: {row[1]})")
                    return True
        return False
    except Exception as e:
        logger.error(f"❌ Failed to download {name} from DB: {e}")
        return False

def save_df_to_table(table_name: str, df: pd.DataFrame):
    """Saves a Pandas DataFrame to a PostgreSQL table dynamically."""
    if df.empty:
        return
    init_db()
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Fetch destination table columns
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = %s
            """, (table_name.lower(),))
            rows = cur.fetchall()
            db_cols = {row[0].lower(): row[0] for row in rows}
            
            if not db_cols:
                logger.warning(f"⚠️ Table '{table_name}' does not exist in DB or has no columns.")
                return

            # 2. Identify date column
            date_col = None
            for candidate in ["date", "run_date", "created_at", "added_at", "updated_at"]:
                if candidate in db_cols:
                    date_col = db_cols[candidate]
                    break

            # 3. If there is old date data, delete it first
            if date_col:
                cur.execute(f"DELETE FROM {table_name} WHERE {date_col} < %s", (today_str,))
                # Also delete today's data just to be safe from duplicates on retry
                cur.execute(f"DELETE FROM {table_name} WHERE {date_col} = %s", (today_str,))
            else:
                cur.execute(f"TRUNCATE TABLE {table_name}")
                
            # 4. Map DataFrame columns to DB columns (case-insensitive)
            df_cols_mapped = {}
            for col in df.columns:
                col_lower = col.lower().replace(" ", "_").replace("%", "pct").replace("yoy", "yoy").replace("qoq", "qoq")
                if col_lower in db_cols:
                    df_cols_mapped[col] = db_cols[col_lower]
                elif col.lower() in db_cols:
                    df_cols_mapped[col] = db_cols[col.lower()]

            insert_cols = list(df_cols_mapped.values())
            df_source_cols = list(df_cols_mapped.keys())

            # If there's a date column and it's not mapped from DataFrame, add it to insert
            add_date_val = False
            if date_col and date_col not in insert_cols:
                insert_cols.append(date_col)
                add_date_val = True

            if not insert_cols:
                logger.warning(f"⚠️ No matching columns found between DataFrame and table '{table_name}'.")
                return

            # 5. Insert rows
            col_list_str = ", ".join(f'"{c}"' for c in insert_cols)
            val_placeholders = ", ".join(["%s"] * len(insert_cols))
            insert_query = f"INSERT INTO {table_name} ({col_list_str}) VALUES ({val_placeholders})"

            for _, row in df.iterrows():
                vals = [row[sc] for sc in df_source_cols]
                # Convert nan to None for DB
                vals = [None if pd.isna(v) else v for v in vals]
                if add_date_val:
                    vals.append(today_str)
                cur.execute(insert_query, tuple(vals))
                
        conn.commit()
    logger.info(f"✅ Saved {len(df)} rows to table '{table_name}' in database.")

def check_data_exists_for_today() -> bool:
    """Checks if the public table 'included' (fundamental watchlist) contains data for today's IST date."""
    init_db()
    from zoneinfo import ZoneInfo
    today_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 1. First check if 'included' table exists
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'included'
                    )
                """)
                if not cur.fetchone()[0]:
                    return False
                
                # 2. Find date column
                cur.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = 'included'
                """)
                db_cols = [row[0].lower() for row in cur.fetchall()]
                date_col = None
                for candidate in ["date", "run_date", "created_at", "added_at"]:
                    if candidate in db_cols:
                        date_col = candidate
                        break
                
                if not date_col:
                    return False
                
                # 3. Check row count for today
                cur.execute(f"SELECT COUNT(*) FROM included WHERE {date_col} = %s", (today_str,))
                count = cur.fetchone()[0]
                return count > 0
    except Exception as e:
        logger.error(f"Error checking if today's data exists in DB: {e}")
        return False

# ── Checkpoint persistence (audit trail) ──────────────────────────────────────────────

def save_checkpoint(checkpoint_name: str, content: str, reason: str = '') -> bool:
    """Save system checkpoint to persistent database."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO system_checkpoints (checkpoint_name, created_at, updated_at, content, reason)
                    VALUES (%s, NOW(), NOW(), %s, %s)
                    ON CONFLICT (checkpoint_name) 
                    DO UPDATE SET updated_at=NOW(), content=EXCLUDED.content, reason=EXCLUDED.reason
                """, (checkpoint_name, content, reason))
                conn.commit()
                logger.info(f"✅ Checkpoint saved: {checkpoint_name}")
                return True
    except Exception as e:
        logger.error(f"❌ Failed to save checkpoint '{checkpoint_name}': {e}")
        return False

def get_checkpoint(checkpoint_name: str) -> str:
    """Retrieve system checkpoint from database."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT content FROM system_checkpoints 
                    WHERE checkpoint_name = %s
                """, (checkpoint_name,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        logger.error(f"❌ Failed to retrieve checkpoint '{checkpoint_name}': {e}")
        return None

# ── Telegram Queue Management ──────────────────────────────────────────────────────────

def queue_alert_to_telegram(symbol: str, message_text: str, alert_id: int = None) -> bool:
    """Queue alert for asynchronous Telegram delivery with rate limiting."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO telegram_queue (alert_id, symbol, message_text, created_at)
                    VALUES (%s, %s, %s, NOW())
                """, (alert_id, symbol, message_text))
                conn.commit()
                logger.debug(f"✅ Queued Telegram alert for {symbol}")
                return True
    except Exception as e:
        logger.error(f"❌ Failed to queue Telegram alert: {e}")
        return False

def get_pending_telegram_alerts(limit: int = 5) -> list:
    """Get pending alerts from queue (5 per batch respects 30/sec Telegram limit)."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, alert_id, symbol, message_text, retry_count
                    FROM telegram_queue 
                    WHERE status = 'pending' AND retry_count < 3
                    ORDER BY created_at ASC
                    LIMIT %s
                """, (limit,))
                return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"❌ Failed to fetch pending Telegram alerts: {e}")
        return []

def mark_telegram_sent(queue_id: int) -> bool:
    """Mark alert as sent in Telegram queue."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE telegram_queue 
                    SET status = 'sent', sent_at = NOW()
                    WHERE id = %s
                """, (queue_id,))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"❌ Failed to mark alert sent: {e}")
        return False

def mark_telegram_failed(queue_id: int) -> bool:
    """Increment retry count for failed Telegram send."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE telegram_queue 
                    SET retry_count = retry_count + 1
                    WHERE id = %s AND retry_count < 3
                """, (queue_id,))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"❌ Failed to retry Telegram alert: {e}")
        return False

def cleanup_old_telegram_sent(days: int = 7) -> int:
    """Clean up sent Telegram messages older than N days."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM telegram_queue 
                    WHERE status = 'sent' 
                    AND created_at < NOW() - INTERVAL %s
                """, (f"{days} days",))
                deleted = cur.rowcount
                conn.commit()
                logger.info(f"🗑️  Deleted {deleted} old Telegram messages (>{days} days)")
                return deleted
    except Exception as e:
        logger.error(f"❌ Failed to cleanup Telegram queue: {e}")
        return 0

# ── Alert Save Verification (2026-06-17) ──────────────────────────────────────────────

def verify_alerts_saved_today(scanner_name: str, expected_count: int) -> bool:
    """
    CRITICAL ERROR CHECK: Verify that alerts from this scan were actually saved to DB.
    
    If a scanner runs but produces 0 alerts in database (when we expected some),
    this is a CRITICAL ERROR indicating database connectivity issues.
    
    Args:
        scanner_name: Name of scanner (e.g., 'INTRADAY', 'EOD', 'REVERSAL')
        expected_count: Number of alerts the scanner generated
    
    Returns:
        True if alerts were successfully saved, False if save failed (CRITICAL ERROR)
    
    Usage:
        total_alerts = 10  # Generated by scanner
        if total_alerts > 0:
            if not verify_alerts_saved_today("INTRADAY", total_alerts):
                # Mark scanner as DOWN - database save failed!
                upsert_scanner_health("INTRADAY", "DOWN", 
                    error_msg="CRITICAL: Alerts failed to save to database")
                return  # Exit early with critical error
    """
    if expected_count == 0:
        return True  # No alerts expected, so nothing to verify
    
    init_db()
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Count alerts from this scanner created today
                cur.execute("""
                    SELECT COUNT(*)
                    FROM alerts
                    WHERE scanner = %s
                    AND DATE(alert_time) = %s
                """, (scanner_name, today_str))
                
                saved_count = cur.fetchone()[0]
                
                if saved_count >= expected_count:
                    logger.info(f"✅ VERIFIED: {scanner_name} saved {saved_count} alerts to DB (expected {expected_count})")
                    return True
                else:
                    logger.error(f"❌ CRITICAL: {scanner_name} expected {expected_count} alerts but only {saved_count} saved to DB")
                    return False
                    
    except Exception as e:
        logger.error(f"❌ CRITICAL: Could not verify alerts for {scanner_name}: {e}")
        return False


def get_current_bayesian_model():
    """
    Get the current ACTIVE (APPROVED) Bayesian model version and weights for all regimes.
    
    CRITICAL: This ONLY returns weights from score_weight_log that have been
    explicitly approved by admin. PENDING updates in bayesian_model_updates
    are NOT included here.
    
    Returns:
        dict: {'BULL': {'version': 'v1', 'weights': {...}}, ...}
    """
    import json
    init_db()
    
    try:
        model = {}
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Get latest APPROVED version and weights for each regime
                # Only read from score_weight_log, which contains only approved weights
                for regime in ['BULL', 'BEAR', 'SIDEWAYS']:
                    cur.execute("""
                        SELECT model_version, weights
                        FROM score_weight_log
                        WHERE regime = %s
                        ORDER BY id DESC
                        LIMIT 1
                    """, (regime,))
                    
                    row = cur.fetchone()
                    if row:
                        model[regime] = {
                            'version': row[0],
                            'weights': json.loads(row[1]) if isinstance(row[1], str) else row[1]
                        }
        
        return model if model else {
            'BULL': {'version': 'v1', 'weights': {}},
            'BEAR': {'version': 'v1', 'weights': {}},
            'SIDEWAYS': {'version': 'v1', 'weights': {}}
        }
    except Exception as e:
        logger.exception(f"❌ Failed to get current Bayesian model: {e}")
        return {}


# ── Bayesian Model Admin Approval Workflow ────────────────────────────────────────────────

def submit_bayesian_update_for_approval(
    regime: str,
    proposed_version: str,
    current_version: str,
    current_weights: dict,
    proposed_weights: dict,
    trades_analyzed: int,
    win_rate: float,
    reason: str
) -> int:
    """
    Submit a Bayesian model weight change for admin approval.
    
    IMPORTANT: This ONLY saves the proposal to bayesian_model_updates.
    Weights are NOT used for calculations until admin explicitly approves.
    
    Args:
        regime: 'BULL', 'BEAR', or 'SIDEWAYS'
        proposed_version: e.g., 'v2'
        current_version: e.g., 'v1' (what's currently live)
        current_weights: dict of current active weights
        proposed_weights: dict of new proposed weights
        trades_analyzed: number of TRAIN trades analyzed
        win_rate: win rate percentage (0.0-1.0)
        reason: explanation of why weights changed
    
    Returns:
        update_id (int) if successful, or None if failed
        
    Side effect: Inserts row into bayesian_model_updates with status='PENDING'
    """
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Check if there's already a PENDING update for this regime
                cur.execute("""
                    SELECT id FROM bayesian_model_updates
                    WHERE regime = %s AND status = 'PENDING'
                    LIMIT 1
                """, (regime,))
                
                pending = cur.fetchone()
                if pending:
                    logger.error(f"❌ BLOCKED: Already have PENDING update for {regime} regime (ID: {pending[0]})")
                    logger.error(f"   Admin must approve/reject it before submitting a new proposal")
                    return None
                
                # Insert the proposal with status='PENDING'
                cur.execute("""
                    INSERT INTO bayesian_model_updates (
                        regime, proposed_version, current_version,
                        current_weights, proposed_weights,
                        trades_analyzed, win_rate, reason, status, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', NOW()::TEXT)
                    RETURNING id
                """, (
                    regime,
                    proposed_version,
                    current_version,
                    json.dumps(current_weights),
                    json.dumps(proposed_weights),
                    trades_analyzed,
                    win_rate,
                    reason
                ))
                
                update_id = cur.fetchone()[0]
                conn.commit()
                
                logger.info(f"✅ Bayesian update SUBMITTED for approval (ID: {update_id})")
                logger.info(f"   Status: PENDING (awaiting admin review)")
                logger.info(f"   Regime: {regime}")
                logger.info(f"   Current version: {current_version}")
                logger.info(f"   Proposed version: {proposed_version}")
                logger.info(f"   Win rate: {win_rate:.1%} from {trades_analyzed} trades")
                
                return update_id
                
    except Exception as e:
        logger.error(f"❌ Failed to submit Bayesian update for approval: {e}")
        return None


def get_pending_bayesian_updates() -> list:
    """Get all PENDING Bayesian updates awaiting admin approval."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, regime, proposed_version, current_version,
                           current_weights, proposed_weights,
                           trades_analyzed, win_rate, reason, created_at
                    FROM bayesian_model_updates
                    WHERE status = 'PENDING'
                    ORDER BY created_at DESC
                """)
                
                updates = []
                for row in cur.fetchall():
                    row_dict = dict(row)
                    # Parse JSON fields
                    row_dict['current_weights'] = json.loads(row_dict['current_weights'])
                    row_dict['proposed_weights'] = json.loads(row_dict['proposed_weights'])
                    updates.append(row_dict)
                
                return updates
    except Exception as e:
        logger.error(f"❌ Failed to fetch pending Bayesian updates: {e}")
        return []


def approve_bayesian_update(update_id: int, admin_name: str, comment: str = "") -> bool:
    """
    ADMIN APPROVES a Bayesian update. Weights are NOW applied to all future scanners.
    
    WORKFLOW:
    1. Update bayesian_model_updates status to APPROVED
    2. INSERT proposed_weights into score_weight_log (makes them LIVE)
    3. Future scanners will use these weights via get_current_bayesian_model()
    
    Args:
        update_id: ID of the bayesian_model_updates row
        admin_name: Admin user who approved
        comment: Optional approval comment
    
    Returns:
        True if approval successful, False otherwise
    """
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Fetch the pending update details
                cur.execute("""
                    SELECT regime, proposed_version, proposed_weights, trades_analyzed, win_rate
                    FROM bayesian_model_updates
                    WHERE id = %s AND status = 'PENDING'
                """, (update_id,))
                
                row = cur.fetchone()
                if not row:
                    logger.error(f"❌ Update {update_id} not found or already processed")
                    return False
                
                regime, proposed_version, proposed_weights_json, trades_analyzed, win_rate = row
                
                # Parse the weights
                proposed_weights = json.loads(proposed_weights_json) if isinstance(proposed_weights_json, str) else proposed_weights_json
                
                # Step 1: Insert into score_weight_log (MAKES WEIGHTS LIVE)
                cur.execute("""
                    INSERT INTO score_weight_log (model_version, regime, weights, created_at)
                    VALUES (%s, %s, %s, NOW()::TEXT)
                """, (proposed_version, regime, json.dumps(proposed_weights)))
                
                # Step 2: Update bayesian_model_updates to APPROVED
                cur.execute("""
                    UPDATE bayesian_model_updates
                    SET status = 'APPROVED', approved_by = %s, approved_at = NOW()::TEXT,
                        admin_comment = %s, applied_at = NOW()::TEXT
                    WHERE id = %s
                """, (admin_name, comment, update_id))
                
                conn.commit()
                
                logger.info(f"✅ APPROVED: Bayesian Update ID {update_id}")
                logger.info(f"   Admin: {admin_name}")
                logger.info(f"   Regime: {regime}")
                logger.info(f"   New version: {proposed_version} NOW LIVE")
                logger.info(f"   Weights inserted into score_weight_log")
                logger.info(f"   Future scanners will use this version")
                
                return True
                
    except Exception as e:
        logger.error(f"❌ Failed to approve Bayesian update {update_id}: {e}")
        return False


def reject_bayesian_update(update_id: int, admin_name: str, reason: str = "") -> bool:
    """
    ADMIN REJECTS a Bayesian update. Weights are NOT applied.
    
    Args:
        update_id: ID of the bayesian_model_updates row
        admin_name: Admin user who rejected
        reason: Why it was rejected
    
    Returns:
        True if rejection successful, False otherwise
    """
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE bayesian_model_updates
                    SET status = 'REJECTED', approved_by = %s, rejected_at = NOW()::TEXT,
                        admin_comment = %s
                    WHERE id = %s AND status = 'PENDING'
                """, (admin_name, reason, update_id))
                
                if cur.rowcount == 0:
                    logger.error(f"❌ Update {update_id} not found or already processed")
                    return False
                
                conn.commit()
                
                logger.info(f"✅ REJECTED: Bayesian Update ID {update_id}")
                logger.info(f"   Admin: {admin_name}")
                logger.info(f"   Reason: {reason or '(none provided)'}")
                logger.info(f"   Current weights remain unchanged")
                
                return True
                
    except Exception as e:
        logger.error(f"❌ Failed to reject Bayesian update {update_id}: {e}")
        return False


def get_bayesian_update_history(regime: str = None, limit: int = 20) -> list:
    """Get approval history for Bayesian updates."""
    init_db()
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if regime:
                    cur.execute("""
                        SELECT id, regime, proposed_version, current_version,
                               trades_analyzed, win_rate, status, approved_by,
                               approved_at, rejected_at, admin_comment, created_at
                        FROM bayesian_model_updates
                        WHERE regime = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (regime, limit))
                else:
                    cur.execute("""
                        SELECT id, regime, proposed_version, current_version,
                               trades_analyzed, win_rate, status, approved_by,
                               approved_at, rejected_at, admin_comment, created_at
                        FROM bayesian_model_updates
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (limit,))
                
                return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"❌ Failed to fetch Bayesian update history: {e}")
        return []


# ──────────────────────────────────────────────────────────────────────────────────────────
# WEALTH BUY ALERT TRACKING
# ──────────────────────────────────────────────────────────────────────────────────────────

def save_wealth_buy_alert(symbol: str, alert_price: float, breakout_type: str = None, 
                         fm_score: float = None, notes: str = None) -> bool:
    """Save a new buy alert to wealth_buy_alert table."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO wealth_buy_alert (symbol, alert_price, breakout_type, fm_score, status, notes)
                    VALUES (%s, %s, %s, %s, 'ACTIVE', %s)
                """, (symbol, alert_price, breakout_type, fm_score, notes))
                conn.commit()
        logger.info(f"✅ Wealth buy alert saved: {symbol} @ ₹{alert_price}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to save wealth buy alert: {e}")
        return False


def get_wealth_buy_alerts(symbol: str = None, days_back: int = 30) -> list:
    """Retrieve wealth buy alerts, optionally filtered by symbol."""
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if symbol:
                    cur.execute("""
                        SELECT * FROM wealth_buy_alert 
                        WHERE symbol = %s AND alert_date >= (CURRENT_DATE - INTERVAL '%s days')
                        ORDER BY alert_date DESC, alert_time DESC
                    """, (symbol, days_back))
                else:
                    cur.execute("""
                        SELECT * FROM wealth_buy_alert 
                        WHERE alert_date >= (CURRENT_DATE - INTERVAL '%s days')
                        ORDER BY alert_date DESC, alert_time DESC
                    """, (days_back,))
                
                return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"❌ Failed to fetch wealth buy alerts: {e}")
        return []


def update_wealth_alert_status(alert_id: int, status: str, current_price: float = None) -> bool:
    """Update the status of a wealth buy alert."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE wealth_buy_alert 
                    SET status = %s, current_price = %s, status_updated_at = now()::TEXT
                    WHERE id = %s
                """, (status, current_price, alert_id))
                conn.commit()
        logger.info(f"✅ Wealth alert {alert_id} status updated to {status}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to update wealth alert status: {e}")
        return False


def get_today_wealth_alerts() -> list:
    """Get all wealth buy alerts for today."""
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM wealth_buy_alert 
                    WHERE alert_date = CURRENT_DATE::TEXT
                    ORDER BY alert_time DESC
                """)
                return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"❌ Failed to fetch today's wealth alerts: {e}")
        return []


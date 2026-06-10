# =====================================================================================
# app/database.py  — PostgreSQL edition
# Drop-in replacement for the SQLite version.
# All public function signatures are IDENTICAL so no other file needs changing.
#
# Requires:  pip install psycopg2-binary
# Env var:   DATABASE_URL  (auto-injected by Railway Postgres addon)
# =====================================================================================

import os
import threading
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ── psycopg2 with graceful fallback error ────────────────────────────────────────────
try:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import pool as pg_pool
except ImportError:
    raise ImportError(
        "psycopg2-binary is required. Add it to requirements.txt and redeploy."
    )

# =====================================================================================
# CONNECTION POOL
# =====================================================================================

_pool: pg_pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()
_db_lock   = threading.Lock()          # kept for API compatibility


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        url = os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL env var is not set. "
                "Add the Railway Postgres addon and it will be injected automatically."
            )
        # Railway sometimes gives postgres:// — psycopg2 needs postgresql://
        url = url.replace("postgres://", "postgresql://", 1)
        _pool = pg_pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=url)
        logger.info("✅ Postgres connection pool created")
        return _pool


@contextmanager
def get_connection():
    """Yield a psycopg2 connection from the pool; return it automatically."""
    p    = _get_pool()
    conn = p.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


# =====================================================================================
# INITIALIZE DATABASE — creates table + runs migrations idempotently
# =====================================================================================

def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:

            # ── Main alerts table ────────────────────────────────────────────────────
            cur.execute(
                """
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
                """
            )

            # ── Migrations: add new columns to existing deployments safely ──────────
            _add_column_if_missing(cur, "alerts", "scanner",      "TEXT")
            _add_column_if_missing(cur, "alerts", "category",     "TEXT")
            _add_column_if_missing(cur, "alerts", "entry_price",  "REAL")
            _add_column_if_missing(cur, "alerts", "signals",      "TEXT")
            _add_column_if_missing(cur, "alerts", "score",        "INTEGER")
            _add_column_if_missing(cur, "alerts", "rsi",          "REAL")
            _add_column_if_missing(cur, "alerts", "volume_ratio", "REAL")

    logger.info("✅ Database ready (Postgres)")


def _add_column_if_missing(cur, table: str, column: str, col_type: str):
    """ALTER TABLE … ADD COLUMN only if the column doesn't already exist."""
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    if cur.fetchone() is None:
        cur.execute(f'ALTER TABLE {table} ADD COLUMN {column} {col_type}')
        logger.info(f"✅ Migration: added column {table}.{column}")


# =====================================================================================
# ATOMIC CHECK + SAVE
# =====================================================================================

def save_alert_if_new(
    symbol,
    breakout_type,
    alert_time,
    alert_date=None,
    *,                      # keyword-only extras — all optional for back-compat
    scanner:      str  = None,
    category:     str  = None,
    entry_price:  float = None,
    signals:      str  = None,
    score:        int  = None,
    rsi:          float = None,
    volume_ratio: float = None,
) -> bool:
    """
    Insert the alert if (symbol, breakout_type, alert_date) is not already present.
    Returns True if a new row was inserted, False if it was a duplicate.

    Backwards-compatible: callers that only pass the original 3 positional args
    continue to work unchanged. Scanners can optionally pass the new keyword args
    to populate the dashboard-friendly columns.
    """
    if alert_date is None:
        alert_date = alert_time[:10]

    with _db_lock:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO alerts
                            (symbol, breakout_type, alert_time, alert_date,
                             scanner, category, entry_price, signals, score,
                             rsi, volume_ratio)
                        VALUES
                            (%s, %s, %s, %s,
                             %s, %s, %s, %s, %s,
                             %s, %s)
                        ON CONFLICT (symbol, breakout_type, alert_date)
                        DO NOTHING
                        """,
                        (
                            symbol, breakout_type, alert_time, alert_date,
                            scanner, category, entry_price, signals, score,
                            rsi, volume_ratio,
                        ),
                    )
                    return cur.rowcount == 1
        except Exception:
            logger.exception(f"❌ DB error saving alert: {symbol} | {breakout_type}")
            return False


# =====================================================================================
# QUERY HELPERS  (used by performance_tracker.py)
# =====================================================================================

def get_all_alerts() -> list[dict]:
    """Return every alert row as a list of dicts, newest first."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id, symbol, breakout_type, alert_time, alert_date,
                    scanner, category, entry_price, signals, score,
                    rsi, volume_ratio
                FROM alerts
                ORDER BY alert_time DESC
                """
            )
            return [dict(r) for r in cur.fetchall()]


def get_alerts_since(days: int = 90) -> list[dict]:
    """Return alerts from the last N days."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id, symbol, breakout_type, alert_time, alert_date,
                    scanner, category, entry_price, signals, score,
                    rsi, volume_ratio
                FROM alerts
                WHERE alert_date >= (CURRENT_DATE - INTERVAL '%s days')::TEXT
                ORDER BY alert_time DESC
                """,
                (days,),
            )
            return [dict(r) for r in cur.fetchall()]


# =====================================================================================
# CLEANUP  (retention policy — currently a no-op as before)
# =====================================================================================

def cleanup_old_alerts(days: int = 7):
    """Retention explicitly disabled — preserving all history for backtesting."""
    logger.info("ℹ️ Data Retention Active: preserving all alerts for historical analysis.")


# =====================================================================================
# LEGACY WRAPPERS  (keep so nothing breaks)
# =====================================================================================

def alert_exists(symbol: str, breakout_type: str) -> bool:
    with _db_lock:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM alerts WHERE symbol=%s AND breakout_type=%s",
                        (symbol, breakout_type),
                    )
                    return cur.fetchone() is not None
        except Exception:
            logger.exception(f"❌ alert_exists error: {symbol}")
            return False


def save_alert(symbol: str, breakout_type: str, alert_time: str):
    save_alert_if_new(symbol, breakout_type, alert_time)

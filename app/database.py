# =====================================================================================
# app/database.py
# =====================================================================================

import sqlite3
import os
import threading
import logging

from config import DB_PATH

logger = logging.getLogger(__name__)

# =====================================================================================
# THREAD LOCK — one lock shared across all threads
# =====================================================================================

_db_lock = threading.Lock()

# =====================================================================================
# SQLITE CONNECTION HELPER
# =====================================================================================

def get_connection():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )

    conn.execute("PRAGMA busy_timeout = 30000")

    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as e:
        logger.warning(f"⚠️ WAL mode unavailable (read-only filesystem?): {e}")

    return conn

# =====================================================================================
# INITIALIZE DATABASE
# =====================================================================================

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    else:
        logger.warning(f"⚠️ DB_PATH has no directory component: {DB_PATH!r} — using current directory")

    with get_connection() as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS alerts (
                symbol        TEXT NOT NULL,
                breakout_type TEXT NOT NULL,
                alert_time    TEXT NOT NULL,
                alert_date    TEXT NOT NULL DEFAULT (date('now')),
                UNIQUE (
                    symbol,
                    breakout_type,
                    alert_date
                )
            )
            '''
        )

        # ── MIGRATION: add alert_date to existing databases ─────────────────────────
        try:
            conn.execute(
                "ALTER TABLE alerts ADD COLUMN alert_date TEXT NOT NULL DEFAULT (date('now'))"
            )
            conn.commit()
            logger.info("✅ Migration: added alert_date column to alerts table")

            # Rebuild the table with the correct 3-column UNIQUE index.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alerts_new (
                    symbol        TEXT NOT NULL,
                    breakout_type TEXT NOT NULL,
                    alert_time    TEXT NOT NULL,
                    alert_date    TEXT NOT NULL DEFAULT (date('now')),
                    UNIQUE (symbol, breakout_type, alert_date)
                );
                INSERT OR IGNORE INTO alerts_new (symbol, breakout_type, alert_time, alert_date)
                    SELECT symbol, breakout_type, alert_time, alert_date FROM alerts;
                DROP TABLE alerts;
                ALTER TABLE alerts_new RENAME TO alerts;
                """
            )
            conn.commit()
            logger.info("✅ Migration: rebuilt alerts table with 3-column UNIQUE index")

        except Exception:
            # Column already exists (already migrated) — normal path after first initialization.
            pass

        conn.commit()

    logger.info(f"✅ Database ready: {DB_PATH}")

# =====================================================================================
# ATOMIC CHECK + SAVE
# =====================================================================================

def save_alert_if_new(
    symbol,
    breakout_type,
    alert_time,
    alert_date=None,
):
    if alert_date is None:
        alert_date = alert_time[:10]

    with _db_lock:
        try:
            with get_connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO alerts
                        (
                            symbol,
                            breakout_type,
                            alert_time,
                            alert_date
                        )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        breakout_type,
                        alert_time,
                        alert_date,
                    )
                )
                conn.commit()
                return cursor.rowcount == 1
        except Exception:
            logger.exception(f"❌ DB error: {symbol} | {breakout_type}")
            return False

# =====================================================================================
# CLEANUP — SAFELY MODIFIED FOR HISTORICAL RETENTION
# =====================================================================================

def cleanup_old_alerts(days=7):
    """
    Deletions explicitly disabled to preserve historical data sets for backtesting.
    """
    with _db_lock:
        try:
            logger.info("ℹ️ Data Retention Active: Preserving old alerts for historical backtest engines.")
        except Exception:
            logger.exception("❌ Cleanup error")

# =====================================================================================
# LEGACY WRAPPERS
# =====================================================================================

def alert_exists(symbol, breakout_type):
    with _db_lock:
        try:
            with get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT 1
                    FROM alerts
                    WHERE symbol=?
                    AND breakout_type=?
                    """,
                    (
                        symbol,
                        breakout_type
                    )
                )
                return cursor.fetchone() is not None
        except Exception:
            logger.exception(f"❌ alert_exists error: {symbol}")
            return False

# =====================================================================================
# LEGACY SAVE WRAPPER
# =====================================================================================

def save_alert(
    symbol,
    breakout_type,
    alert_time
):
    save_alert_if_new(
        symbol,
        breakout_type,
        alert_time
    )

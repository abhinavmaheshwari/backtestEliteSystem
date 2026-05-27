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
# THREAD LOCK
# Single lock shared across all threads — prevents race conditions
# =====================================================================================

_db_lock = threading.Lock()

# =====================================================================================
# INITIALIZE DATABASE
# =====================================================================================

def init_db():

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with sqlite3.connect(DB_PATH, timeout=30) as conn:

        conn.execute("PRAGMA journal_mode=WAL")   # safe concurrent reads + writes

        conn.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                symbol        TEXT NOT NULL,
                breakout_type TEXT NOT NULL,
                alert_time    TEXT NOT NULL,
                UNIQUE (symbol, breakout_type)    -- hard constraint, DB-level dedup
            )
        ''')

        conn.commit()

    logger.info(f"✅ Database ready: {DB_PATH}")

# =====================================================================================
# CHECK + SAVE IN ONE ATOMIC OPERATION
# Uses INSERT OR IGNORE — if row exists, silently skips
# No race condition possible — check and write happen in one DB statement
# =====================================================================================

def save_alert_if_new(symbol, breakout_type, alert_time):
    """
    Atomically checks and saves in one operation.
    Returns True  — alert was new, saved successfully
    Returns False — alert already existed, skipped
    """
    with _db_lock:

        try:

            with sqlite3.connect(DB_PATH, timeout=30) as conn:

                conn.execute("PRAGMA journal_mode=WAL")

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO alerts
                        (symbol, breakout_type, alert_time)
                    VALUES
                        (?, ?, ?)
                    """,
                    (symbol, breakout_type, alert_time)
                )

                conn.commit()

                # rowcount = 1 → inserted (new alert)
                # rowcount = 0 → ignored (duplicate)
                return cursor.rowcount == 1

        except Exception:
            logger.exception(
                f"❌ DB error for {symbol} | {breakout_type}"
            )
            return False

# =====================================================================================
# CLEANUP — delete alerts older than N days
# Call once at startup to prevent DB growing forever
# =====================================================================================

def cleanup_old_alerts(days=7):

    with _db_lock:

        try:

            with sqlite3.connect(DB_PATH, timeout=30) as conn:

                conn.execute("PRAGMA journal_mode=WAL")

                conn.execute(
                    """
                    DELETE FROM alerts
                    WHERE alert_time < datetime('now', ? )
                    """,
                    (f"-{days} days",)
                )

                conn.commit()

            logger.info(f"🧹 Cleaned alerts older than {days} days")

        except Exception:
            logger.exception("❌ Cleanup error")

# =====================================================================================
# KEEP OLD FUNCTIONS AS WRAPPERS — so scanners don't break
# =====================================================================================

def alert_exists(symbol, breakout_type):
    """Legacy wrapper — still works but prefer save_alert_if_new()"""
    with _db_lock:
        try:
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                cursor = conn.execute(
                    "SELECT 1 FROM alerts WHERE symbol=? AND breakout_type=?",
                    (symbol, breakout_type)
                )
                return cursor.fetchone() is not None
        except Exception:
            logger.exception(f"❌ alert_exists error: {symbol}")
            return False

def save_alert(symbol, breakout_type, alert_time):
    """Legacy wrapper — still works but prefer save_alert_if_new()"""
    save_alert_if_new(symbol, breakout_type, alert_time)

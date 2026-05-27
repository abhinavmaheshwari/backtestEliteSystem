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
# INITIALIZE DATABASE
# =====================================================================================

def init_db():

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with sqlite3.connect(DB_PATH, timeout=30) as conn:

        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                symbol        TEXT NOT NULL,
                breakout_type TEXT NOT NULL,
                alert_time    TEXT NOT NULL,
                UNIQUE (symbol, breakout_type)
            )
        ''')

        conn.commit()

    logger.info(f"✅ Database ready: {DB_PATH}")

# =====================================================================================
# ATOMIC CHECK + SAVE
# INSERT OR IGNORE — if row exists, silently skips (no race condition)
# Returns True  → new alert saved
# Returns False → duplicate, skipped
# =====================================================================================

def save_alert_if_new(symbol, breakout_type, alert_time):

    with _db_lock:

        try:

            with sqlite3.connect(DB_PATH, timeout=30) as conn:

                conn.execute("PRAGMA journal_mode=WAL")

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO alerts
                        (symbol, breakout_type, alert_time)
                    VALUES (?, ?, ?)
                    """,
                    (symbol, breakout_type, alert_time)
                )

                conn.commit()

                return cursor.rowcount == 1

        except Exception:
            logger.exception(f"❌ DB error: {symbol} | {breakout_type}")
            return False

# =====================================================================================
# CLEANUP — remove alerts older than N days
# =====================================================================================

def cleanup_old_alerts(days=7):

    with _db_lock:

        try:

            with sqlite3.connect(DB_PATH, timeout=30) as conn:

                conn.execute("PRAGMA journal_mode=WAL")

                conn.execute(
                    "DELETE FROM alerts WHERE alert_time < datetime('now', ?)",
                    (f"-{days} days",)
                )

                conn.commit()

            logger.info(f"🧹 Cleaned alerts older than {days} days")

        except Exception:
            logger.exception("❌ Cleanup error")

# =====================================================================================
# LEGACY WRAPPERS — backwards compatible
# =====================================================================================

def alert_exists(symbol, breakout_type):

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

    save_alert_if_new(symbol, breakout_type, alert_time)

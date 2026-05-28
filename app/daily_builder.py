import sqlite3
import os
import threading
import logging
from contextlib import contextmanager

from config import DB_PATH

logger = logging.getLogger(__name__)

# Thread lock to prevent race conditions at the Python application level
_db_lock = threading.Lock()

@contextmanager
def get_db_cursor():
    """Context manager to handle connection, cursor, and commit automatically."""
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL") # Vital for multi-threaded access
    conn.execute("PRAGMA synchronous=NORMAL") # Speed up writes
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db_lock:
        try:
            with get_db_cursor() as cur:
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS alerts (
                        symbol TEXT NOT NULL,
                        breakout_type TEXT NOT NULL,
                        alert_time TEXT NOT NULL,
                        UNIQUE (symbol, breakout_type)
                    )
                ''')
            logger.info(f"✅ Database ready: {DB_PATH}")
        except Exception:
            logger.exception("❌ DB Init failed")

def save_alert_if_new(symbol, breakout_type, alert_time):
    # Using 'INSERT OR IGNORE' is excellent. 
    # Returning the rowcount confirms if a new row was actually created.
    with _db_lock:
        try:
            with get_db_cursor() as cur:
                cur.execute(
                    "INSERT OR IGNORE INTO alerts (symbol, breakout_type, alert_time) VALUES (?, ?, ?)",
                    (symbol, breakout_type, alert_time)
                )
                return cur.rowcount == 1
        except Exception:
            logger.exception(f"❌ DB error saving alert: {symbol}")
            return False

def cleanup_old_alerts(days=7):
    with _db_lock:
        try:
            with get_db_cursor() as cur:
                # Using SQLite's date math directly
                cur.execute(
                    "DELETE FROM alerts WHERE date(alert_time) < date('now', ?)",
                    (f"-{days} days",)
                )
            logger.info(f"🧹 Cleaned alerts older than {days} days")
        except Exception:
            logger.exception

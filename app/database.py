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
        # Existing deployments have the old 2-column UNIQUE(symbol, breakout_type).
        # We add alert_date as a new column (SQLite supports ADD COLUMN).
        # The old UNIQUE index cannot be dropped in SQLite without rebuilding the table,
        # but INSERT OR IGNORE will still use the new index once the column exists
        # because dedup_key already encodes the date (e.g. "Daily Breakout|2025-01-07|EOD")
        # — so (symbol, breakout_type) pairs are already unique per day by key design.
        # A full table rebuild is done below to correctly install the 3-column index.
        try:
            conn.execute(
                "ALTER TABLE alerts ADD COLUMN alert_date TEXT NOT NULL DEFAULT (date('now'))"
            )
            conn.commit()
            logger.info("✅ Migration: added alert_date column to alerts table")

            # Rebuild the table with the correct 3-column UNIQUE index.
            # SQLite cannot ALTER a UNIQUE constraint directly — the only safe approach
            # is: create new table → copy data → drop old → rename new.
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
            # Column already exists (already migrated) — this is the normal path
            # after the first successful migration. No action needed.
            pass

        conn.commit()

    logger.info(
        f"✅ Database ready: {DB_PATH}"
    )

# =====================================================================================
# ATOMIC CHECK + SAVE
# INSERT OR IGNORE — race-condition safe
# The UNIQUE constraint is now (symbol, breakout_type, alert_date), so:
#   - Same stock + same signal + same day  → blocked (no duplicate alert today)
#   - Same stock + same signal + next day  → allowed (fresh alert next day)
#   - Same stock + different signal + any day → allowed (different breakout type)
# =====================================================================================

def save_alert_if_new(
    symbol,
    breakout_type,
    alert_time,
    alert_date=None,
):
    """
    Saves an alert if it hasn't already been sent today for this symbol+breakout_type.

    Parameters
    ----------
    symbol        : str  — NSE ticker symbol
    breakout_type : str  — dedup key, e.g. "Daily Breakout|2025-01-07|EOD"
    alert_time    : str  — ISO datetime string, e.g. "2025-01-07 18:35:00"
    alert_date    : str  — ISO date string, e.g. "2025-01-07". If None, extracted
                           from alert_time. Passed explicitly for testability.

    Returns True if the alert was new and saved, False if it was a duplicate.
    """

    if alert_date is None:
        # Extract date portion from alert_time ("2025-01-07 18:35:00" → "2025-01-07")
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

            logger.exception(
                f"❌ DB error: "
                f"{symbol} | {breakout_type}"
            )

            return False

# =====================================================================================
# CLEANUP — removes rows older than N days to keep the DB lean
# =====================================================================================

def cleanup_old_alerts(days=7):
    """
    Deletions disabled to preserve historical data needed for backtesting.
    """
    with _db_lock:
        logger.info(f"ℹ️ Data Retention Active: Preserving old alerts for historical backtest engines.")

        except Exception:

            logger.exception(
                "❌ Cleanup error"
            )

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

            logger.exception(
                f"❌ alert_exists error: {symbol}"
            )

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

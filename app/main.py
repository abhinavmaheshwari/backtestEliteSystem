# =====================================================================================
# app/main.py  — launches all scanners in parallel threads (FIXED)
# =====================================================================================
#
# FIX #1: Wrap scanner loops in functions before import
# Previously: import intraday → runs while True at module level → blocks forever
# Now: import intraday → call intraday.start() → runs while True inside function
#
# =====================================================================================

import sys
import os
import threading
import logging
import time

from zoneinfo import ZoneInfo
from datetime import datetime, time as dt_time

# =====================================================================================
# PATH FIX
# =====================================================================================

APP_DIR = os.path.dirname(os.path.abspath(__file__))

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# =====================================================================================
# LOGGING
# =====================================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

logger.info(f"📁 APP_DIR resolved to: {APP_DIR}")

# =====================================================================================
# TIMEZONE
# =====================================================================================

IST = ZoneInfo("Asia/Kolkata")

# =====================================================================================
# WINDOW DEFINITIONS
# =====================================================================================

WINDOWS = {
    "intraday": (
        dt_time(9, 32),
        dt_time(15, 30)
    ),
    "live": (
        dt_time(10, 17),
        dt_time(15, 30)
    ),
    "eod": (
        dt_time(18, 30),
        dt_time(20, 0)
    ),
}

# =====================================================================================
# WAIT FOR WINDOW
# =====================================================================================

def wait_for_window(name: str):

    start_time, _ = WINDOWS[name]

    while True:

        now = datetime.now(IST)
        weekday = now.weekday()

        if weekday >= 5:
            logger.info(f"[{name}] 📅 Weekend detected | Sleeping 1 hour...")
            time.sleep(3600)
            continue

        if now.time() >= start_time:
            logger.info(
                f"[{name}] ✅ Window open | "
                f"{now.strftime('%H:%M:%S')} | Launching scanner"
            )
            return

        logger.info(
            f"[{name}] ⏰ Waiting for "
            f"{start_time.strftime('%H:%M')} | "
            f"Current={now.strftime('%H:%M:%S')}"
        )

        time.sleep(60)

# =====================================================================================
# WATCHLIST PRE-FLIGHT
# =====================================================================================

from config import WATCHLIST_PATH

# ── WATCHLIST PRE-FLIGHT — FIX GAP 5: non-blocking ──────────────────────────
#
# PROBLEM: the original synchronous build_watchlist() call (30–60s) blocked ALL
# three scanner threads at t.start() until it finished. Railway's health check
# or process monitor could interpret this silent pause as a hang and restart.
#
# FIX: Run the initial build in a background thread so main.py reaches the
# thread-start loop and the alive-monitor loop immediately. The build thread logs
# clearly when it finishes (or fails). Scanners that need the watchlist and find
# it missing will trigger their own inline rebuild at first scan attempt.
#
# A threading.Event lets the alive-monitor log a one-time notice if the scanners
# start before the build finishes, rather than silently racing.
#
import threading as _threading

_watchlist_ready = _threading.Event()

def _build_watchlist_background():
    """Runs build_watchlist() in a daemon thread at startup (non-blocking)."""
    if os.path.exists(WATCHLIST_PATH):
        logger.info(f"✅ Watchlist found | {WATCHLIST_PATH}")
        _watchlist_ready.set()
        return
    logger.info("📋 Watchlist missing | Running daily builder in background thread...")
    try:
        from daily_builder import build_watchlist
        build_watchlist()
        logger.info("✅ Watchlist built successfully (background)")
    except Exception:
        logger.exception("❌ Daily builder failed — scanners will rebuild at first scan cycle")
    finally:
        _watchlist_ready.set()   # release even on failure so nothing waits forever

_watchlist_thread = _threading.Thread(
    target=_build_watchlist_background,
    name="WatchlistBuilder",
    daemon=True,
)
_watchlist_thread.start()

# =====================================================================================
# SCANNER THREADS — FIX: Import then call function, don't rely on module-level loops
# =====================================================================================

def run_intraday_scanner():
    """Import intraday module, then call start() function to avoid module-level blocking."""
    wait_for_window("intraday")
    logger.info("⚡ Starting INTRADAY SCANNER (15m candles)")
    try:
        import intraday
        intraday.start()
    except Exception:
        logger.exception("💀 INTRADAY SCANNER THREAD CRASHED — thread is dead")
        raise  # re-raise so Railway/systemd can detect the failure


def run_live_scanner():
    """Import live_scanner module, then call start() function."""
    wait_for_window("live")
    logger.info("🚀 Starting LIVE SCANNER (1h candles)")
    try:
        import live_scanner
        live_scanner.start()
    except Exception:
        logger.exception("💀 LIVE SCANNER THREAD CRASHED — thread is dead")
        raise


def run_eod_scanner():
    """Import eod_scanner module, then call start() function."""
    wait_for_window("eod")
    logger.info("📊 Starting EOD SCANNER (Daily candles)")
    try:
        import eod_scanner
        eod_scanner.start()
    except Exception:
        logger.exception("💀 EOD SCANNER THREAD CRASHED — thread is dead")
        raise

# =====================================================================================
# MAIN
# =====================================================================================

if __name__ == "__main__":

    # ── STARTUP VALIDATION ────────────────────────────────────────────────────────
    _missing_env = [v for v in ("BOT_TOKEN", "CHAT_ID") if not os.getenv(v)]
    if _missing_env:
        logger.error(
            f"❌ FATAL: Missing required env vars: {_missing_env} — "
            "Telegram alerts will fail immediately. Set them in Railway Variables."
        )
    else:
        logger.info("✅ BOT_TOKEN and CHAT_ID present")

    threads = [
        threading.Thread(
            target=run_intraday_scanner,
            name="IntradayScanner",
            daemon=True
        ),
        threading.Thread(
            target=run_live_scanner,
            name="LiveScanner",
            daemon=True
        ),
        threading.Thread(
            target=run_eod_scanner,
            name="EODScanner",
            daemon=True
        ),
    ]

    # Start threads
    for t in threads:
        t.start()

    # Summary
    logger.info("=" * 70)
    logger.info("✅ ALL SCANNER THREADS STARTED")
    logger.info("⚡ intraday.py      | 15m | Opens 09:32 AM")
    logger.info("🚀 live_scanner.py | 1h  | Opens 10:17 AM")
    logger.info("📊 eod_scanner.py  | 1D  | Opens 06:30 PM")
    logger.info("=" * 70)

    # Keep main thread alive — also monitor for dead threads and log clearly.
    # Log once when the background watchlist build completes (Gap 5 FIX).
    _logged_ready = False
    while True:
        if not _logged_ready and _watchlist_ready.is_set():
            logger.info("✅ Watchlist build complete — all scanners can proceed")
            _logged_ready = True
        for t in threads:
            if not t.is_alive():
                logger.critical(
                    f"💀 THREAD DEAD: {t.name} has stopped running. "
                    "Check above for the crash traceback."
                )
        time.sleep(60)

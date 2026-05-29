# =====================================================================================
# app/main.py  — launches all scanners in parallel threads
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
#
# intraday  → 15m scanner
# live      → 1h scanner
# eod       → daily candle scanner
#
# IMPORTANT:
#
# EOD intentionally starts at 3:45 PM
# to ensure:
#
# ✅ NSE close settled
# ✅ Yahoo settled
# ✅ Volume finalized
# ✅ Indicators stabilized
#
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

        # =====================================================
        # WEEKEND
        # =====================================================

        if weekday >= 5:

            logger.info(
                f"[{name}] 📅 Weekend detected | "
                f"Sleeping 1 hour..."
            )

            time.sleep(3600)

            continue

        # =====================================================
        # WINDOW OPEN
        # =====================================================

        if now.time() >= start_time:

            logger.info(
                f"[{name}] ✅ Window open | "
                f"{now.strftime('%H:%M:%S')} | "
                f"Launching scanner"
            )

            return

        # =====================================================
        # WAITING
        # =====================================================

        logger.info(
            f"[{name}] ⏰ Waiting for "
            f"{start_time.strftime('%H:%M')} | "
            f"Current={now.strftime('%H:%M:%S')}"
        )

        time.sleep(60)

# =====================================================================================
# WATCHLIST PRE-FLIGHT
# =====================================================================================

WATCHLIST_PATH = (
    "/app/data/"
    "elite_fundamental_watchlist.parquet"
)

if not os.path.exists(WATCHLIST_PATH):

    logger.info(
        "📋 Watchlist missing | "
        "Running daily builder..."
    )

    try:

        from daily_builder import main as build_watchlist

        build_watchlist()

        logger.info(
            "✅ Watchlist built successfully"
        )

    except Exception:

        logger.exception(
            "❌ Daily builder failed"
        )

else:

    logger.info(
        f"✅ Watchlist found | "
        f"{WATCHLIST_PATH}"
    )

# =====================================================================================
# SCANNER THREADS
# =====================================================================================

def run_intraday_scanner():

    wait_for_window("intraday")

    logger.info(
        "⚡ Starting INTRADAY SCANNER "
        "(15m candles)"
    )

    import intraday


def run_live_scanner():

    wait_for_window("live")

    logger.info(
        "🚀 Starting LIVE SCANNER "
        "(1h candles)"
    )

    import live_scanner


def run_eod_scanner():

    wait_for_window("eod")

    logger.info(
        "📊 Starting EOD SCANNER "
        "(Daily candles)"
    )

    import eod_scanner

# =====================================================================================
# MAIN
# =====================================================================================

if __name__ == "__main__":

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

    # =====================================================
    # START THREADS
    # =====================================================

    for t in threads:

        t.start()

    # =====================================================
    # SUMMARY
    # =====================================================

    logger.info("=" * 70)

    logger.info(
        "✅ ALL SCANNER THREADS STARTED"
    )

    logger.info(
        "⚡ intraday.py      | "
        "15m | Opens 09:32 AM"
    )

    logger.info(
        "🚀 live_scanner.py | "
        "1h  | Opens 10:17 AM"
    )

    logger.info(
        "📊 eod_scanner.py  | "
        "1D  | Opens 06:30 PM"
    )

    logger.info("=" * 70)

    # =====================================================
    # KEEP MAIN THREAD ALIVE
    # =====================================================

    for t in threads:

        t.join()

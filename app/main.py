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
# intraday  → 15m scanner   (09:32 AM – 03:30 PM)
# live      → 1h scanner    (10:17 AM – 03:30 PM)
# eod       → daily scanner (06:00 PM – 07:15 PM)
#
# EOD TIMING RATIONALE:
#   3:30 PM — NSE market closes
#   5:00–5:30 PM — NSE publishes bhavcopy; NOT reliable before 6 PM
#   6:00 PM — bhavcopy is reliably available; eod_scanner begins
#   7:15 PM — eod_scanner window closes after up to 4 scan attempts
#
#   eod_scanner.py manages its own internal retry loop (up to 4 scans, 15 min apart).
#   main.py only needs to launch the thread at 18:00 so eod_scanner can self-manage.
#   Setting the main.py EOD window start to anything earlier (e.g. 15:45) is wrong —
#   eod_scanner will launch but then idle-wait until 18:00 anyway, wasting a thread
#   slot and producing confusing "waiting for EOD window" logs from two places.
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

    # EOD: start at 18:00 to align with bhavcopy availability.
    # eod_scanner.py runs up to 4 scans between 18:00 and 19:15.
    "eod": (
        dt_time(18, 0),
        dt_time(19, 15)
    ),
}

# =====================================================================================
# WAIT FOR WINDOW
# =====================================================================================

def wait_for_window(name: str):
    """
    Block until the scanner's window start time on the next valid weekday.
    Checks every 60 seconds. Does NOT check the window end — each scanner
    manages its own exit condition internally.
    """

    start_time, _ = WINDOWS[name]

    while True:

        now     = datetime.now(IST)
        weekday = now.weekday()

        # ── WEEKEND ──────────────────────────────────────────────────────────────────
        if weekday >= 5:
            logger.info(
                f"[{name}] 📅 Weekend — sleeping 1 hour | "
                f"Day={now.strftime('%A')} {now.strftime('%H:%M')}"
            )
            time.sleep(3600)
            continue

        # ── WINDOW OPEN ───────────────────────────────────────────────────────────────
        if now.time() >= start_time:
            logger.info(
                f"[{name}] ✅ Window open | "
                f"{now.strftime('%H:%M:%S IST')} ≥ {start_time.strftime('%H:%M')} | "
                f"Launching scanner"
            )
            return

        # ── WAITING ───────────────────────────────────────────────────────────────────
        logger.info(
            f"[{name}] ⏰ Waiting for {start_time.strftime('%H:%M')} | "
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
        logger.info("✅ Watchlist built successfully")

    except Exception:
        logger.exception("❌ Daily builder failed")

else:
    logger.info(f"✅ Watchlist found | {WATCHLIST_PATH}")

# =====================================================================================
# SCANNER THREADS
# =====================================================================================

def run_intraday_scanner():
    wait_for_window("intraday")
    logger.info("⚡ Starting INTRADAY SCANNER (15m candles)")
    import intraday


def run_live_scanner():
    wait_for_window("live")
    logger.info("🚀 Starting LIVE SCANNER (1h candles)")
    import live_scanner


def run_eod_scanner():
    wait_for_window("eod")
    logger.info(
        "📊 Starting EOD SCANNER (daily candles) | "
        "Will run up to 4 times between 18:00–19:15 IST"
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

    for t in threads:
        t.start()

    logger.info("=" * 70)
    logger.info("✅ ALL SCANNER THREADS STARTED")
    logger.info("⚡ intraday.py      | 15m | Opens 09:32 AM IST")
    logger.info("🚀 live_scanner.py  | 1h  | Opens 10:17 AM IST")
    logger.info("📊 eod_scanner.py   | 1D  | Opens 06:00 PM IST (up to 4 scans by 07:15 PM)")
    logger.info("=" * 70)

    for t in threads:
        t.join()

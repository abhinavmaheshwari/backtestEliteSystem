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
# WINDOW DEFINITIONS — single source of truth
# =====================================================================================

WINDOWS = {
    "intraday":   (dt_time(9,  32), dt_time(15, 30)),  # 15m | first candle closes 9:30
    "live":       (dt_time(10, 17), dt_time(15, 30)),  # 1h  | first candle closes 10:15
    "eod":        (dt_time(15, 16), dt_time(15, 30)),  # 1d  | daily candle settled
}

# =====================================================================================
# GATE — blocks the thread until its window opens
# =====================================================================================

def wait_for_window(name: str):
    """
    Sleeps until the scanner's open time is reached.
    Checks every 60 seconds so it wakes up promptly.
    """
    start_time, _ = WINDOWS[name]

    while True:

        now     = datetime.now(IST)
        weekday = now.weekday()

        if weekday >= 5:
            logger.info(f"[{name}] 📅 Weekend — sleeping 1h...")
            time.sleep(3600)
            continue

        if now.time() >= start_time:
            logger.info(
                f"[{name}] ✅ Window open at "
                f"{now.strftime('%H:%M:%S')} — launching scanner"
            )
            return

        logger.info(
            f"[{name}] ⏰ Waiting for {start_time.strftime('%H:%M')} | "
            f"Now: {now.strftime('%H:%M:%S')}"
        )
        time.sleep(60)

# =====================================================================================
# PRE-FLIGHT — build watchlist ONCE before any thread starts
# =====================================================================================

WATCHLIST_PATH = "/app/data/elite_fundamental_watchlist.parquet"

if not os.path.exists(WATCHLIST_PATH):
    logger.info("📋 Watchlist not found — running daily builder...")
    try:
        from daily_builder import main as build_watchlist
        build_watchlist()
        logger.info("✅ Watchlist built successfully")
    except Exception as e:
        logger.exception("❌ Daily builder failed — scanners may error on load")
else:
    logger.info(f"✅ Watchlist already exists: {WATCHLIST_PATH}")

# =====================================================================================
# SCANNER THREADS
# =====================================================================================

def run_intraday_scanner():
    wait_for_window("intraday")
    logger.info("⚡ Starting INTRADAY SCANNER (15m bars)...")
    import intraday

def run_live_scanner():
    wait_for_window("live")
    logger.info("🚀 Starting LIVE SCANNER (1h bars)...")
    import live_scanner

def run_eod_scanner():
    wait_for_window("eod")
    logger.info("📊 Starting EOD SCANNER (daily candle)...")
    import eod_scanner

# =====================================================================================
# LAUNCH
# =====================================================================================

if __name__ == "__main__":

    threads = [
        threading.Thread(target=run_intraday_scanner, name="IntradayScanner", daemon=True),
        threading.Thread(target=run_live_scanner,     name="LiveScanner",     daemon=True),
        threading.Thread(target=run_eod_scanner,      name="EODScanner",      daemon=True),
    ]

    for t in threads:
        t.start()

    logger.info("=" * 60)
    logger.info("✅ All scanner threads started — waiting for windows")
    logger.info("   ⚡ intraday.py     — 15m | opens 9:32 AM")
    logger.info("   🚀 live_scanner.py — 1h  | opens 10:17 AM")
    logger.info("   📊 eod_scanner.py  — 1d  | opens 3:16 PM")
    logger.info("=" * 60)

    for t in threads:
        t.join()

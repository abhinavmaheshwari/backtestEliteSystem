# =====================================================================================
# app/main.py  — launches all scanners in parallel threads
# =====================================================================================

import sys
import os
import threading
import logging

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
# PRE-FLIGHT — build watchlist ONCE before any thread starts
# Prevents 3 simultaneous daily_builder runs corrupting the parquet file
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
    logger.info("⚡ Starting INTRADAY SCANNER (15m bars, from 9:31 AM)...")
    import intraday


def run_live_scanner():
    logger.info("🚀 Starting LIVE SCANNER (1h bars, from 10:16 AM)...")
    import live_scanner


def run_eod_scanner():
    logger.info("📊 Starting EOD SCANNER (daily candle, fires at 3:15 PM)...")
    import eod_scanner


if __name__ == "__main__":

    threads = [
        threading.Thread(target=run_intraday_scanner, name="IntradayScanner", daemon=True),
        threading.Thread(target=run_live_scanner,     name="LiveScanner",     daemon=True),
        threading.Thread(target=run_eod_scanner,      name="EODScanner",      daemon=True),
    ]

    for t in threads:
        t.start()

    logger.info("=" * 60)
    logger.info("✅ All scanners running in parallel")
    logger.info("   ⚡ intraday.py      — 15m | starts 9:31 AM")
    logger.info("   🚀 live_scanner.py  — 1h  | starts 10:16 AM")
    logger.info("   📊 eod_scanner.py   — 1d  | fires at 3:15 PM")
    logger.info("=" * 60)

    for t in threads:
        t.join()

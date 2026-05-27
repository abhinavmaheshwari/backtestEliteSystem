# =====================================================================================
# app/main.py  — launches all scanners in parallel threads
# =====================================================================================
#
# SCANNER SCHEDULE:
#
#   intraday.py      — 15m bars  | runs 9:31 AM → 3:30 PM  | every 5 min
#   live_scanner.py  — 1h bars   | runs 10:16 AM → 3:30 PM | every 5 min
#   eod_scanner.py   — daily     | runs once at 3:15 PM     | once per day
#
# =====================================================================================

import threading
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)


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

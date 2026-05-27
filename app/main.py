# =====================================================================================
# app/main.py  — launches both scanners in parallel threads
# =====================================================================================

import threading
import logging

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def run_live_scanner():
    logger.info("🚀 Starting LIVE SCANNER (daily bars)...")
    import live_scanner

def run_early_scanner():
    logger.info("⚡ Starting EARLY MOMENTUM SCANNER (15m bars)...")
    import intraday

if __name__ == "__main__":

    t1 = threading.Thread(target=run_live_scanner, name="LiveScanner", daemon=True)
    t2 = threading.Thread(target=run_early_scanner, name="EarlyScanner", daemon=True)

    t1.start()
    t2.start()

    logger.info("✅ Both scanners running in parallel")

    t1.join()
    t2.join()

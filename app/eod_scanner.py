import pandas as pd
import yfinance as yf
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
from datetime import datetime, date, time as dt_time, timedelta
from technical_indicators import apply_indicators
from breakout_engine import detect_breakouts
from scoring_engine import calculate_score
from telegram_engine import send_telegram_message
from message_formatter import build_message
from database import init_db, save_alert_if_new, cleanup_old_alerts
from delivery_data import fetch_delivery_data
from sector_rotation import get_sector_scores
from config import WATCHLIST_PATH, SCORE_THRESHOLDS, SCAN_CONFIG, BATCH_DOWNLOAD_SIZE, DEDUP_DAYS, ADX_MIN_THRESHOLD

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
EOD_START, EOD_END = dt_time(18, 30), dt_time(20, 0)
CHUNK_SIZE = 10

def start():
    init_db()
    cleanup_old_alerts(days=DEDUP_DAYS)
    last_scan_date = None

    while True:
        ist_now = datetime.now(IST)
        today_str = ist_now.strftime("%Y-%m-%d")
        
        if EOD_START <= ist_now.time() <= EOD_END and ist_now.weekday() < 5 and last_scan_date != today_str:
            logger.info("📊 Starting EOD Scan")
            try:
                watchlist = pd.read_parquet(WATCHLIST_PATH)
                # ... (rest of scan logic)
                # Ensure you use the updated save_alert_if_new call here:
                # saved = save_alert_if_new(symbol, dedup_key, ..., float(candle_close), float(suggested_stop))
                last_scan_date = today_str
            except Exception as e:
                logger.error(f"Scan failed: {e}")
        
        time.sleep(300)

if __name__ == "__main__":
    start()

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
from config import (
    WATCHLIST_PATH, SCORE_THRESHOLDS, SCAN_CONFIG, BATCH_DOWNLOAD_SIZE, 
    DEDUP_DAYS, ADX_MIN_THRESHOLD
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
EOD_START, EOD_END = dt_time(18, 30), dt_time(20, 0)
CHUNK_SIZE = 10
DELIVERY_FETCH_RETRIES = 5
DELIVERY_RETRY_INTERVAL_S = 600

def seconds_until_eod() -> int:
    now = datetime.now(IST)
    target_today = now.replace(hour=18, minute=30, second=0, microsecond=0)
    delta = (target_today - now) if now < target_today else (target_today + timedelta(days=1) - now)
    return max(int(delta.total_seconds()), 0)

def fetch_watchlist_data(watchlist, period="2y", interval="1d"):
    symbols = watchlist["Stock"].tolist()
    all_data = {}
    for i in range(0, len(symbols), BATCH_DOWNLOAD_SIZE):
        batch = symbols[i : i + BATCH_DOWNLOAD_SIZE]
        tickers_str = " ".join(f"{sym}.NS" for sym in batch)
        try:
            raw = yf.download(tickers_str, period=period, interval=interval, progress=False, auto_adjust=True, group_by="ticker")
            if not raw.empty:
                for sym in batch:
                    key = f"{sym}.NS" if f"{sym}.NS" in raw.columns else sym
                    if key in raw.columns:
                        all_data[sym] = raw[key].reset_index().copy()
        except Exception:
            logger.exception("Batch download failed")
    return all_data

def start():
    init_db()
    cleanup_old_alerts(days=DEDUP_DAYS)
    last_scan_date = None
    
    while True:
        ist_now = datetime.now(IST)
        if EOD_START <= ist_now.time() <= EOD_END and ist_now.weekday() < 5 and last_scan_date != ist_now.strftime("%Y-%m-%d"):
            scan_start = datetime.now(IST)
            watchlist = pd.read_parquet(WATCHLIST_PATH)
            all_ticker_data = fetch_watchlist_data(watchlist)
            rotation_result = get_sector_scores()
            alerts_by_category = {}
            total_alerts = 0

            for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):
                try:
                    symbol, category = row["Stock"], row["Category"]
                    ticker = all_ticker_data.get(symbol)
                    if ticker is None or ticker.empty: continue
                    
                    ticker = apply_indicators(ticker, timeframe="1d")
                    signals = detect_breakouts(ticker, timeframe="1d")
                    latest = ticker.iloc[-1]
                    
                    # Calculations
                    candle_close, candle_open = float(latest["Close"]), float(latest["Open"])
                    candle_high, candle_low = float(latest["High"]), float(latest["Low"])
                    
                    current_atr = float(latest["ATR"]) if "ATR" in ticker.columns else ((candle_high - candle_low) * 1.5)
                    suggested_stop = candle_close - (1.5 * current_atr)
                    
                    # TREND METRICS
                    above_ema20  = bool(candle_close >= float(latest["EMA20"])) if "EMA20" in ticker.columns else None
                    above_sma50  = bool(candle_close >= float(latest["SMA50"])) if "SMA50" in ticker.columns else None
                    golden_cross = bool(float(latest["SMA50"]) >= float(latest["SMA200"])) if ("SMA50" in ticker.columns and "SMA200" in ticker.columns) else None

                    # DATABASE SAVE
                    saved = save_alert_if_new(symbol, f"{category}|EOD", ist_now.strftime("%Y-%m-%d %H:%M:%S"), float(candle_close), float(suggested_stop))
                    
                    if saved:
                        alerts_by_category.setdefault(category, []).append({
                            "symbol": symbol, "category": category, "price": round(candle_close, 2),
                            "above_ema20": above_ema20, "above_sma50": above_sma50, "golden_cross": golden_cross,
                            "atr_stop": round(suggested_stop, 2), "score": 80 # Placeholder for score
                        })
                        total_alerts += 1
                except Exception as e:
                    logger.error(f"Error {symbol}: {e}")

            # Reporting logic...
            last_scan_date = ist_now.strftime("%Y-%m-%d")
        time.sleep(300)

if __name__ == "__main__":
    start()

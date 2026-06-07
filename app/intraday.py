# =====================================================================================
# app/intraday.py (ULTIMATE EDITION)
# EARLY MOMENTUM SCANNER — 15M BARS + MULTI-TIMEFRAME ALIGNMENT + MORNING FILTER
# =====================================================================================

import pandas as pd
import yfinance as yf
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from zoneinfo import ZoneInfo
from datetime import datetime, date, time as dt_time

from technical_indicators import apply_indicators
from breakout_engine import detect_breakouts
from scoring_engine import calculate_score
from telegram_engine import send_telegram_message
from message_formatter import build_message
from database import init_db, save_alert_if_new, cleanup_old_alerts
from delivery_data import fetch_previous_day_delivery
from price_cache import fetch_watchlist_data  

from sector_rotation import get_sector_scores  

from config import (
    WATCHLIST_PATH,
    SCORE_THRESHOLDS,
    SCAN_CONFIG,
    DEDUP_DAYS,
    ADX_MIN_THRESHOLD,   
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

IST        = ZoneInfo("Asia/Kolkata")
CHUNK_SIZE = 10   

TIMEFRAME        = "15m"
MIN_SIGNALS      = SCAN_CONFIG["15m"]["MIN_SIGNALS"]
MIN_BODY_RATIO   = SCAN_CONFIG["15m"]["MIN_BODY_RATIO"]
MIN_CLOSE_POSITION = SCAN_CONFIG["15m"]["MIN_CLOSE_POSITION"]
MAX_UPPER_WICK   = SCAN_CONFIG["15m"]["MAX_UPPER_WICK"]
MIN_VOLUME_RATIO = SCAN_CONFIG["15m"]["MIN_VOLUME_RATIO"]
MIN_VOLUME_AVG   = SCAN_CONFIG["15m"]["MIN_VOLUME_AVG"]
MIN_RSI          = SCAN_CONFIG["15m"]["MIN_RSI"]
MAX_RSI          = SCAN_CONFIG["15m"]["MAX_RSI"]
MIN_SCORE        = SCORE_THRESHOLDS["15m"]

MIN_STOCK_PRICE   = 50.0
RSI_LOOKBACK_BARS = 5      
MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0
MAX_SINGLE_BAR_MOVE_PCT        = 6.0
MAX_GAP_FROM_PRIOR_HIGH_PCT = 3.0   
GAP_LOOKBACK_BARS           = 10    


def start():
    init_db()
    cleanup_old_alerts(days=DEDUP_DAYS)

    prev_delivery_map    = fetch_previous_day_delivery()
    _delivery_fetch_date = datetime.now(IST).date()
    if prev_delivery_map:
        logger.info(f"📦 Previous-day delivery loaded | {len(prev_delivery_map)} symbols")

    while True:
        ist_now      = datetime.now(IST)
        current_time = ist_now.time()
        weekday      = ist_now.weekday()
        
        market_open  = dt_time(9, 32) <= current_time <= dt_time(15, 35)
        
        if weekday >= 5 or not market_open:
            logger.info("📅 Outside market hours | Sleeping 5 minutes")
            time.sleep(300)
            continue
            
        # ── MACRO REGIME CHECK ──────────────────────────────────────────────────
        from market_filter import is_market_regime_bullish
        if not is_market_regime_bullish():
            logger.info("🛑 Bearish macro regime detected. Skipping all scans to preserve capital.")
            time.sleep(300)
            continue
        # ────────────────────────────────────────────────────────────────────────
        
        scan_start = datetime.now(IST)
        logger.info("=" * 80)
        logger.info(f"⚡ INTRADAY SCAN START | {scan_start.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 80)

        if datetime.now(IST).date() != _delivery_fetch_date:
            prev_delivery_map    = fetch_previous_day_delivery()
            _delivery_fetch_date = datetime.now(IST).date()

        sleep_time = 300  
        try:
            try:
                watchlist = pd.read_parquet(WATCHLIST_PATH)
            except Exception:
                try:
                    from daily_builder import build_watchlist
                    build_watchlist()
                    watchlist = pd.read_parquet(WATCHLIST_PATH)
                except Exception:
                    time.sleep(300)
                    continue
            
            # ── BATCH DOWNLOAD: INTRADAY + DAILY CONTEXT (MTA) ──────────────────────
            all_ticker_data = {}
            daily_context_data = {}
            
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_15m = pool.submit(fetch_watchlist_data, watchlist, "10d", "15m")
                future_1d  = pool.submit(fetch_watchlist_data, watchlist, "60d", "1d")
                all_ticker_data = future_15m.result()
                daily_context_data = future_1d.result()
                
            logger.info(f"📥 Data downloaded | 15m: {len(all_ticker_data)} | Daily: {len(daily_context_data)}")
            
            try:
                rotation_result = get_sector_scores()
            except Exception:
                from sector_rotation import SectorRotationResult
                rotation_result = SectorRotationResult({}, set(), set(), "", date.today(), 0.0)
            
            alerts_by_category = {}
            rejection_counts   = {k: 0 for k in [
                "no_data", "missing_col", "forming_candle_stripped", "insufficient_bars", 
                "indicator_fail", "weak_signals", "weak_body", "bearish_candle", 
                "weak_close_pos", "upper_wick", "low_volume", "low_avg_volume", 
                "penny_stock", "rsi_range", "rsi_not_rising", "weak_adx", "below_ema20", 
                "below_sma50", "no_golden_cross", "macd_bearish", "far_from_52w_high", 
                "gap_bar", "extended_breakout", "below_daily_ema20", "low_score", "duplicate", "stale_data"
            ]}
            total_alerts = 0

            for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):
                symbol = "UNKNOWN"
                try:
                    symbol   = row["Stock"]
                    category = row["Category"]
                    sector   = row.get("Sector", None)

                    if symbol not in all_ticker_data:
                        rejection_counts["no_data"] += 1
                        continue

                    ticker = all_ticker_data[symbol].copy()

                    if ticker.empty:
                        rejection_counts["no_data"] += 1
                        continue

                    if isinstance(ticker.columns, pd.MultiIndex):
                        ticker.columns = ticker.columns.get_level_values(0)

                    ticker = ticker.loc[:, ~ticker.columns.duplicated()]

                    required_cols = ["Open", "High", "Low", "Close", "Volume"]
                    missing_col   = False

                    for col_name in required_cols:
                        if col_name not in ticker.columns:
                            missing_col = True
                            break
                        if isinstance(ticker[col_name], pd.DataFrame):
                            ticker[col_name] = ticker[col_name].iloc[:, 0]
                        ticker[col_name] = pd.Series(ticker[col_name]).astype(float)

                    if missing_col:
                        rejection_counts["missing_col"] += 1
                        continue

                    ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

                    if ticker.empty:
                        rejection_counts["no_data"] += 1
                        continue

                    datetime_col = next((c for c in ["Datetime", "Date", "index"] if c in ticker.columns), None)
                    if datetime_col is not None:
                        try:
                            raw_ts = pd.Timestamp(ticker.iloc[-1][datetime_col])
                            if raw_ts.tzinfo is not None:
                                raw_ts = raw_ts.tz_convert("Asia/Kolkata")
                            candle_start = raw_ts.replace(tzinfo=None)
                            candle_end   = candle_start + pd.Timedelta(minutes=15)
                            now_naive    = datetime.now(IST).replace(tzinfo=None)
                            if now_naive < candle_end:
                                ticker = ticker.iloc[:-1].copy()
                                rejection_counts["forming_candle_stripped"] += 1
                        except Exception:
                            pass

                    if len(ticker) < 105:
                        rejection_counts["insufficient_bars"] += 1
                        continue

                    ticker = apply_indicators(ticker, timeframe=TIMEFRAME)

                    if ticker is

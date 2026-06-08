# =====================================================================================
# app/reversal_scanner.py
# DEEP DISCOUNT & MEAN REVERSION SCANNER
# Finds fundamentally elite stocks bottoming out, regardless of macro trend.
# =====================================================================================

import pandas as pd
import yfinance as yf
import time
import logging
from concurrent.futures import ThreadPoolExecutor

from zoneinfo import ZoneInfo
from datetime import datetime, date, time as dt_time, timedelta

from technical_indicators import apply_indicators
from telegram_engine import send_telegram_message
from message_formatter import build_message
from database import init_db, save_alert_if_new, cleanup_old_alerts
from config import WATCHLIST_PATH, BATCH_DOWNLOAD_SIZE, DEDUP_DAYS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
# Reversals are best scanned at EOD to confirm the daily candle hasn't faded.
REVERSAL_SCAN_START = dt_time(18, 45)  
CHUNK_SIZE = 10

# ── REVERSAL PARAMETERS ──────────────────────────────────────────────────────────────
MIN_DROP_FROM_52W_HIGH = 18.0  # Must be at least 18% down from 52-week high
MAX_DROP_FROM_52W_HIGH = 60.0  # Avoid stocks down more than 60% (structural damage)
RSI_OVERSOLD_THRESHOLD = 35    # RSI must have been below this recently
RSI_CURL_MIN           = 40    # Current RSI must have recovered above this
MIN_VOLUME_RATIO       = 1.5   # Needs 1.5x average volume showing accumulation
# ─────────────────────────────────────────────────────────────────────────────────────

def seconds_until_scan() -> int:
    now = datetime.now(IST)
    target = now.replace(hour=REVERSAL_SCAN_START.hour, minute=REVERSAL_SCAN_START.minute, second=0, microsecond=0)
    if now > target:
        target += timedelta(days=1)
    return int((target - now).total_seconds())

def fetch_watchlist_data(watchlist: pd.DataFrame) -> dict:
    symbols = watchlist["Stock"].tolist()
    all_data = {}
    
    for i in range(0, len(symbols), BATCH_DOWNLOAD_SIZE):
        batch = symbols[i : i + BATCH_DOWNLOAD_SIZE]
        tickers_str = " ".join(f"{sym}.NS" for sym in batch)
        logger.info(f"📥 Batch downloading {len(batch)} symbols for Reversal Scan...")
        
        try:
            raw = yf.download(tickers_str, period="1y", interval="1d", progress=False, group_by="ticker")
            if raw is None or raw.empty: continue
            
            if not isinstance(raw.columns, pd.MultiIndex):
                if len(batch) == 1:
                    df = raw.reset_index().copy()
                    if not df.empty: all_data[batch[0]] = df
            else:
                for sym in batch:
                    ns_sym = f"{sym}.NS"
                    if ns_sym in raw.columns.get_level_values(0):
                        df = raw[ns_sym].reset_index().copy()
                        if not df.empty: all_data[sym] = df
        except Exception as e:
            logger.error(f"❌ Batch download failed: {e}")
            
    return all_data

def start():
    init_db()
    cleanup_old_alerts(days=DEDUP_DAYS)
    logger.info("🔄 REVERSAL SCANNER READY | Waiting for EOD window...")
    last_scan_date = None

    while True:
        ist_now = datetime.now(IST)
        current_time = ist_now.time()
        today_str = ist_now.strftime("%Y-%m-%d")

        # Run only on weekdays, after 6:45 PM
        is_weekday = ist_now.weekday() < 5
        in_window = current_time >= REVERSAL_SCAN_START
        already_ran = (last_scan_date == today_str)

        if not is_weekday or already_ran or not in_window:
            time.sleep(min(seconds_until_scan(), 3600))
            continue

        # NOTICE: No 'is_market_regime_bullish()' check here. 
        # Reversals trigger irrespective of macro trends.

        logger.info("=" * 70)
        logger.info(f"🔄 MEAN REVERSION SCAN | {ist_now.strftime('%Y-%m-%d %H:%M:%S IST')}")
        logger.info("=" * 70)
        
        try:
            watchlist = pd.read_parquet(WATCHLIST_PATH)
            all_ticker_data = fetch_watchlist_data(watchlist)
            alerts_by_category = {}
            total_alerts = 0

            for _, row in watchlist.iterrows():
                symbol = row["Stock"]
                category = row["Category"]

                if symbol not in all_ticker_data: continue
                
                ticker = all_ticker_data[symbol].copy()
                ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
                if len(ticker) < 100: continue

                # Apply standard indicators
                ticker = apply_indicators(ticker, timeframe="1d")
                if ticker is None or ticker.empty: continue

                latest = ticker.iloc[-1]
                
                # Check required columns
                required = ["Close", "High", "Low", "Open", "Volume", "RSI", "EMA20", "MACD", "MACD_SIGNAL", "HIGH_52W"]
                if not all(col in ticker.columns for col in required): continue
                if pd.isna(latest["RSI"]) or pd.isna(latest["MACD"]): continue

                close_price = float(latest["Close"])
                high_52w = float(latest["HIGH_52W"])
                
                # ── LOGIC 1: THE DEEP DISCOUNT ─────────────────────────────────────
                if high_52w <= 0: continue
                drop_pct = ((high_52w - close_price) / high_52w) * 100
                
                if drop_pct < MIN_DROP_FROM_52W_HIGH or drop_pct > MAX_DROP_FROM_52W_HIGH:
                    continue

                # ── LOGIC 2: RSI CURL (MOMENTUM SHIFT) ─────────────────────────────
                current_rsi = float(latest["RSI"])
                # Look back at the last 10 days to see if RSI dipped into oversold
                past_10_rsi = ticker["RSI"].iloc[-11:-1].min()
                
                if current_rsi < RSI_CURL_MIN or past_10_rsi > RSI_OVERSOLD_THRESHOLD:
                    continue # RSI hasn't dropped low enough, or hasn't curled up enough
                    
                # ── LOGIC 3: BASE BREAK (CLOSING ABOVE 20 EMA) ─────────────────────
                ema20 = float(latest["EMA20"])
                if close_price < ema20:
                    continue # Still trapped under short term resistance

                # ── LOGIC 4: VOLUME ACCUMULATION ───────────────────────────────────
                vol_now = float(latest["Volume"])
                vol_avg = float(ticker["Volume"].iloc[-21:-1].mean())
                if vol_avg <= 0: continue
                vol_ratio = vol_now / vol_avg
                
                if vol_ratio < MIN_VOLUME_RATIO:
                    continue

                # ── LOGIC 5: MACD BULLISH CROSSOVER ────────────────────────────────
                macd = float(latest["MACD"])
                macd_sig = float(latest["MACD_SIGNAL"])
                # Must be a recent cross (MACD > Signal) and ideally below the zero line
                if macd < macd_sig or macd > 2.0: 
                    continue

                # ── PASSED ALL REVERSAL LOGIC ──────────────────────────────────────
                
                # Build custom signals list for the message formatter
                reversal_signals = [
                    f"📉 -{drop_pct:.1f}% from 52W High",
                    "📈 RSI Oversold Curl",
                    "🎯 Closed above 20 EMA",
                    "📊 MACD Bullish Cross"
                ]

                # Deduplication
                dedup_key = f"{category}|REVERSAL|{today_str}"
                if not save_alert_if_new(symbol, dedup_key, datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")):
                    continue

                candle_range = float(latest["High"]) - float(latest["Low"])
                atr_val = float(latest["ATR"]) if "ATR" in ticker.columns else (candle_range * 1.5)
                suggested_stop = close_price - (1.5 * atr_val)

                alerts_by_category.setdefault(category, []).append({
                    "symbol":           symbol,
                    "category":         category,
                    "breakout_signals": reversal_signals, # Spoofing the breakout engine
                    "price":            round(close_price, 2),
                    "open":             round(float(latest["Open"]), 2),
                    "day_high":         round(float(latest["High"]), 2),
                    "day_low":          round(float(latest["Low"]), 2),
                    "rsi":              round(current_rsi, 1),
                    "volume_ratio":     round(vol_ratio, 2),
                    "body_ratio":       round(abs(close_price - float(latest["Open"])) / candle_range * 100) if candle_range > 0 else 0,
                    "score":            85, # Hardcode a high score for reversals to trigger the 🔥 emoji
                    "above_ema20":      True,
                    "atr_stop":         round(suggested_stop, 2)
                })
                total_alerts += 1

            # ── SEND ALERTS ──────────────────────────────────────────────────────────
            if total_alerts > 0:
                for cat in sorted(alerts_by_category.keys()):
                    cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["symbol"])
                    chunks = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

                    for chunk_num, chunk in enumerate(chunks, start=1):
                        # Spoof the scanner tag as "REVERSAL" so you know what it is in Telegram
                        msg = build_message("REVERSAL", cat, chunk, chunk_num, len(chunks), ist_now.strftime("%Y-%m-%d %H:%M:%S"))
                        send_telegram_message(msg, scan_type="REVERSAL")
            
            logger.info(f"✅ REVERSAL SCAN DONE | Found {total_alerts} bottoming stocks.")
            last_scan_date = today_str

        except Exception as e:
            logger.error(f"❌ Reversal scan error: {e}")
            time.sleep(300)

if __name__ == "__main__":
    start()

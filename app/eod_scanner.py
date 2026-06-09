# =====================================================================================
# app/reversal_scanner.py (SCHEDULER READY)
# DEEP DISCOUNT & MEAN REVERSION SCANNER (With Valuation Metrics)
# =====================================================================================

import pandas as pd
import logging
import time as _time
from zoneinfo import ZoneInfo
from datetime import datetime

from technical_indicators import apply_indicators
from telegram_engine import send_telegram_message
from message_formatter import build_message
from database import init_db, save_alert_if_new, cleanup_old_alerts
from price_cache import fetch_watchlist_data
from config import WATCHLIST_PATH, DEDUP_DAYS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
CHUNK_SIZE = 10

# ── REVERSAL PARAMETERS ──────────────────────────────────────────────────────────────
MIN_DROP_FROM_52W_HIGH = 18.0
MAX_DROP_FROM_52W_HIGH = 60.0
RSI_OVERSOLD_THRESHOLD = 35
RSI_CURL_MIN           = 40
MIN_VOLUME_RATIO       = 1.5
# ─────────────────────────────────────────────────────────────────────────────────────

# Re-check every hour; dedup key prevents duplicate alerts on the same day
_SCAN_INTERVAL_SECONDS = 3600


def _run_scan():
    """Execute a single reversal scan pass. Called inside the scheduling loop."""
    cleanup_old_alerts(days=DEDUP_DAYS)

    ist_now = datetime.now(IST)
    logger.info("=" * 80)
    logger.info(f"🔄 MEAN REVERSION SCAN | {ist_now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 80)

    watchlist = pd.read_parquet(WATCHLIST_PATH)
    # Pulling 1y data to ensure we catch the 52W High correctly
    all_ticker_data = fetch_watchlist_data(watchlist, period="1y", interval="1d")

    alerts_by_category = {}
    total_alerts = 0

    for _, row in watchlist.iterrows():
        symbol   = row["Stock"]
        category = row["Category"]

        if symbol not in all_ticker_data or all_ticker_data[symbol].empty:
            continue

        ticker = all_ticker_data[symbol].copy()
        if isinstance(ticker.columns, pd.MultiIndex):
            ticker.columns = ticker.columns.get_level_values(0)
        ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

        if len(ticker) < 100:
            continue

        ticker = apply_indicators(ticker, timeframe="1d")
        if ticker is None or ticker.empty:
            continue

        latest   = ticker.iloc[-1]
        required = ["Close", "High", "Low", "Open", "Volume", "RSI", "EMA20", "MACD", "MACD_SIGNAL", "HIGH_52W"]
        if not all(col in ticker.columns for col in required):
            continue
        if pd.isna(latest["RSI"]) or pd.isna(latest["MACD"]):
            continue

        close_price = float(latest["Close"])
        high_52w    = float(latest["HIGH_52W"])

        if high_52w <= 0:
            continue
        drop_pct = ((high_52w - close_price) / high_52w) * 100

        if drop_pct < MIN_DROP_FROM_52W_HIGH or drop_pct > MAX_DROP_FROM_52W_HIGH:
            continue

        current_rsi = float(latest["RSI"])
        past_10_rsi = ticker["RSI"].iloc[-11:-1].min()

        if current_rsi < RSI_CURL_MIN or past_10_rsi > RSI_OVERSOLD_THRESHOLD:
            continue

        ema20 = float(latest["EMA20"])
        if close_price < ema20:
            continue

        vol_now = float(latest["Volume"])
        vol_avg = float(ticker["Volume"].iloc[-21:-1].mean())
        if vol_avg <= 0:
            continue

        vol_ratio = vol_now / vol_avg
        if vol_ratio < MIN_VOLUME_RATIO:
            continue

        macd     = float(latest["MACD"])
        macd_sig = float(latest["MACD_SIGNAL"])
        if macd < macd_sig or macd > 2.0:
            continue

        reversal_signals = [
            f"📉 -{drop_pct:.1f}% from 52W High",
            "📈 RSI Oversold Curl",
            "🎯 Closed above 20 EMA",
            "📊 MACD Bullish Cross"
        ]

        today_str  = ist_now.strftime("%Y-%m-%d")
        # FIX: key order corrected to Category|Signals|Date|Scanner to match daily_report.py split logic
        dedup_key  = f"{category}|{symbol}|{today_str}|REVERSAL"

        candle_range  = float(latest["High"]) - float(latest["Low"])
        atr_val       = float(latest["ATR"]) if "ATR" in ticker.columns else (candle_range * 1.5)
        suggested_stop = close_price - (1.5 * atr_val)

        if not save_alert_if_new(symbol, dedup_key, ist_now.strftime("%Y-%m-%d %H:%M:%S")):
            continue

        alerts_by_category.setdefault(category, []).append({
            "symbol":          symbol,
            "category":        category,
            "breakout_signals": reversal_signals,
            "price":           round(close_price, 2),
            "open":            round(float(latest["Open"]), 2),
            "day_high":        round(float(latest["High"]), 2),
            "day_low":         round(float(latest["Low"]), 2),
            "rsi":             round(current_rsi, 1),
            "volume_ratio":    round(vol_ratio, 2),
            "body_ratio":      round(abs(close_price - float(latest["Open"])) / candle_range * 100)
                               if candle_range > 0 else 0,
            "score":           85,
            "above_ema20":     True,
            "atr_stop":        round(suggested_stop, 2),
            "peg":             row.get("PEG Ratio"),
            "yoy_rev":         row.get("YOY Revenue %"),
            "yoy_profit":      row.get("YOY Profit %"),
            "roe":             row.get("ROE %"),
        })
        total_alerts += 1

    if total_alerts > 0:
        for cat in sorted(alerts_by_category.keys()):
            cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["symbol"])
            chunks = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

            for chunk_num, chunk in enumerate(chunks, start=1):
                msg = build_message(
                    "REVERSAL", cat, chunk, chunk_num, len(chunks),
                    ist_now.strftime("%Y-%m-%d %H:%M:%S")
                )
                send_telegram_message(msg, scan_type="REVERSAL")

    logger.info(f"✅ REVERSAL SCAN DONE | Found {total_alerts} bottoming stocks.")


def start():
    """
    Scheduling loop — keeps the thread alive indefinitely.
    Skips weekends; otherwise runs a scan every _SCAN_INTERVAL_SECONDS.
    Dedup keys prevent duplicate Telegram alerts on the same calendar day.
    """
    init_db()

    while True:
        ist_now = datetime.now(IST)

        if ist_now.weekday() >= 5:
            logger.info("🔄 REVERSAL | Weekend detected — sleeping 1 hour")
            _time.sleep(3600)
            continue

        try:
            _run_scan()
        except Exception:
            logger.exception("❌ CRITICAL REVERSAL SCAN ERROR")

        logger.info(f"⏳ REVERSAL | Next scan in {_SCAN_INTERVAL_SECONDS // 60} minutes...")
        _time.sleep(_SCAN_INTERVAL_SECONDS)

# =====================================================================================
# app/live_scanner.py  — EARLY MOMENTUM VERSION
# =====================================================================================

import pandas as pd
import yfinance as yf
import time
import logging

from zoneinfo import ZoneInfo
from datetime import datetime, time as dt_time

from technical_indicators import apply_indicators
from breakout_engine import detect_breakouts
from scoring_engine import calculate_score
from telegram_engine import send_telegram_message
from database import init_db, alert_exists, save_alert

from config import WATCHLIST_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
init_db()
logger.info("✅ Database Initialized")

while True:

    ist_now = datetime.now(IST)
    current_time = ist_now.time()
    weekday = ist_now.weekday()

    market_open = dt_time(9, 15) <= current_time <= dt_time(15, 30)
    weekday_open = weekday < 5

    if not (market_open and weekday_open):
        logger.info(
            f"⏰ Market closed | "
            f"Current IST Time: {ist_now.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"Sleeping for 5 minutes..."
        )
        time.sleep(300)
        continue

    # ── LOAD WATCHLIST ───────────────────────────────────────────────────────────
    try:
        watchlist = pd.read_parquet(WATCHLIST_PATH)
    except Exception:
        logger.exception("❌ WATCHLIST LOAD ERROR")
        logger.info("🚀 RUNNING DAILY BUILDER...")
        from daily_builder import main as build_watchlist
        build_watchlist()
        watchlist = pd.read_parquet(WATCHLIST_PATH)

    scan_start = datetime.now(IST)
    total_alerts = 0

    logger.info("=" * 80)
    logger.info(f"🚀 NEW SCAN CYCLE STARTED | Stocks={len(watchlist)}")
    logger.info(f"⏰ IST Scan Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 80)

    for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):
        symbol = "UNKNOWN"
        try:
            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 Checking: {symbol} | 📊 Progress: {idx}/{len(watchlist)}")

            # ── DOWNLOAD — 15-MINUTE INTRADAY (5 days of data) ──────────────────
            # FIX 1: Was "1d/1d" — daily bars only fire alerts after the whole
            #         day closes, so by the time you see the signal the stock has
            #         already moved 8-10%.  15m bars let you catch the FIRST surge
            #         candle within minutes of momentum starting.
            ticker = yf.download(
                f"{symbol}.NS",
                period="5d",          # enough history for EMA20 / SMA50 on 15m bars
                interval="15m",       # ← changed from "1d"
                progress=False,
                auto_adjust=True,
                threads=False
            )

            if ticker.empty:
                logger.warning(f"❌ No data: {symbol}")
                continue

            ticker.reset_index(inplace=True)
            ticker = ticker.copy()

            if isinstance(ticker.columns, pd.MultiIndex):
                ticker.columns = ticker.columns.get_level_values(0)

            ticker = ticker.loc[:, ~ticker.columns.duplicated()]

            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            missing_col = False

            for col_name in required_cols:
                if col_name not in ticker.columns:
                    logger.warning(f"❌ Missing column {col_name}: {symbol}")
                    missing_col = True
                    break
                if isinstance(ticker[col_name], pd.DataFrame):
                    ticker[col_name] = ticker[col_name].iloc[:, 0]
                ticker[col_name] = pd.Series(ticker[col_name]).astype(float)

            if missing_col:
                continue

            ticker = ticker.dropna(subset=required_cols)

            # FIX 2: 15m bars — 5 days × ~25 bars/day = ~125 bars.
            #         Keep minimum at 50 so EMA20 is valid.
            if len(ticker) < 50:
                logger.warning(f"❌ Insufficient candles: {symbol}")
                continue

            ticker = apply_indicators(ticker)

            if ticker is None or ticker.empty:
                logger.warning(f"❌ Indicator failure: {symbol}")
                continue

            signals = detect_breakouts(ticker)

            if len(signals) == 0:
                continue

            latest = ticker.iloc[-1]

            if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
                continue

            # ── VOLUME ANALYSIS ──────────────────────────────────────────────────
            latest_volume = float(latest["Volume"])
            avg_volume    = float(ticker["Volume"].tail(20).mean())   # 20-bar avg on 15m

            if avg_volume <= 0:
                continue

            volume_ratio = latest_volume / avg_volume

            # ── CANDLE BODY RATIO ────────────────────────────────────────────────
            candle_range = float(latest["High"]) - float(latest["Low"])
            candle_body  = abs(float(latest["Close"]) - float(latest["Open"]))

            if candle_range <= 0:
                continue

            body_ratio = candle_body / candle_range

            # FIX 3: Was 0.5 — rejects many early breakout candles that are still
            #         forming.  0.4 is loose enough to catch the first decisive move.
            if body_ratio < 0.4:
                continue

            # ── MUST BE A GREEN CANDLE ────────────────────────────────────────────
            # FIX 4: NEW CHECK — only alert on bullish candles.
            #         This replaces the more aggressive RSI / golden-cross guards
            #         while still filtering noise.
            if float(latest["Close"]) <= float(latest["Open"]):
                continue

            # ── FAKE BREAKOUT FILTERS (RELAXED) ─────────────────────────────────

            # FIX 5: Volume — was 1.5x (misses the first expansion candle).
            #         1.2x catches the initial surge before the crowd piles in.
            if volume_ratio < 1.2:
                continue

            # FIX 6: RSI — was > 55 (lagging; stock already moved significantly).
            #         45 catches momentum as it's starting, not after it's confirmed.
            if latest["RSI"] < 45:
                continue

            # Keep overbought guard — avoid chasing
            if latest["RSI"] > 80:
                continue

            # FIX 7: Trend — was SMA50 > SMA200 (golden cross is a very lagging
            #         signal; rules out stocks in early-stage breakout from a base).
            #         Keeping only "above EMA20" is enough to confirm near-term trend.
            if float(latest["Close"]) < float(latest["EMA20"]):
                continue

            # Optional: above 50 DMA for a medium-term bias check (keep or remove)
            if "SMA50" in ticker.columns and not pd.isna(latest["SMA50"]):
                if float(latest["Close"]) < float(latest["SMA50"]):
                    continue

            # ── DUPLICATE GUARD ──────────────────────────────────────────────────
            breakout_type = ", ".join(signals)

            # Deduplicate per symbol+type within the same day (not per 15m bar)
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            dedup_key = f"{breakout_type}|{today_str}"

            if alert_exists(symbol, dedup_key):
                logger.info(f"⚠️ Duplicate skipped: {symbol}")
                continue

            # ── SCORE ────────────────────────────────────────────────────────────
            score = calculate_score(
                category=category,
                breakout_count=len(signals),
                rsi=float(latest["RSI"]),
                volume_ratio=volume_ratio
            )

            # FIX 8: Was 70 — lowered to 60 to match the relaxed early-momentum
            #         filters.  Score 60-70 = emerging setup; 70+ = strong setup.
            if score < 60:
                logger.info(f"❌ Weak setup skipped: {symbol} | Score={score}")
                continue

            # ── ALERT MESSAGE ────────────────────────────────────────────────────
            message = f'''
⚡ EARLY MOMENTUM ALERT

Stock: {symbol}
Category: {category}
Breakouts: {breakout_type}

Price:     ₹{round(float(latest["Close"]), 2)}
RSI:       {round(float(latest["RSI"]), 2)}
Vol Surge: {round(volume_ratio, 2)}x avg

Candle:    {"🟢 Bullish"} | Body {round(body_ratio * 100)}%
Trend:     ✅ Above EMA20

Score:     {score}/100
Bar:       15-min
Time:      {datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}
'''

            send_telegram_message(message)

            # Store with date suffix so duplicate check resets each day
            save_alert(symbol, dedup_key, datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"))

            total_alerts += 1
            logger.info(f"✅ ALERT SENT: {symbol} | Score={score} | Vol={round(volume_ratio,2)}x")

        except Exception:
            logger.exception(f"❌ ERROR: {symbol}")

    scan_end = datetime.now(IST)
    duration = (scan_end - scan_start).total_seconds()

    logger.info("=" * 80)
    logger.info(f"✅ SCAN COMPLETED | Duration={round(duration, 2)} sec")
    logger.info(f"📨 Alerts Sent={total_alerts}")
    logger.info("⏰ Sleeping 5 mins before next cycle...")
    logger.info("=" * 80)

    time.sleep(300)

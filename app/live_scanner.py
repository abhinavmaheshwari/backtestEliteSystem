# =====================================================================================
# app/live_scanner.py
# TREND CONFIRMATION SCANNER — 1H BARS
# =====================================================================================
#
# PURPOSE:
#   Second layer after intraday.py (15m early momentum).
#   By the time this fires, the stock has held its move for at least one full
#   hour — confirming the breakout is real and not a fake morning spike.
#
# FILTERS ARE INTENTIONALLY STRICTER THAN intraday.py:
#   RSI      > 55   (vs 45 in intraday)  — momentum confirmed, not just starting
#   Vol      > 1.5x (vs 1.2x in intraday) — sustained expansion, not one-candle spike
#   Body     > 55%  (vs 40% in intraday)  — strong decisive candle
#   SMA50    required                      — medium-term trend aligned
#   Golden cross check (SMA50 > SMA200)   — macro trend bullish
#
# CANDLE SAFETY:
#   Drops the latest bar if it is still within its 60-minute window.
#   Scanner starts at 10:16 AM so the first full 1h candle (9:15–10:15) is ready.
#
# DEDUP:
#   Per symbol + breakout type + date, with |1H suffix to avoid clash with
#   intraday.py alerts for the same stock on the same day.
#
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

# =====================================================================================
# LOGGER
# =====================================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

# =====================================================================================
# IST TIMEZONE
# =====================================================================================

IST = ZoneInfo("Asia/Kolkata")

# =====================================================================================
# INITIALIZE DATABASE
# =====================================================================================

init_db()

logger.info("✅ Database Initialized")

# =====================================================================================
# CONTINUOUS LIVE SCANNER
# =====================================================================================

while True:

    # ============================================================================
    # MARKET HOURS CHECK (IST)
    # Start at 10:16 AM — first full 1h candle (9:15–10:15) is complete
    # ============================================================================

    ist_now      = datetime.now(IST)
    current_time = ist_now.time()
    weekday      = ist_now.weekday()

    market_open = (
        dt_time(10, 16)
        <= current_time
        <= dt_time(15, 30)
    )

    weekday_open = weekday < 5

    if not (market_open and weekday_open):

        logger.info(
            f"⏰ Outside window (10:16–15:30) | "
            f"IST: {ist_now.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"Sleeping 5 mins..."
        )

        time.sleep(300)

        continue

    # ============================================================================
    # LOAD WATCHLIST
    # ============================================================================

    try:

        watchlist = pd.read_parquet(WATCHLIST_PATH)

    except Exception:

        logger.exception("❌ WATCHLIST LOAD ERROR")

        logger.info("🚀 RUNNING DAILY BUILDER...")

        from daily_builder import main as build_watchlist

        build_watchlist()

        watchlist = pd.read_parquet(WATCHLIST_PATH)

    # ============================================================================
    # SCAN START
    # ============================================================================

    scan_start   = datetime.now(IST)
    total_alerts = 0

    logger.info("=" * 80)
    logger.info(f"🚀 1H SCAN STARTED | Stocks={len(watchlist)}")
    logger.info(f"⏰ IST: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 80)

    # ============================================================================
    # MAIN STOCK LOOP
    # ============================================================================

    for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

        symbol = "UNKNOWN"

        try:

            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 [{idx}/{len(watchlist)}] {symbol}")

            # ====================================================================
            # DOWNLOAD — 1H BARS, 60 DAYS
            # ~360 bars — enough for EMA20, SMA50, SMA200 on hourly
            # ====================================================================

            ticker = yf.download(
                f"{symbol}.NS",
                period="60d",
                interval="1h",
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

            # ====================================================================
            # FORCE OHLCV TO 1D SERIES
            # ====================================================================

            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            missing_col   = False

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

            ticker = ticker.dropna(
                subset=["Open", "High", "Low", "Close", "Volume"]
            )

            # ====================================================================
            # COMPLETED CANDLE GUARD
            # Drop latest bar if still within its 60-min window
            # ====================================================================

            datetime_col = None

            for col in ["Datetime", "Date", "index"]:
                if col in ticker.columns:
                    datetime_col = col
                    break

            if datetime_col is not None:

                try:

                    latest_candle_time = pd.Timestamp(
                        ticker.iloc[-1][datetime_col]
                    ).replace(tzinfo=None)

                    now_naive = datetime.now(IST).replace(tzinfo=None)

                    candle_age_minutes = (
                        now_naive - latest_candle_time
                    ).total_seconds() / 60

                    if candle_age_minutes < 60:

                        logger.warning(
                            f"⚠️ 1h candle still forming "
                            f"({candle_age_minutes:.0f} min old) — "
                            f"dropping: {symbol}"
                        )

                        ticker = ticker.iloc[:-1].copy()

                except Exception:
                    logger.warning(f"⚠️ Could not check candle age: {symbol}")

            # ====================================================================
            # MINIMUM CANDLE CHECK
            # 100 bars — ensures SMA50 and EMA20 are reliable on 1h
            # ====================================================================

            if len(ticker) < 100:
                logger.warning(
                    f"❌ Insufficient candles ({len(ticker)}): {symbol}"
                )
                continue

            # ====================================================================
            # APPLY TECHNICAL INDICATORS
            # ====================================================================

            ticker = apply_indicators(ticker)

            if ticker is None or ticker.empty:
                logger.warning(f"❌ Indicator failure: {symbol}")
                continue

            # ====================================================================
            # DETECT BREAKOUTS
            # ====================================================================

            signals = detect_breakouts(ticker)

            if len(signals) == 0:
                continue

            latest = ticker.iloc[-1]

            # ====================================================================
            # RSI SAFETY
            # ====================================================================

            if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
                continue

            # ====================================================================
            # VOLUME ANALYSIS
            # 20-bar avg on 1h = ~3.5 trading days baseline
            # ====================================================================

            latest_volume = float(latest["Volume"])
            avg_volume    = float(ticker["Volume"].tail(20).mean())

            if avg_volume <= 0:
                continue

            volume_ratio = latest_volume / avg_volume

            # ====================================================================
            # CANDLE QUALITY
            # ====================================================================

            candle_range = float(latest["High"]) - float(latest["Low"])
            candle_body  = abs(float(latest["Close"]) - float(latest["Open"]))

            if candle_range <= 0:
                continue

            body_ratio = candle_body / candle_range

            # STRICTER than intraday (0.4) — need a decisive 1h candle
            if body_ratio < 0.55:
                continue

            # must be bullish
            if float(latest["Close"]) <= float(latest["Open"]):
                continue

            # ====================================================================
            # CONFIRMATION FILTERS — STRICTER THAN intraday.py
            # ====================================================================

            # Sustained volume expansion (not a one-candle spike)
            # intraday uses 1.2x — here we need 1.5x to confirm
            if volume_ratio < 1.5:
                continue

            # RSI confirms momentum is established, not just starting
            # intraday uses 45 — here we need 55
            if latest["RSI"] < 55:
                continue

            # Avoid overextended / parabolic
            if latest["RSI"] > 85:
                continue

            # Above 20 EMA
            if latest["Close"] < latest["EMA20"]:
                continue

            # Above 50 SMA — medium-term trend aligned
            if "SMA50" in ticker.columns and not pd.isna(latest["SMA50"]):
                if latest["Close"] < latest["SMA50"]:
                    continue

            # Golden cross — macro trend bullish
            # intraday does NOT check this; 1h scanner does
            if (
                "SMA50"  in ticker.columns and
                "SMA200" in ticker.columns and
                not pd.isna(latest["SMA50"]) and
                not pd.isna(latest["SMA200"])
            ):
                if latest["SMA50"] < latest["SMA200"]:
                    continue

            # ====================================================================
            # BREAKOUT TYPE
            # ====================================================================

            breakout_type = ", ".join(signals)

            # ====================================================================
            # DEDUP — |1H suffix prevents clash with intraday alerts
            # ====================================================================

            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            dedup_key = f"{breakout_type}|{today_str}|1H"

            if alert_exists(symbol, dedup_key):
                logger.info(f"⚠️ Duplicate skipped: {symbol}")
                continue

            # ====================================================================
            # CALCULATE SCORE
            # ====================================================================

            score = calculate_score(
                category=category,
                breakout_count=len(signals),
                rsi=float(latest["RSI"]),
                volume_ratio=volume_ratio
            )

            # STRICTER minimum score than intraday (60)
            if score < 75:
                logger.info(
                    f"❌ Weak setup skipped: {symbol} | Score={score}"
                )
                continue

            # ====================================================================
            # ALERT MESSAGE
            # ====================================================================

            message = f'''
🚀 TREND CONFIRMED ALERT — 1H

Stock: {symbol}

Category:
{category}

Breakouts:
{breakout_type}

Price:
₹{round(float(latest["Close"]), 2)}

RSI:
{round(float(latest["RSI"]), 2)}

Volume Expansion:
{round(volume_ratio, 2)}x

Candle:
🟢 Bullish | Body {round(body_ratio * 100)}%

Trend Structure:
✅ Above EMA20
✅ Above SMA50
✅ Bullish 50/200 DMA (Golden Cross)

Breakout Score:
{score}/100

Bar: 1H (completed)
Time:
{datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}
'''

            send_telegram_message(message)

            save_alert(
                symbol,
                dedup_key,
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            )

            total_alerts += 1

            logger.info(
                f"✅ 1H ALERT SENT: {symbol} | "
                f"Score={score} | "
                f"RSI={round(float(latest['RSI']), 1)} | "
                f"Vol={round(volume_ratio, 2)}x | "
                f"Body={round(body_ratio * 100)}%"
            )

        except Exception:
            logger.exception(f"❌ ERROR: {symbol}")

    # ============================================================================
    # SCAN END SUMMARY
    # ============================================================================

    scan_end = datetime.now(IST)
    duration = (scan_end - scan_start).total_seconds()

    logger.info("=" * 80)
    logger.info(f"✅ 1H SCAN COMPLETED | Duration={round(duration, 2)} sec")
    logger.info(f"📨 Alerts Sent={total_alerts}")
    logger.info("⏰ Sleeping 5 mins before next cycle...")
    logger.info("=" * 80)

    time.sleep(300)

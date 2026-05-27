# =====================================================================================
# app/intraday.py
# EARLY MOMENTUM SCANNER — 15M BARS
# =====================================================================================
#
# CANDLE SAFETY:
#   Uses interval="15m". Only analyses COMPLETED candles by dropping the latest
#   bar if it is still within its 15-minute window (candle_age_minutes < 15).
#   Scanner waits until 9:31 IST so the first full 15m candle (9:15–9:30) is
#   available before any stock is checked.
#
# INDICATOR BASIS:
#   period="5d" gives ~125 15m bars — enough for EMA20 and SMA50 on 15m.
#
# DESIGN INTENT:
#   Fires EARLIER than live_scanner.py by catching the first 15m momentum surge.
#   Looser filters (RSI > 45, vol > 1.2x) vs live scanner (RSI > 45, vol > 1.2x
#   on 1h bars) mean this alerts sooner in the move.
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
# CONTINUOUS INTRADAY SCANNER
# =====================================================================================

while True:

    # ============================================================================
    # MARKET HOURS CHECK (IST)
    # ============================================================================

    ist_now = datetime.now(IST)

    current_time = ist_now.time()

    weekday = ist_now.weekday()

    # Wait until 9:31 so the first full 15m candle (9:15–9:30) is complete
    market_open = (
        dt_time(9, 31)
        <= current_time
        <= dt_time(15, 30)
    )

    weekday_open = weekday < 5

    if not (market_open and weekday_open):

        logger.info(
            f"⏰ Market closed or pre-9:31 | "
            f"Current IST Time: "
            f"{ist_now.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"Sleeping for 5 minutes..."
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

    scan_start = datetime.now(IST)

    total_alerts = 0

    logger.info("=" * 80)

    logger.info(
        f"⚡ NEW INTRADAY SCAN CYCLE STARTED | "
        f"Stocks={len(watchlist)}"
    )

    logger.info(
        f"⏰ IST Scan Time: "
        f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}"
    )

    logger.info("=" * 80)

    # ============================================================================
    # MAIN STOCK LOOP
    # ============================================================================

    for idx, (_, row) in enumerate(

        watchlist.iterrows(),

        start=1
    ):

        symbol = "UNKNOWN"

        try:

            # ====================================================================
            # STOCK INFO
            # ====================================================================

            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 Checking: {symbol}")
            logger.info(f"📊 Progress: {idx}/{len(watchlist)}")

            # ====================================================================
            # DOWNLOAD PRICE DATA — 15M BARS
            # 5 days × ~25 bars/day = ~125 bars
            # ====================================================================

            ticker = yf.download(
                f"{symbol}.NS",
                period="5d",
                interval="15m",
                progress=False,
                auto_adjust=True,
                threads=False
            )

            if ticker.empty:

                logger.warning(f"❌ No data: {symbol}")

                continue

            # ====================================================================
            # RESET INDEX
            # ====================================================================

            ticker.reset_index(inplace=True)

            ticker = ticker.copy()

            # ====================================================================
            # FIX YFINANCE MULTI-INDEX / DUPLICATE COLUMNS
            # ====================================================================

            if isinstance(ticker.columns, pd.MultiIndex):

                ticker.columns = ticker.columns.get_level_values(0)

            ticker = ticker.loc[:, ~ticker.columns.duplicated()]

            # ====================================================================
            # FORCE OHLCV TO 1D SERIES
            # ====================================================================

            required_cols = ["Open", "High", "Low", "Close", "Volume"]

            missing_col = False

            for col_name in required_cols:

                if col_name not in ticker.columns:

                    logger.warning(
                        f"❌ Missing column {col_name}: {symbol}"
                    )

                    missing_col = True

                    break

                if isinstance(ticker[col_name], pd.DataFrame):

                    ticker[col_name] = ticker[col_name].iloc[:, 0]

                ticker[col_name] = pd.Series(
                    ticker[col_name]
                ).astype(float)

            if missing_col:

                continue

            # ====================================================================
            # DROP INVALID ROWS
            # ====================================================================

            ticker = ticker.dropna(
                subset=["Open", "High", "Low", "Close", "Volume"]
            )

            # ====================================================================
            # COMPLETED CANDLE GUARD
            # Detect the datetime column (yfinance uses "Datetime" for intraday)
            # Drop the latest bar if it is still within its 15-min window.
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

                    if candle_age_minutes < 15:

                        logger.warning(
                            f"⚠️ Latest 15m candle still forming "
                            f"({candle_age_minutes:.0f} min old) — "
                            f"dropping it: {symbol}"
                        )

                        # Drop the incomplete latest candle
                        ticker = ticker.iloc[:-1].copy()

                except Exception:

                    logger.warning(
                        f"⚠️ Could not check candle age for {symbol}"
                    )

            # ====================================================================
            # MINIMUM CANDLE CHECK
            # 50 bars minimum — ensures EMA20 is reliable on 15m
            # ====================================================================

            if len(ticker) < 50:

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

            # ====================================================================
            # LATEST COMPLETED CANDLE
            # ====================================================================

            latest = ticker.iloc[-1]

            # ====================================================================
            # RSI SAFETY
            # ====================================================================

            if "RSI" not in ticker.columns:

                continue

            if pd.isna(latest["RSI"]):

                continue

            # ====================================================================
            # VOLUME ANALYSIS
            # 20-bar avg on 15m = ~5 hours of intraday baseline
            # ====================================================================

            latest_volume = float(latest["Volume"])

            avg_volume = float(
                ticker["Volume"].tail(20).mean()
            )

            if avg_volume <= 0:

                continue

            volume_ratio = latest_volume / avg_volume

            # ====================================================================
            # STRONG BREAKOUT CANDLE FILTER
            # ====================================================================

            candle_range = (
                float(latest["High"]) - float(latest["Low"])
            )

            candle_body = abs(
                float(latest["Close"]) - float(latest["Open"])
            )

            if candle_range <= 0:

                continue

            body_ratio = candle_body / candle_range

            # weak candle rejection — looser than live scanner to catch early moves
            if body_ratio < 0.4:

                continue

            # must be a green (bullish) candle
            if float(latest["Close"]) <= float(latest["Open"]):

                continue

            # ====================================================================
            # FAKE BREAKOUT FILTERS (EARLY MOMENTUM — RELAXED)
            # ====================================================================

            # first volume surge — lower than live scanner
            if volume_ratio < 1.2:

                continue

            # RSI rising — catching momentum as it starts
            if latest["RSI"] < 45:

                continue

            # avoid overextended moves
            if latest["RSI"] > 80:

                continue

            # above 20 EMA — near-term trend confirmation only
            if latest["Close"] < latest["EMA20"]:

                continue

            # above 50 SMA if available
            if "SMA50" in ticker.columns and not pd.isna(latest["SMA50"]):

                if latest["Close"] < latest["SMA50"]:

                    continue

            # ====================================================================
            # BREAKOUT TYPE
            # ====================================================================

            breakout_type = ", ".join(signals)

            # ====================================================================
            # AVOID DUPLICATE ALERTS (per symbol + type + day)
            # Resets each day so next-day signals are not blocked
            # ====================================================================

            today_str = datetime.now(IST).strftime("%Y-%m-%d")

            dedup_key = f"{breakout_type}|{today_str}"

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

            # ====================================================================
            # MINIMUM SCORE FILTER
            # ====================================================================

            if score < 60:

                logger.info(
                    f"❌ Weak setup skipped: {symbol} | Score={score}"
                )

                continue

            # ====================================================================
            # ALERT MESSAGE
            # ====================================================================

            message = f'''
⚡ INTRADAY SCANNER — 15M EARLY MOMENTUM

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

Trend:
✅ Above EMA20

Breakout Score:
{score}/100

Bar: 15M (completed)
Time:
{datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}
'''

            # ====================================================================
            # SEND TELEGRAM ALERT
            # ====================================================================

            send_telegram_message(message)

            # ====================================================================
            # SAVE ALERT
            # ====================================================================

            save_alert(
                symbol,
                dedup_key,
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            )

            total_alerts += 1

            logger.info(
                f"✅ ALERT SENT: {symbol} | "
                f"Score={score} | "
                f"Vol={round(volume_ratio, 2)}x"
            )

        # ========================================================================
        # ERROR HANDLING
        # ========================================================================

        except Exception:

            logger.exception(f"❌ ERROR: {symbol}")

    # ============================================================================
    # SCAN END SUMMARY
    # ============================================================================

    scan_end = datetime.now(IST)

    duration = (scan_end - scan_start).total_seconds()

    logger.info("=" * 80)

    logger.info(
        f"✅ INTRADAY SCAN COMPLETED | "
        f"Duration={round(duration, 2)} sec"
    )

    logger.info(f"📨 Alerts Sent={total_alerts}")

    logger.info("⏰ Sleeping 5 mins before next cycle...")

    logger.info("=" * 80)

    # ============================================================================
    # WAIT BEFORE NEXT SCAN
    # ============================================================================

    time.sleep(300)

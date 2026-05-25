# =====================================================================================
# app/live_scanner.py
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
    # ============================================================================

    ist_now = datetime.now(IST)

    current_time = ist_now.time()

    weekday = ist_now.weekday()

    market_open = (

        dt_time(9, 15)

        <= current_time

        <= dt_time(15, 30)
    )

    weekday_open = weekday < 5

    if not (market_open and weekday_open):

        logger.info(
            f"⏰ Market closed | "
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

        watchlist = pd.read_parquet(
            WATCHLIST_PATH
        )

    except Exception:

        logger.exception(
            "❌ WATCHLIST LOAD ERROR"
        )

        logger.info(
            "🚀 RUNNING DAILY BUILDER..."
        )

        from daily_builder import main as build_watchlist

        build_watchlist()

        watchlist = pd.read_parquet(
            WATCHLIST_PATH
        )

    # ============================================================================
    # SCAN START
    # ============================================================================

    scan_start = datetime.now(IST)

    total_alerts = 0

    logger.info("=" * 80)

    logger.info(
        f"🚀 NEW SCAN CYCLE STARTED | "
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

            symbol = row["Stock"]

            category = row["Category"]

            logger.info(
                f"🔍 Checking: {symbol}"
            )

            logger.info(
                f"📊 Progress: "
                f"{idx}/{len(watchlist)}"
            )

            # ====================================================================
            # DOWNLOAD PRICE DATA
            # ====================================================================

            ticker = yf.download(

                f"{symbol}.NS",

                period="1y",

                interval="1d",

                progress=False,

                auto_adjust=True,

                threads=False
            )

            if ticker.empty:

                logger.warning(
                    f"❌ No data: {symbol}"
                )

                continue

            # ====================================================================
            # RESET INDEX
            # ====================================================================

            ticker.reset_index(inplace=True)

            ticker = ticker.copy()

            # ====================================================================
            # FIX YFINANCE MULTI-INDEX / DUPLICATE COLUMNS
            # ====================================================================

            if isinstance(
                ticker.columns,
                pd.MultiIndex
            ):

                ticker.columns = (
                    ticker.columns
                    .get_level_values(0)
                )

            # remove duplicate columns
            ticker = ticker.loc[
                :,
                ~ticker.columns.duplicated()
            ]

            # ====================================================================
            # FORCE OHLCV TO 1D SERIES
            # ====================================================================

            required_cols = [

                "Open",
                "High",
                "Low",
                "Close",
                "Volume"
            ]

            missing_col = False

            for col_name in required_cols:

                if col_name not in ticker.columns:

                    logger.warning(
                        f"❌ Missing column "
                        f"{col_name}: {symbol}"
                    )

                    missing_col = True

                    break

                # dataframe -> series
                if isinstance(

                    ticker[col_name],

                    pd.DataFrame
                ):

                    ticker[col_name] = (

                        ticker[col_name]

                        .iloc[:, 0]
                    )

                # ndarray/object -> float series
                ticker[col_name] = pd.Series(

                    ticker[col_name]

                ).astype(float)

            if missing_col:

                continue

            # ====================================================================
            # DROP INVALID ROWS
            # ====================================================================

            ticker = ticker.dropna(

                subset=[

                    "Open",
                    "High",
                    "Low",
                    "Close",
                    "Volume"
                ]
            )

            if len(ticker) < 50:

                logger.warning(
                    f"❌ Insufficient candles: "
                    f"{symbol}"
                )

                continue

            # ====================================================================
            # APPLY TECHNICAL INDICATORS
            # ====================================================================

            ticker = apply_indicators(
                ticker
            )

            if ticker is None or ticker.empty:

                logger.warning(
                    f"❌ Indicator failure: "
                    f"{symbol}"
                )

                continue

            # ====================================================================
            # DETECT BREAKOUTS
            # ====================================================================

            signals = detect_breakouts(
                ticker
            )

            if len(signals) == 0:

                continue

            # ====================================================================
            # LATEST CANDLE
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
            # ====================================================================

            latest_volume = float(
                latest["Volume"]
            )

            avg_volume = float(

                ticker["Volume"]

                .tail(10)

                .mean()
            )

            if avg_volume <= 0:

                continue

            volume_ratio = (

                latest_volume

                / avg_volume
            )

            # ====================================================================
            # STRONG BREAKOUT CANDLE FILTER
            # ====================================================================

            candle_range = (

                float(latest["High"])

                - float(latest["Low"])
            )

            candle_body = abs(

                float(latest["Close"])

                - float(latest["Open"])
            )

            if candle_range <= 0:

                continue

            body_ratio = (

                candle_body

                / candle_range
            )

            # weak candle rejection
            if body_ratio < 0.5:

                continue

            # ====================================================================
            # FAKE BREAKOUT FILTERS
            # ====================================================================

            # volume expansion required
            if volume_ratio < 1.5:

                continue

            # healthy RSI
            if latest["RSI"] < 55:

                continue

            # avoid overextended moves
            if latest["RSI"] > 85:

                continue

            # above 20 EMA
            if latest["Close"] < latest["EMA20"]:

                continue

            # above 50 DMA
            if latest["Close"] < latest["SMA50"]:

                continue

            # bullish trend structure
            if latest["SMA50"] < latest["SMA200"]:

                continue

            # ====================================================================
            # BREAKOUT TYPE
            # ====================================================================

            breakout_type = ", ".join(
                signals
            )

            # ====================================================================
            # AVOID DUPLICATE ALERTS
            # ====================================================================

            if alert_exists(

                symbol,

                breakout_type
            ):

                logger.info(
                    f"⚠️ Duplicate skipped: "
                    f"{symbol}"
                )

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

            if score < 70:

                logger.info(

                    f"❌ Weak setup skipped: "

                    f"{symbol} | Score={score}"
                )

                continue

            # ====================================================================
            # ALERT MESSAGE
            # ====================================================================

            message = f'''
🚀 ELITE BREAKOUT ALERT

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

Trend Structure:
✅ Above 20 EMA
✅ Above 50 DMA
✅ Bullish 50/200 DMA

Breakout Score:
{score}/100

Time:
{datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}
'''

            # ====================================================================
            # SEND TELEGRAM ALERT
            # ====================================================================

            send_telegram_message(
                message
            )

            # ====================================================================
            # SAVE ALERT
            # ====================================================================

            save_alert(

                symbol,

                breakout_type,

                datetime.now(IST).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )

            total_alerts += 1

            logger.info(
                f"✅ ALERT SENT: {symbol}"
            )

        # ========================================================================
        # ERROR HANDLING
        # ========================================================================

        except Exception:

            logger.exception(
                f"❌ ERROR: {symbol}"
            )

    # ============================================================================
    # SCAN END SUMMARY
    # ============================================================================

    scan_end = datetime.now(IST)

    duration = (

        scan_end - scan_start

    ).total_seconds()

    logger.info("=" * 80)

    logger.info(
        f"✅ SCAN COMPLETED | "
        f"Duration={round(duration, 2)} sec"
    )

    logger.info(
        f"📨 Alerts Sent={total_alerts}"
    )

    logger.info(
        "⏰ Sleeping 5 mins before next cycle..."
    )

    logger.info("=" * 80)

    # ============================================================================
    # WAIT BEFORE NEXT SCAN
    # ============================================================================

    time.sleep(300)

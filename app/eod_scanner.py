# =====================================================================================
# app/eod_scanner.py
# EOD BREAKOUT SCANNER — DAILY CANDLES
# =====================================================================================
#
# DESIGN INTENT:
#   Runs once per day at 3:16 PM IST — daily candle is fully settled.
#   This is the "end of day confirmation" scanner — by 3:16 PM the candle body,
#   volume, and RSI are settled enough to trust as a daily signal.
#
# CANDLE BASIS:
#   Uses interval="1d", period="1y" — full year of daily candles.
#   The latest (today's) candle is INCLUDED intentionally since we run at 3:16 PM
#   — it is near-complete and reflects the full day's price action.
#
# DEDUP:
#   Stores alerts with today's date suffix so the same stock can fire again
#   on a different day if it sets up again.
#
# ALERT WINDOW:
#   Fires only between 3:16 PM and 3:30 PM IST — a tight 14-min window.
#   Outside this window the scanner sleeps and waits for the next trading day.
#
# CONSOLIDATED ALERTS:
#   Collects all alerts during the scan cycle.
#   Sends one Telegram message per category at the end (sorted by score desc).
#   Large categories are chunked at 10 stocks per message.
#
# =====================================================================================

import pandas as pd
import yfinance as yf
import time
import logging

from zoneinfo import ZoneInfo
from datetime import datetime, time as dt_time, timedelta

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
# EOD WINDOW
# =====================================================================================

EOD_START = dt_time(15, 16)   # 3:16 PM — daily candle fully settled
EOD_END   = dt_time(15, 30)   # 3:30 PM — market close

# =====================================================================================
# SCANNER HEADER
# =====================================================================================

HEADER = "📊 EOD BREAKOUT — DAILY"

# =====================================================================================
# CHUNK SIZE — max stocks per Telegram message (4096 char limit safety)
# =====================================================================================

CHUNK_SIZE = 10

# =====================================================================================
# INITIALIZE DATABASE
# =====================================================================================

init_db()

logger.info("✅ Database Initialized")

# =====================================================================================
# HELPER — seconds until next EOD window
# =====================================================================================

def seconds_until_eod():
    """
    Returns how many seconds to sleep until the next 3:16 PM IST window.
    If already past today's window, calculates time to tomorrow's 3:16 PM.
    """
    now = datetime.now(IST)

    target_today = now.replace(
        hour=15, minute=16, second=0, microsecond=0
    )

    if now < target_today:
        delta = target_today - now
    else:
        target_tomorrow = target_today + timedelta(days=1)
        delta = target_tomorrow - now

    return max(int(delta.total_seconds()), 0)

# =====================================================================================
# TRACK WHETHER TODAY'S SCAN HAS ALREADY RUN
# =====================================================================================

last_scan_date = None

# =====================================================================================
# CONTINUOUS LOOP
# =====================================================================================

while True:

    ist_now      = datetime.now(IST)
    current_time = ist_now.time()
    weekday      = ist_now.weekday()
    today_str    = ist_now.strftime("%Y-%m-%d")

    # ============================================================================
    # CHECK: WEEKDAY + EOD WINDOW + NOT ALREADY RUN TODAY
    # ============================================================================

    in_eod_window = EOD_START <= current_time <= EOD_END
    is_weekday    = weekday < 5
    already_ran   = (last_scan_date == today_str)

    if not is_weekday:

        sleep_secs = seconds_until_eod()

        logger.info(
            f"📅 Weekend — next scan in "
            f"{sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
        )

        time.sleep(min(sleep_secs, 3600))

        continue

    if already_ran:

        sleep_secs = seconds_until_eod()

        logger.info(
            f"✅ EOD scan already completed for {today_str} | "
            f"Next scan in "
            f"{sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
        )

        time.sleep(min(sleep_secs, 3600))

        continue

    if not in_eod_window:

        sleep_secs = seconds_until_eod()

        logger.info(
            f"⏰ Waiting for EOD window (3:16 PM IST) | "
            f"Current: {ist_now.strftime('%H:%M:%S')} | "
            f"Sleeping {sleep_secs // 60}m {sleep_secs % 60}s"
        )

        time.sleep(min(sleep_secs, 60))

        continue

    # ============================================================================
    # EOD WINDOW REACHED — RUN THE SCAN
    # ============================================================================

    logger.info("=" * 80)
    logger.info(
        f"📊 EOD SCAN TRIGGERED | "
        f"IST: {ist_now.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    logger.info("=" * 80)

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

    scan_start         = datetime.now(IST)
    total_alerts       = 0
    alerts_by_category = {}   # { category: [ alert_dict, ... ] }

    logger.info(
        f"🚀 EOD SCAN STARTED | "
        f"Stocks={len(watchlist)}"
    )

    # ============================================================================
    # MAIN STOCK LOOP
    # ============================================================================

    for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

        symbol = "UNKNOWN"

        try:

            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 [{idx}/{len(watchlist)}] Checking: {symbol}")

            # ====================================================================
            # DOWNLOAD DAILY DATA — 1 YEAR
            # ~252 trading days — sufficient for SMA200
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
            # MINIMUM CANDLE CHECK
            # Need at least 200 daily candles for SMA200 to be valid
            # ====================================================================

            if len(ticker) < 200:
                logger.warning(
                    f"❌ Insufficient daily candles "
                    f"({len(ticker)}): {symbol}"
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
            # LATEST DAILY CANDLE (near-complete at 3:16 PM)
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
            # 10-bar avg on daily = 2 trading weeks baseline
            # ====================================================================

            latest_volume = float(latest["Volume"])
            avg_volume    = float(ticker["Volume"].tail(10).mean())

            if avg_volume <= 0:
                continue

            volume_ratio = latest_volume / avg_volume

            # ====================================================================
            # CANDLE QUALITY FILTER
            # ====================================================================

            candle_range = float(latest["High"]) - float(latest["Low"])
            candle_body  = abs(float(latest["Close"]) - float(latest["Open"]))

            if candle_range <= 0:
                continue

            body_ratio = candle_body / candle_range

            if body_ratio < 0.5:
                continue

            if float(latest["Close"]) <= float(latest["Open"]):
                continue

            # ====================================================================
            # EOD CONFIRMATION FILTERS
            # ====================================================================

            if volume_ratio < 1.5:
                continue

            if latest["RSI"] < 55:
                continue

            if latest["RSI"] > 85:
                continue

            if latest["Close"] < latest["EMA20"]:
                continue

            if "SMA50" in ticker.columns and not pd.isna(latest["SMA50"]):
                if latest["Close"] < latest["SMA50"]:
                    continue

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
            # AVOID DUPLICATE ALERTS (per symbol + type + day)
            # ====================================================================

            dedup_key = f"{breakout_type}|{today_str}|EOD"

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

            if score < 80:
                logger.info(
                    f"❌ Weak setup skipped: {symbol} | Score={score}"
                )
                continue

            # ====================================================================
            # COLLECT ALERT — do NOT send individually
            # ====================================================================

            alert_data = {
                "symbol":        symbol,
                "breakout_type": breakout_type,
                "price":         round(float(latest["Close"]), 2),
                "rsi":           round(float(latest["RSI"]), 2),
                "volume_ratio":  round(volume_ratio, 2),
                "body_ratio":    round(body_ratio * 100),
                "score":         score,
            }

            if category not in alerts_by_category:
                alerts_by_category[category] = []

            alerts_by_category[category].append(alert_data)

            # ====================================================================
            # SAVE DEDUP
            # ====================================================================

            save_alert(
                symbol,
                dedup_key,
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            )

            total_alerts += 1

            logger.info(
                f"✅ EOD ALERT COLLECTED: {symbol} | "
                f"Score={score} | "
                f"Vol={round(volume_ratio, 2)}x"
            )

        except Exception:
            logger.exception(f"❌ ERROR: {symbol}")

    # ============================================================================
    # SEND CONSOLIDATED MESSAGES — one per category, chunked if large
    # ============================================================================

    scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    for cat in sorted(alerts_by_category.keys()):

        cat_alerts = sorted(
            alerts_by_category[cat],
            key=lambda x: x["score"],
            reverse=True
        )

        chunks = [
            cat_alerts[i:i + CHUNK_SIZE]
            for i in range(0, len(cat_alerts), CHUNK_SIZE)
        ]

        for chunk_num, chunk in enumerate(chunks, start=1):

            suffix = (
                f" ({chunk_num}/{len(chunks)})"
                if len(chunks) > 1
                else ""
            )

            lines = []

            for a in chunk:
                lines.append(
                    f"📌 {a['symbol']}\n"
                    f"   Breakout : {a['breakout_type']}\n"
                    f"   Price    : ₹{a['price']}\n"
                    f"   RSI      : {a['rsi']}\n"
                    f"   Volume   : {a['volume_ratio']}x\n"
                    f"   Candle   : 🟢 Body {a['body_ratio']}%\n"
                    f"   Score    : {a['score']}/100\n"
                )

            message = (
                f"{HEADER}{suffix}\n"
                f"{'=' * 35}\n"
                f"Category : {cat}\n"
                f"Stocks   : {len(cat_alerts)}\n"
                f"{'=' * 35}\n\n"
                + "\n".join(lines)
                + f"\n⏰ {scan_time}"
            )

            send_telegram_message(message)

            logger.info(
                f"📨 Consolidated alert sent | "
                f"Category={cat} | "
                f"Chunk={chunk_num}/{len(chunks)} | "
                f"Stocks={len(chunk)}"
            )

    # ============================================================================
    # SCAN COMPLETE
    # ============================================================================

    scan_end = datetime.now(IST)
    duration = (scan_end - scan_start).total_seconds()

    logger.info("=" * 80)
    logger.info(
        f"✅ EOD SCAN COMPLETED | "
        f"Duration={round(duration, 2)} sec"
    )
    logger.info(f"📨 EOD Alerts Sent={total_alerts}")
    logger.info("=" * 80)

    # ============================================================================
    # MARK TODAY AS DONE — sleep until tomorrow's EOD window
    # ============================================================================

    last_scan_date = today_str

    sleep_secs = seconds_until_eod()

    logger.info(
        f"💤 EOD scan done for {today_str} | "
        f"Next scan in "
        f"{sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
    )

    time.sleep(min(sleep_secs, 3600))

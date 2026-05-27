# =====================================================================================
# app/eod_scanner.py
# EOD BREAKOUT SCANNER — DAILY CANDLES
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
from database import init_db, save_alert_if_new, cleanup_old_alerts

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

IST        = ZoneInfo("Asia/Kolkata")
EOD_START  = dt_time(15, 16)
EOD_END    = dt_time(15, 30)
HEADER     = "📊 EOD BREAKOUT DAILY"
CHUNK_SIZE = 10

# =====================================================================================
# INIT
# =====================================================================================

init_db()
cleanup_old_alerts(days=7)
logger.info("✅ Database Initialized")

# =====================================================================================
# HELPER
# =====================================================================================

def seconds_until_eod():
    now          = datetime.now(IST)
    target_today = now.replace(hour=15, minute=16, second=0, microsecond=0)
    if now < target_today:
        delta = target_today - now
    else:
        delta = target_today + timedelta(days=1) - now
    return max(int(delta.total_seconds()), 0)

# =====================================================================================
# TRACK DAILY RUN
# =====================================================================================

last_scan_date = None

# =====================================================================================
# MAIN LOOP
# =====================================================================================

while True:

    ist_now      = datetime.now(IST)
    current_time = ist_now.time()
    weekday      = ist_now.weekday()
    today_str    = ist_now.strftime("%Y-%m-%d")

    in_eod_window = EOD_START <= current_time <= EOD_END
    is_weekday    = weekday < 5
    already_ran   = (last_scan_date == today_str)

    if not is_weekday:
        sleep_secs = seconds_until_eod()
        logger.info(f"📅 Weekend | next scan in {sleep_secs//3600}h {(sleep_secs%3600)//60}m")
        time.sleep(min(sleep_secs, 3600))
        continue

    if already_ran:
        sleep_secs = seconds_until_eod()
        logger.info(f"✅ EOD done for {today_str} | next in {sleep_secs//3600}h {(sleep_secs%3600)//60}m")
        time.sleep(min(sleep_secs, 3600))
        continue

    if not in_eod_window:
        sleep_secs = seconds_until_eod()
        logger.info(f"⏰ Waiting 3:16 PM | now {ist_now.strftime('%H:%M:%S')} | {sleep_secs//60}m {sleep_secs%60}s")
        time.sleep(min(sleep_secs, 60))
        continue

    # ============================================================================
    # EOD WINDOW — RUN SCAN
    # ============================================================================

    logger.info("=" * 80)
    logger.info(f"📊 EOD SCAN | {ist_now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 80)

    try:
        watchlist = pd.read_parquet(WATCHLIST_PATH)
    except Exception:
        logger.exception("❌ WATCHLIST LOAD ERROR")
        from daily_builder import main as build_watchlist
        build_watchlist()
        watchlist = pd.read_parquet(WATCHLIST_PATH)

    scan_start         = datetime.now(IST)
    total_alerts       = 0
    alerts_by_category = {}

    logger.info(f"🚀 EOD SCAN STARTED | Stocks={len(watchlist)}")

    # ============================================================================
    # STOCK LOOP
    # ============================================================================

    for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

        symbol = "UNKNOWN"

        try:

            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 [{idx}/{len(watchlist)}] {symbol}")

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

            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            missing_col   = False

            for col_name in required_cols:
                if col_name not in ticker.columns:
                    logger.warning(f"❌ Missing {col_name}: {symbol}")
                    missing_col = True
                    break
                if isinstance(ticker[col_name], pd.DataFrame):
                    ticker[col_name] = ticker[col_name].iloc[:, 0]
                ticker[col_name] = pd.Series(ticker[col_name]).astype(float)

            if missing_col:
                continue

            ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

            if len(ticker) < 200:
                logger.warning(f"❌ Insufficient candles ({len(ticker)}): {symbol}")
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

            latest_volume = float(latest["Volume"])
            avg_volume    = float(ticker["Volume"].tail(10).mean())

            if avg_volume <= 0:
                continue

            volume_ratio = latest_volume / avg_volume

            candle_range = float(latest["High"]) - float(latest["Low"])
            candle_body  = abs(float(latest["Close"]) - float(latest["Open"]))

            if candle_range <= 0:
                continue

            body_ratio = candle_body / candle_range

            if body_ratio < 0.5:
                continue
            if float(latest["Close"]) <= float(latest["Open"]):
                continue
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
                "SMA50" in ticker.columns and "SMA200" in ticker.columns and
                not pd.isna(latest["SMA50"]) and not pd.isna(latest["SMA200"])
            ):
                if latest["SMA50"] < latest["SMA200"]:
                    continue

            breakout_type = ", ".join(signals)
            dedup_key     = f"{breakout_type}|{today_str}|EOD"

            score = calculate_score(
                category=category,
                breakout_count=len(signals),
                rsi=float(latest["RSI"]),
                volume_ratio=volume_ratio
            )

            if score < 80:
                logger.info(f"❌ Low score: {symbol} | {score}")
                continue

            saved = save_alert_if_new(
                symbol,
                dedup_key,
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            )

            if not saved:
                logger.info(f"⚠️ Duplicate: {symbol}")
                continue

            alert_data = {
                "symbol":        symbol,
                "breakout_type": breakout_type,
                "price":         round(float(latest["Close"]), 2),
                "rsi":           round(float(latest["RSI"]), 1),
                "volume_ratio":  round(volume_ratio, 2),
                "body_ratio":    round(body_ratio * 100),
                "score":         score,
            }

            alerts_by_category.setdefault(category, []).append(alert_data)
            total_alerts += 1

            logger.info(f"✅ Collected: {symbol} | Score={score} | Vol={round(volume_ratio,2)}x")

        except Exception:
            logger.exception(f"❌ ERROR: {symbol}")

    # ============================================================================
    # SEND CONSOLIDATED MESSAGES
    # ============================================================================

    scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    for cat in sorted(alerts_by_category.keys()):

        cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)
        chunks     = [cat_alerts[i:i+CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

        for chunk_num, chunk in enumerate(chunks, start=1):

            suffix = f" ({chunk_num}/{len(chunks)})" if len(chunks) > 1 else ""
            lines  = []

            for a in chunk:
                lines.append(
                    f"▸ {a['symbol']} | ₹{a['price']} | {a['score']}/100\n"
                    f"  {a['breakout_type']}\n"
                    f"  RSI {a['rsi']} | Vol {a['volume_ratio']}x | Body {a['body_ratio']}%"
                )

            message = (
                f"{HEADER}{suffix}\n"
                f"{'─' * 30}\n"
                f"{cat} | {len(cat_alerts)} stock{'s' if len(cat_alerts)>1 else ''}\n"
                f"{'─' * 30}\n\n"
                + "\n\n".join(lines)
                + f"\n\n⏰ {scan_time}"
            )

            send_telegram_message(message)
            logger.info(f"📨 Sent | {cat} | {chunk_num}/{len(chunks)} | {len(chunk)} stocks")

    # ============================================================================
    # DONE
    # ============================================================================

    duration       = (datetime.now(IST) - scan_start).total_seconds()
    last_scan_date = today_str
    sleep_secs     = seconds_until_eod()

    logger.info("=" * 80)
    logger.info(f"✅ EOD DONE | {round(duration,2)}s | Alerts={total_alerts}")
    logger.info(f"💤 Next scan in {sleep_secs//3600}h {(sleep_secs%3600)//60}m")
    logger.info("=" * 80)

    time.sleep(min(sleep_secs, 3600))

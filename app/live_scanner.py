# =====================================================================================
# app/live_scanner.py
# TREND CONFIRMATION SCANNER — 1H BARS
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
from message_formatter import build_message
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
CHUNK_SIZE = 10

# =====================================================================================
# STRICT FILTERS — 1H
# =====================================================================================

MIN_SIGNALS      = 2
MIN_BODY_RATIO   = 0.55
MIN_VOLUME_RATIO = 1.8
MIN_RSI          = 55
MAX_RSI          = 80
MIN_SCORE        = 80

# =====================================================================================
# INIT
# =====================================================================================

init_db()
cleanup_old_alerts(days=7)
logger.info("✅ Database Initialized")

# =====================================================================================
# MAIN LOOP
# =====================================================================================

while True:

    ist_now      = datetime.now(IST)
    current_time = ist_now.time()
    weekday      = ist_now.weekday()

    market_open  = dt_time(10, 17) <= current_time <= dt_time(15, 30)
    weekday_open = weekday < 5

    if not (market_open and weekday_open):
        logger.info(f"⏰ Outside 10:17-15:30 | {ist_now.strftime('%H:%M:%S')} | sleeping 5m")
        time.sleep(300)
        continue

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

    logger.info("=" * 80)
    logger.info(f"🚀 1H SCAN | Stocks={len(watchlist)} | {scan_start.strftime('%H:%M:%S')}")
    logger.info("=" * 80)

    for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

        symbol = "UNKNOWN"

        try:

            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 [{idx}/{len(watchlist)}] {symbol}")

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

            datetime_col = None
            for col in ["Datetime", "Date", "index"]:
                if col in ticker.columns:
                    datetime_col = col
                    break

            if datetime_col is not None:
                try:
                    candle_start = pd.Timestamp(ticker.iloc[-1][datetime_col]).replace(tzinfo=None)
                    candle_end   = candle_start + pd.Timedelta(minutes=60)
                    now_naive    = datetime.now(IST).replace(tzinfo=None)
                    if now_naive < candle_end:
                        logger.warning(f"⚠️ Candle forming until {candle_end.strftime('%H:%M')} — dropped: {symbol}")
                        ticker = ticker.iloc[:-1].copy()
                except Exception:
                    logger.warning(f"⚠️ Candle age check failed: {symbol}")

            if len(ticker) < 100:
                logger.warning(f"❌ Insufficient candles ({len(ticker)}): {symbol}")
                continue

            # FIX: pass timeframe="1h" so HIGH_52W uses all available bars
            ticker = apply_indicators(ticker, timeframe="1h")

            if ticker is None or ticker.empty:
                logger.warning(f"❌ Indicator failure: {symbol}")
                continue

            signals = detect_breakouts(ticker)

            if len(signals) < MIN_SIGNALS:
                logger.info(f"❌ Weak signals ({len(signals)}): {symbol}")
                continue

            latest = ticker.iloc[-1]

            if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
                continue

            latest_volume = float(latest["Volume"])
            avg_volume    = float(ticker["Volume"].tail(20).mean())

            if avg_volume <= 0:
                continue

            volume_ratio = latest_volume / avg_volume
            candle_range = float(latest["High"]) - float(latest["Low"])
            candle_body  = abs(float(latest["Close"]) - float(latest["Open"]))

            if candle_range <= 0:
                continue

            body_ratio = candle_body / candle_range

            if body_ratio < MIN_BODY_RATIO:
                continue
            if float(latest["Close"]) <= float(latest["Open"]):
                continue
            if volume_ratio < MIN_VOLUME_RATIO:
                continue
            if not (MIN_RSI <= latest["RSI"] <= MAX_RSI):
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
                    logger.info(f"❌ No golden cross: {symbol}")
                    continue

            breakout_type = ", ".join(signals)
            today_str     = datetime.now(IST).strftime("%Y-%m-%d")
            dedup_key     = f"{breakout_type}|{today_str}|1H"

            # FIX: pass ticker, latest, symbol so all scoring components run
            score = calculate_score(
                category=category,
                breakout_count=len(signals),
                rsi=float(latest["RSI"]),
                volume_ratio=volume_ratio,
                breakout_signals=signals,
                ticker=ticker,
                latest=latest,
                symbol=symbol,
            )

            if score < MIN_SCORE:
                logger.info(f"❌ Low score {score}: {symbol}")
                continue

            saved = save_alert_if_new(
                symbol,
                dedup_key,
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            )

            if not saved:
                logger.info(f"⚠️ Duplicate: {symbol}")
                continue

            above_sma50 = (
                bool(latest["Close"] >= latest["SMA50"])
                if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50"))
                else None
            )
            golden_cross = (
                bool(latest["SMA50"] >= latest["SMA200"])
                if (
                    "SMA50" in ticker.columns and "SMA200" in ticker.columns
                    and not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200"))
                )
                else None
            )

            alerts_by_category.setdefault(category, []).append({
                "symbol":           symbol,
                "category":         category,
                "breakout_signals": signals,
                "price":            round(float(latest["Close"]), 2),
                "open":             round(float(latest["Open"]), 2),
                "day_high":         round(float(latest["High"]), 2),
                "day_low":          round(float(latest["Low"]), 2),
                "rsi":              round(float(latest["RSI"]), 1),
                "volume_ratio":     round(volume_ratio, 2),
                "body_ratio":       round(body_ratio * 100),
                "score":            score,
                "above_ema20":      bool(latest["Close"] >= latest["EMA20"]),
                "above_sma50":      above_sma50,
                "golden_cross":     golden_cross,
            })
            total_alerts += 1

            logger.info(f"✅ Collected: {symbol} | Score={score} | Vol={round(volume_ratio,2)}x | Signals={len(signals)}")

        except Exception:
            logger.exception(f"❌ ERROR: {symbol}")

    # ============================================================================
    # SEND
    # ============================================================================

    scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    for cat in sorted(alerts_by_category.keys()):

        cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)
        chunks     = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

        for chunk_num, chunk in enumerate(chunks, start=1):
            msg = build_message("1H", cat, chunk, chunk_num, len(chunks), scan_time)
            send_telegram_message(msg)
            logger.info(f"📨 Sent | {cat} | {chunk_num}/{len(chunks)} | {len(chunk)} stocks")

    duration = (datetime.now(IST) - scan_start).total_seconds()
    logger.info("=" * 80)
    logger.info(f"✅ 1H DONE | {round(duration,2)}s | Alerts={total_alerts}")
    logger.info("=" * 80)

    time.sleep(300)

# =====================================================================================
# app/intraday.py
# EARLY MOMENTUM SCANNER — 15M BARS
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
HEADER     = "⚡ INTRADAY 15M"
CHUNK_SIZE = 10

# =====================================================================================
# STRICT FILTER CONSTANTS — 15M (intraday momentum, slightly looser than EOD)
# =====================================================================================

MIN_SIGNALS      = 1        # single strong breakout acceptable intraday
MIN_BODY_RATIO   = 0.50     # decent candle body
MIN_VOLUME_RATIO = 1.5      # clear volume confirmation
MIN_RSI          = 52       # momentum starting to build
MAX_RSI          = 78       # not overbought
MIN_SCORE        = 75       # good but not perfect setups

# =====================================================================================
# MESSAGE FORMATTER
# =====================================================================================

def score_badge(score):
    if score >= 90: return "🏆"
    if score >= 80: return "🔥"
    if score >= 70: return "⚡"
    return "📌"

def signal_bar(score):
    filled = round(score / 20)
    return "█" * filled + "░" * (5 - filled)

def breakout_emoji(signals):
    if "52W Breakout"     in signals: return "🚀"
    if "Monthly Breakout" in signals: return "🌕"
    if "Weekly Breakout"  in signals: return "📈"
    return "📊"

def format_intraday_alert(a):
    badge = score_badge(a["score"])
    bar   = signal_bar(a["score"])
    bem   = breakout_emoji(a["breakout_signals"])

    if a["score"] >= 90:   tier = "ELITE"
    elif a["score"] >= 80: tier = "STRONG"
    else:                  tier = "GOOD"

    lines = [
        f"{badge} <b>{a['symbol']}</b>  [{tier} · {a['score']}/100]",
        f"   {bar}",
        f"   ₹{a['price']}   RSI {a['rsi']}   Vol {a['volume_ratio']}x   Body {a['body_ratio']}%",
        f"   {bem} {a['breakout_type']}",
    ]
    return "\n".join(lines)

def build_intraday_message(cat, alerts, chunk_num, total_chunks, scan_time):

    suffix = f" — part {chunk_num}/{total_chunks}" if total_chunks > 1 else ""

    lines = [
        f"⚡ <b>INTRADAY 15M</b>{suffix}",
        f"{'─' * 32}",
        f"<b>{cat}</b>  ·  {len(alerts)} stock{'s' if len(alerts) > 1 else ''}",
        f"{'─' * 32}",
        "",
    ]

    for a in alerts:
        lines.append(format_intraday_alert(a))
        lines.append("")

    lines.append(f"⏰ <i>{scan_time}</i>")

    return "\n".join(lines)

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

    market_open  = dt_time(9, 32) <= current_time <= dt_time(15, 30)
    weekday_open = weekday < 5

    if not (market_open and weekday_open):
        logger.info(
            f"⏰ Pre-9:32 or closed | "
            f"{ist_now.strftime('%H:%M:%S')} | sleeping 5m"
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
        from daily_builder import main as build_watchlist
        build_watchlist()
        watchlist = pd.read_parquet(WATCHLIST_PATH)

    scan_start         = datetime.now(IST)
    total_alerts       = 0
    alerts_by_category = {}

    logger.info("=" * 80)
    logger.info(f"⚡ INTRADAY SCAN | Stocks={len(watchlist)} | {scan_start.strftime('%H:%M:%S')}")
    logger.info("=" * 80)

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
                period="5d",
                interval="15m",
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

            # ====================================================================
            # COMPLETED CANDLE GUARD
            # ====================================================================

            datetime_col = None
            for col in ["Datetime", "Date", "index"]:
                if col in ticker.columns:
                    datetime_col = col
                    break

            if datetime_col is not None:
                try:
                    candle_start = pd.Timestamp(
                        ticker.iloc[-1][datetime_col]
                    ).replace(tzinfo=None)
                    candle_end = candle_start + pd.Timedelta(minutes=15)
                    now_naive  = datetime.now(IST).replace(tzinfo=None)
                    if now_naive < candle_end:
                        logger.warning(
                            f"⚠️ Candle forming until {candle_end.strftime('%H:%M')} — dropped: {symbol}"
                        )
                        ticker = ticker.iloc[:-1].copy()
                except Exception:
                    logger.warning(f"⚠️ Candle age check failed: {symbol}")

            if len(ticker) < 50:
                logger.warning(f"❌ Insufficient candles ({len(ticker)}): {symbol}")
                continue

            ticker = apply_indicators(ticker)

            if ticker is None or ticker.empty:
                logger.warning(f"❌ Indicator failure: {symbol}")
                continue

            signals = detect_breakouts(ticker)

            if len(signals) < MIN_SIGNALS:
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

            # ── STRICT FILTERS ────────────────────────────────────────────────────
            if body_ratio < MIN_BODY_RATIO:
                logger.info(f"❌ Weak body ({body_ratio:.0%}): {symbol}")
                continue
            if float(latest["Close"]) <= float(latest["Open"]):
                continue
            if volume_ratio < MIN_VOLUME_RATIO:
                logger.info(f"❌ Low volume ({volume_ratio:.2f}x): {symbol}")
                continue
            if not (MIN_RSI <= latest["RSI"] <= MAX_RSI):
                logger.info(f"❌ RSI out of range ({latest['RSI']:.1f}): {symbol}")
                continue
            if latest["Close"] < latest["EMA20"]:
                continue
            if "SMA50" in ticker.columns and not pd.isna(latest["SMA50"]):
                if latest["Close"] < latest["SMA50"]:
                    continue

            breakout_type = ", ".join(signals)
            today_str     = datetime.now(IST).strftime("%Y-%m-%d")
            dedup_key     = f"{breakout_type}|{today_str}"

            score = calculate_score(
                category=category,
                breakout_count=len(signals),
                rsi=float(latest["RSI"]),
                volume_ratio=volume_ratio,
                breakout_signals=signals
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

            alert_data = {
                "symbol":           symbol,
                "breakout_type":    breakout_type,
                "breakout_signals": signals,
                "price":            round(float(latest["Close"]), 2),
                "rsi":              round(float(latest["RSI"]), 1),
                "volume_ratio":     round(volume_ratio, 2),
                "body_ratio":       round(body_ratio * 100),
                "score":            score,
            }

            alerts_by_category.setdefault(category, []).append(alert_data)
            total_alerts += 1

            logger.info(f"✅ Collected: {symbol} | Score={score} | Vol={round(volume_ratio,2)}x | Signals={len(signals)}")

        except Exception:
            logger.exception(f"❌ ERROR: {symbol}")

    # ============================================================================
    # SEND CONSOLIDATED MESSAGES
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
            message = build_intraday_message(cat, chunk, chunk_num, len(chunks), scan_time)
            send_telegram_message(message)
            logger.info(f"📨 Sent | {cat} | {chunk_num}/{len(chunks)} | {len(chunk)} stocks")

    # ============================================================================
    # SUMMARY
    # ============================================================================

    duration = (datetime.now(IST) - scan_start).total_seconds()
    logger.info("=" * 80)
    logger.info(f"✅ INTRADAY DONE | {round(duration,2)}s | Alerts={total_alerts}")
    logger.info("=" * 80)

    time.sleep(300)

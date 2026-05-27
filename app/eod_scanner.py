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
CHUNK_SIZE = 10

# =====================================================================================
# STRICT FILTER CONSTANTS — EOD (Daily candle, highest conviction)
# =====================================================================================

MIN_SIGNALS      = 2        # require at least 2 breakout signals
MIN_BODY_RATIO   = 0.60     # strong bullish candle body
MIN_VOLUME_RATIO = 2.0      # meaningful volume surge
MIN_RSI          = 58       # confirmed momentum
MAX_RSI          = 78       # not overbought
MIN_SCORE        = 85       # only A-grade setups

# =====================================================================================
# MESSAGE FORMATTER
# =====================================================================================

def score_badge(score):
    if score >= 90:
        return "🏆"
    elif score >= 80:
        return "🔥"
    elif score >= 70:
        return "⚡"
    else:
        return "📌"

def signal_bar(score):
    filled = round(score / 20)   # 0–5 blocks
    return "█" * filled + "░" * (5 - filled)

def breakout_emoji(signals):
    if "52W Breakout"     in signals: return "🚀"
    if "Monthly Breakout" in signals: return "🌕"
    if "Weekly Breakout"  in signals: return "📈"
    return "📊"

def format_eod_alert(a):
    badge = score_badge(a["score"])
    bar   = signal_bar(a["score"])
    bem   = breakout_emoji(a["breakout_signals"])

    # score tier label
    if a["score"] >= 90:
        tier = "ELITE"
    elif a["score"] >= 80:
        tier = "STRONG"
    else:
        tier = "GOOD"

    lines = [
        f"{badge} <b>{a['symbol']}</b>  [{tier} · {a['score']}/100]",
        f"   {bar}",
        f"   ₹{a['price']}   RSI {a['rsi']}   Vol {a['volume_ratio']}x   Body {a['body_ratio']}%",
        f"   {bem} {a['breakout_type']}",
    ]
    return "\n".join(lines)

def build_eod_message(cat, alerts, chunk_num, total_chunks, scan_time):

    suffix = f" — part {chunk_num}/{total_chunks}" if total_chunks > 1 else ""

    # header
    lines = [
        f"📊 <b>EOD BREAKOUT DAILY</b>{suffix}",
        f"{'─' * 32}",
        f"<b>{cat}</b>  ·  {len(alerts)} stock{'s' if len(alerts) > 1 else ''}",
        f"{'─' * 32}",
        "",
    ]

    for a in alerts:
        lines.append(format_eod_alert(a))
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

            # ── STRICT: minimum 2 breakout signals ──────────────────────────────
            if len(signals) < MIN_SIGNALS:
                logger.info(f"❌ Weak signals ({len(signals)}): {symbol}")
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
            # ── Golden cross mandatory for EOD ───────────────────────────────────
            if (
                "SMA50" in ticker.columns and "SMA200" in ticker.columns and
                not pd.isna(latest["SMA50"]) and not pd.isna(latest["SMA200"])
            ):
                if latest["SMA50"] < latest["SMA200"]:
                    logger.info(f"❌ No golden cross: {symbol}")
                    continue

            breakout_type = ", ".join(signals)
            dedup_key     = f"{breakout_type}|{today_str}|EOD"

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
            message = build_eod_message(cat, chunk, chunk_num, len(chunks), scan_time)
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

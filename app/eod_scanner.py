# =====================================================================================
# app/eod_scanner.py
# EOD BREAKOUT SCANNER — DAILY CANDLES
#
# WHAT THIS FILE DOES:
#   Runs between 6:00 PM and 7:15 PM IST, performing up to MAX_SCAN_ATTEMPTS scans
#   per trading day. Multiple runs ensure at least one alert fires even if the first
#   pass has a transient data issue (yfinance returning stale data, bhavcopy delayed).
#
#   Timing rationale:
#     3:30 PM — NSE market closes
#     5:00–5:30 PM — NSE publishes bhavcopy (delivery data); inconsistent before 6 PM
#     6:00 PM — EOD_START: bhavcopy is reliably available every trading day
#     7:15 PM — EOD_END: window closes; all alerts sent well before end of evening
#     ~15 min — SCAN_INTERVAL_MINUTES between scans
#
#   Why multiple scans?
#     On some days yfinance returns yesterday's close at 6:00 PM (CDN propagation lag).
#     The data-freshness guard rejects stale bars. By retrying at 6:15 and 6:30 we
#     almost certainly catch the data once it propagates. The dedup key prevents the
#     same stock from being alerted twice on the same day.
#
# SCAN SCHEDULE (example):
#   Attempt 1 — 6:00 PM  (bhavcopy just published, some yfinance data still propagating)
#   Attempt 2 — 6:15 PM  (yfinance fully settled, nearly all stocks fresh)
#   Attempt 3 — 6:30 PM  (safety net; bhavcopy almost always available)
#   Attempt 4 — 6:45 PM  (final pass; catches any stragglers)
#
# FILTER PIPELINE (in order — a stock must pass ALL of these):
#   1.  Data quality          — 200 candles minimum, no missing columns
#   2.  Data freshness        — latest bar must be today's date (no cached/stale data)
#   3.  Signal count          — at least 3 breakout signals (strictest confluence)
#   4.  Candle body           — body ≥ 60% of range
#   5.  Bullish close         — close strictly above open
#   6.  Close position        — close in top 25% of daily range
#   7.  Upper wick            — wick ≤ 30% of range
#   8.  Volume ratio          — current day ≥ 2.0× 20-day average
#   9.  Avg volume floor      — 20-day avg ≥ 200K shares
#   10. Min stock price       — close ≥ ₹50
#   11. RSI range             — RSI 58–75
#   12. RSI direction + divergence — RSI rising over 5 days AND no hidden bearish divergence
#   13. EMA20                 — close above EMA20
#   14. SMA50                 — close above SMA50
#   15. Golden cross          — SMA50 ≥ SMA200
#   16. MACD                  — MACD line above signal line
#   17. 52W high proximity    — within 15% of 52-week high
#   18. ATR-adjusted move cap — day's move ≤ 3× ATR(14)
#   19. Score threshold       — composite score ≥ 78 (boosted if sector confluence ≥ 3)
#       Score incorporates delivery conviction bonus (+2/+4/+6) from NSE bhavcopy
#
# CHANGES FROM PREVIOUS VERSION:
#   + MULTIPLE SCANS: up to MAX_SCAN_ATTEMPTS (4) per trading day, spaced
#     SCAN_INTERVAL_MINUTES (15) apart within the 6:00–7:15 PM window.
#     Each scan uses a per-scan dedup key so a stock alerted in attempt 1 is not
#     re-alerted in attempt 2. A per-day dedup ensures one alert per stock per day.
#   + EOD_END extended from 19:00 → 19:15 to fit 4 attempts at 15-min intervals.
#   + BHAVCOPY RETRY: fetch_delivery_data now has built-in retry logic (in
#     delivery_data.py). The scanner still logs bhavcopy status clearly per attempt.
#   + TIMING FIX: main.py EOD window must align with EOD_START (18:00) — see main.py.
#   + All other logic (ATR cap, data freshness, hidden divergence, sector confluence,
#     rotating log file, scoring) unchanged.
# =====================================================================================

import pandas as pd
import yfinance as yf
import time
import logging
import logging.handlers
import os

from zoneinfo import ZoneInfo
from datetime import datetime, date, time as dt_time, timedelta

from technical_indicators import apply_indicators
from breakout_engine import detect_breakouts
from scoring_engine import calculate_score
from telegram_engine import send_telegram_message
from message_formatter import build_message
from database import init_db, save_alert_if_new, cleanup_old_alerts
from delivery_data import fetch_delivery_data

from config import WATCHLIST_PATH

# =====================================================================================
# LOGGER — dual output: console + persistent rejections.log
# =====================================================================================

LOG_DIR            = os.environ.get("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
REJECTION_LOG_PATH = os.path.join(LOG_DIR, "rejections.log")

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)

_file_handler = logging.handlers.RotatingFileHandler(
    REJECTION_LOG_PATH,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setLevel(logging.INFO)

_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
_console_handler.setFormatter(_formatter)
_file_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── EOD SCAN WINDOW ───────────────────────────────────────────────────────────────────
#
# 6:00 PM — bhavcopy is reliably published by NSE
# 7:15 PM — extended end to fit 4 scan attempts at 15-min intervals
#
# Attempt schedule (approximate):
#   18:00, 18:15, 18:30, 18:45  ← 4 attempts, 15 min apart
#
EOD_START              = dt_time(18, 0)
EOD_END                = dt_time(19, 15)
SCAN_INTERVAL_MINUTES  = 15    # gap between consecutive scan attempts
MAX_SCAN_ATTEMPTS      = 4     # max scans per trading day

CHUNK_SIZE = 10

# =====================================================================================
# FILTER CONSTANTS — EOD DAILY
# =====================================================================================

MIN_SIGNALS            = 3
MIN_BODY_RATIO         = 0.60
MIN_CLOSE_POSITION     = 0.75
MAX_UPPER_WICK_RATIO   = 0.30
MIN_VOLUME_RATIO       = 2.0
MIN_AVG_VOLUME_SHARES  = 200_000
MIN_STOCK_PRICE        = 50.0
MIN_RSI                = 58
MAX_RSI                = 75
RSI_LOOKBACK_BARS      = 5
ATR_MOVE_MULTIPLIER    = 3.0
MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0
MIN_SCORE              = 78

SECTOR_CONFLUENCE_THRESHOLD = 3
SECTOR_CONFLUENCE_BONUS     = 5

# =====================================================================================
# INIT
# =====================================================================================

init_db()
cleanup_old_alerts(days=7)
logger.info("✅ Database initialized | Stale alerts cleaned (7-day window)")
logger.info(f"📝 Rejection log: {os.path.abspath(REJECTION_LOG_PATH)}")

# =====================================================================================
# HELPERS
# =====================================================================================

def seconds_until_eod() -> int:
    """Return seconds until the next 6:00 PM IST scan window opens."""
    now          = datetime.now(IST)
    target_today = now.replace(hour=EOD_START.hour, minute=EOD_START.minute,
                               second=0, microsecond=0)
    if now < target_today:
        delta = target_today - now
    else:
        delta = target_today + timedelta(days=1) - now
    return max(int(delta.total_seconds()), 0)


def compute_atr(ticker: pd.DataFrame, period: int = 14) -> float | None:
    """
    Compute ATR(14) from a daily OHLCV DataFrame.
    Returns None if insufficient data. ATR is in price units (₹).
    """
    if len(ticker) < period + 1:
        return None

    high  = ticker["High"].values
    low   = ticker["Low"].values
    close = ticker["Close"].values

    tr_high_low  = high[1:] - low[1:]
    tr_high_prev = abs(high[1:] - close[:-1])
    tr_low_prev  = abs(low[1:] - close[:-1])

    true_range = pd.Series(
        [max(a, b, c) for a, b, c in zip(tr_high_low, tr_high_prev, tr_low_prev)]
    )
    return float(true_range.tail(period).mean())


# ── STATE TRACKING ────────────────────────────────────────────────────────────────────
#
# last_scan_date    — the calendar date of the last completed scan attempt
# scan_attempt_num  — how many scan attempts have been made today (resets each new day)
# last_scan_time    — wall-clock time (IST) when the last scan finished, for interval gating
#
last_scan_date   = None
scan_attempt_num = 0
last_scan_time   = None   # datetime object (IST-aware) or None

# =====================================================================================
# MAIN LOOP
# =====================================================================================

while True:

    ist_now      = datetime.now(IST)
    current_time = ist_now.time()
    weekday      = ist_now.weekday()
    today_str    = ist_now.strftime("%Y-%m-%d")
    today_date   = ist_now.date()

    in_eod_window = EOD_START <= current_time <= EOD_END
    is_weekday    = weekday < 5

    # Reset attempt counter on a new calendar day
    if last_scan_date != today_str:
        scan_attempt_num = 0
        last_scan_time   = None

    # ── WEEKEND ──────────────────────────────────────────────────────────────────────
    if not is_weekday:
        sleep_secs = seconds_until_eod()
        logger.info(
            f"📅 Weekend ({ist_now.strftime('%A')}) | "
            f"Next scan window opens in {sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
        )
        time.sleep(min(sleep_secs, 3600))
        continue

    # ── MAX ATTEMPTS REACHED TODAY ────────────────────────────────────────────────────
    if scan_attempt_num >= MAX_SCAN_ATTEMPTS:
        sleep_secs = seconds_until_eod()
        logger.info(
            f"✅ EOD: {MAX_SCAN_ATTEMPTS} scan attempts completed for {today_str} | "
            f"Next window opens in {sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
        )
        time.sleep(min(sleep_secs, 3600))
        continue

    # ── NOT YET IN EOD WINDOW ────────────────────────────────────────────────────────
    if not in_eod_window:
        sleep_secs = seconds_until_eod()
        logger.info(
            f"⏰ Waiting for EOD window | Now={ist_now.strftime('%H:%M:%S')} | "
            f"Opens {EOD_START.strftime('%H:%M')} | "
            f"{sleep_secs // 60}m {sleep_secs % 60}s remaining"
        )
        time.sleep(min(sleep_secs, 60))
        continue

    # ── INTERVAL GATE: wait SCAN_INTERVAL_MINUTES between attempts ────────────────────
    if last_scan_time is not None:
        elapsed_since_last = (ist_now - last_scan_time).total_seconds()
        interval_secs      = SCAN_INTERVAL_MINUTES * 60
        if elapsed_since_last < interval_secs:
            wait_remaining = int(interval_secs - elapsed_since_last)
            logger.info(
                f"⏳ Interval gate | Attempt {scan_attempt_num + 1}/{MAX_SCAN_ATTEMPTS} | "
                f"Next scan in {wait_remaining // 60}m {wait_remaining % 60}s"
            )
            time.sleep(min(wait_remaining, 60))
            continue

    # ================================================================================
    # EOD SCAN ATTEMPT
    # ================================================================================

    scan_attempt_num += 1

    logger.info("=" * 80)
    logger.info(
        f"📊 EOD SCAN ATTEMPT {scan_attempt_num}/{MAX_SCAN_ATTEMPTS} | "
        f"{ist_now.strftime('%Y-%m-%d %H:%M:%S IST')}"
    )
    logger.info("=" * 80)

    # ── FETCH DELIVERY DATA ───────────────────────────────────────────────────────────
    # Fetched once per scan attempt, before the stock loop starts.
    # delivery_data.py now includes retry logic (up to 3 HTTP attempts with backoff).
    # delivery_map: {symbol_str: delivery_pct_float} — e.g. {"RELIANCE": 54.3}
    # Empty dict if all retries fail — scoring engine handles None gracefully per stock.
    delivery_map = fetch_delivery_data(today_date)

    if delivery_map:
        logger.info(
            f"📦 Delivery data ready | {len(delivery_map):,} symbols | "
            f"Attempt={scan_attempt_num}/{MAX_SCAN_ATTEMPTS}"
        )
    else:
        logger.warning(
            f"⚠️ Delivery data unavailable for attempt {scan_attempt_num} | "
            f"Scoring will proceed WITHOUT delivery bonus | "
            f"Check delivery_data.py logs above for the specific failure reason."
        )

    # ── LOAD WATCHLIST ───────────────────────────────────────────────────────────────
    try:
        watchlist = pd.read_parquet(WATCHLIST_PATH)
        logger.info(f"📋 Watchlist loaded | {len(watchlist)} stocks")
    except Exception:
        logger.exception("❌ Watchlist load failed — rebuilding from daily_builder")
        from daily_builder import main as build_watchlist
        build_watchlist()
        watchlist = pd.read_parquet(WATCHLIST_PATH)
        logger.info(f"📋 Watchlist rebuilt | {len(watchlist)} stocks")

    scan_start         = datetime.now(IST)
    total_alerts       = 0
    alerts_by_category: dict[str, list[dict]] = {}

    rejection_counts = {
        "no_data":               0,
        "stale_data":            0,
        "missing_col":           0,
        "insufficient_bars":     0,
        "indicator_fail":        0,
        "weak_signals":          0,
        "weak_body":             0,
        "bearish_candle":        0,
        "weak_close_pos":        0,
        "upper_wick":            0,
        "low_volume":            0,
        "low_avg_volume":        0,
        "penny_stock":           0,
        "rsi_range":             0,
        "rsi_not_rising":        0,
        "rsi_hidden_divergence": 0,
        "below_ema20":           0,
        "below_sma50":           0,
        "no_golden_cross":       0,
        "macd_bearish":          0,
        "far_from_52w_high":     0,
        "exhaustion_move":       0,
        "low_score":             0,
        "duplicate":             0,
    }

    logger.info(f"🚀 Processing {len(watchlist)} stocks | Attempt {scan_attempt_num}/{MAX_SCAN_ATTEMPTS}...")

    for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

        symbol = "UNKNOWN"

        try:

            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 [{idx}/{len(watchlist)}] {symbol} | Category={category}")

            # ── DOWNLOAD DATA ────────────────────────────────────────────────────────
            ticker = yf.download(
                f"{symbol}.NS",
                period="1y",
                interval="1d",
                progress=False,
                auto_adjust=True,
                threads=False
            )

            if ticker.empty:
                logger.warning(f"  ❌ No data returned from yfinance: {symbol}")
                rejection_counts["no_data"] += 1
                continue

            ticker.reset_index(inplace=True)
            ticker = ticker.copy()

            if isinstance(ticker.columns, pd.MultiIndex):
                ticker.columns = ticker.columns.get_level_values(0)

            ticker = ticker.loc[:, ~ticker.columns.duplicated()]

            # ── COLUMN VALIDATION ─────────────────────────────────────────────────────
            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            missing_col   = False

            for col_name in required_cols:
                if col_name not in ticker.columns:
                    logger.warning(f"  ❌ Missing column '{col_name}': {symbol}")
                    missing_col = True
                    break
                if isinstance(ticker[col_name], pd.DataFrame):
                    ticker[col_name] = ticker[col_name].iloc[:, 0]
                ticker[col_name] = pd.Series(ticker[col_name]).astype(float)

            if missing_col:
                rejection_counts["missing_col"] += 1
                continue

            ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

            # ── MINIMUM BAR COUNT ─────────────────────────────────────────────────────
            if len(ticker) < 200:
                logger.warning(f"  ❌ Insufficient history ({len(ticker)} < 200 bars): {symbol}")
                rejection_counts["insufficient_bars"] += 1
                continue

            # ── DATA FRESHNESS GUARD ──────────────────────────────────────────────────
            # At 6 PM, the latest daily bar should be today.
            # If yfinance returns yesterday's data (CDN lag), reject and wait for next
            # scan attempt when the data will have propagated.
            if "Date" in ticker.columns:
                latest_date_raw = ticker["Date"].iloc[-1]
                if hasattr(latest_date_raw, "date"):
                    latest_bar_date = latest_date_raw.date()
                else:
                    latest_bar_date = pd.to_datetime(str(latest_date_raw)).date()

                if latest_bar_date != today_date:
                    logger.warning(
                        f"  ❌ Stale data (latest bar={latest_bar_date}, today={today_date}): {symbol} | "
                        f"Will retry on next scan attempt."
                    )
                    rejection_counts["stale_data"] += 1
                    continue

            # ── INDICATORS ───────────────────────────────────────────────────────────
            ticker = apply_indicators(ticker, timeframe="1d")

            if ticker is None or ticker.empty:
                logger.warning(f"  ❌ Indicator calculation failed: {symbol}")
                rejection_counts["indicator_fail"] += 1
                continue

            # ── BREAKOUT SIGNALS ──────────────────────────────────────────────────────
            signals = detect_breakouts(ticker)

            if len(signals) < MIN_SIGNALS:
                logger.info(f"  ❌ Weak signals ({len(signals)} < {MIN_SIGNALS}): {symbol}")
                rejection_counts["weak_signals"] += 1
                continue

            logger.info(f"  ✔ Signals ({len(signals)}): {', '.join(signals)}")

            latest = ticker.iloc[-1]

            if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
                logger.warning(f"  ❌ RSI unavailable: {symbol}")
                continue

            # ── VOLUME ───────────────────────────────────────────────────────────────
            latest_volume = float(latest["Volume"])
            avg_volume    = float(ticker["Volume"].tail(20).mean())

            if avg_volume <= 0:
                logger.warning(f"  ❌ Zero average volume: {symbol}")
                continue

            volume_ratio = latest_volume / avg_volume

            # ── CANDLE GEOMETRY ───────────────────────────────────────────────────────
            candle_high  = float(latest["High"])
            candle_low   = float(latest["Low"])
            candle_open  = float(latest["Open"])
            candle_close = float(latest["Close"])
            candle_range = candle_high - candle_low
            candle_body  = abs(candle_close - candle_open)
            upper_wick   = candle_high - candle_close

            if candle_range <= 0:
                logger.warning(f"  ❌ Zero candle range: {symbol}")
                continue

            body_ratio     = candle_body / candle_range
            close_position = (candle_close - candle_low) / candle_range
            wick_ratio     = upper_wick / candle_range
            rsi_val        = float(latest["RSI"])

            # ── FILTER 1: CANDLE BODY ─────────────────────────────────────────────────
            if body_ratio < MIN_BODY_RATIO:
                logger.info(f"  ❌ Weak body ({body_ratio:.0%} < {MIN_BODY_RATIO:.0%}): {symbol}")
                rejection_counts["weak_body"] += 1
                continue

            # ── FILTER 2: BULLISH CANDLE ──────────────────────────────────────────────
            if candle_close <= candle_open:
                logger.info(
                    f"  ❌ Bearish/doji candle (C={candle_close:.2f} ≤ O={candle_open:.2f}): {symbol}"
                )
                rejection_counts["bearish_candle"] += 1
                continue

            # ── FILTER 3: CLOSE POSITION ──────────────────────────────────────────────
            if close_position < MIN_CLOSE_POSITION:
                logger.info(
                    f"  ❌ Weak close position ({close_position:.0%} in range, need ≥{MIN_CLOSE_POSITION:.0%}): {symbol}"
                )
                rejection_counts["weak_close_pos"] += 1
                continue

            # ── FILTER 4: UPPER WICK ──────────────────────────────────────────────────
            if wick_ratio > MAX_UPPER_WICK_RATIO:
                logger.info(
                    f"  ❌ Upper wick ({wick_ratio:.0%} > {MAX_UPPER_WICK_RATIO:.0%}): {symbol}"
                )
                rejection_counts["upper_wick"] += 1
                continue

            # ── FILTER 5: VOLUME RATIO ────────────────────────────────────────────────
            if volume_ratio < MIN_VOLUME_RATIO:
                logger.info(
                    f"  ❌ Low volume ({volume_ratio:.2f}x < {MIN_VOLUME_RATIO}x 20-day avg): {symbol}"
                )
                rejection_counts["low_volume"] += 1
                continue

            # ── FILTER 6: AVG VOLUME FLOOR ────────────────────────────────────────────
            if avg_volume < MIN_AVG_VOLUME_SHARES:
                logger.info(
                    f"  ❌ Illiquid (avg vol {avg_volume:,.0f} < {MIN_AVG_VOLUME_SHARES:,}): {symbol}"
                )
                rejection_counts["low_avg_volume"] += 1
                continue

            # ── FILTER 7: MINIMUM PRICE ───────────────────────────────────────────────
            if candle_close < MIN_STOCK_PRICE:
                logger.info(f"  ❌ Penny stock (₹{candle_close:.2f} < ₹{MIN_STOCK_PRICE}): {symbol}")
                rejection_counts["penny_stock"] += 1
                continue

            # ── FILTER 8: RSI RANGE ───────────────────────────────────────────────────
            if not (MIN_RSI <= rsi_val <= MAX_RSI):
                logger.info(
                    f"  ❌ RSI out of range ({rsi_val:.1f}, need {MIN_RSI}–{MAX_RSI}): {symbol}"
                )
                rejection_counts["rsi_range"] += 1
                continue

            # ── FILTER 9: RSI DIRECTION + HIDDEN BEARISH DIVERGENCE ──────────────────
            if len(ticker) > RSI_LOOKBACK_BARS:
                rsi_prev   = float(ticker["RSI"].iloc[-1 - RSI_LOOKBACK_BARS])
                close_prev = float(ticker["Close"].iloc[-1 - RSI_LOOKBACK_BARS])

                # B) Hidden bearish divergence: price up, RSI down — reject first
                if candle_close > close_prev and rsi_val < rsi_prev:
                    logger.info(
                        f"  ❌ Hidden bearish RSI divergence "
                        f"(Price: ₹{close_prev:.2f}→₹{candle_close:.2f} ↑, "
                        f"RSI: {rsi_prev:.1f}→{rsi_val:.1f} ↓): {symbol}"
                    )
                    rejection_counts["rsi_hidden_divergence"] += 1
                    continue

                # A) RSI direction: must be rising
                if rsi_val <= rsi_prev:
                    logger.info(
                        f"  ❌ RSI not rising ({rsi_val:.1f} ≤ {rsi_prev:.1f} "
                        f"from {RSI_LOOKBACK_BARS} days ago): {symbol}"
                    )
                    rejection_counts["rsi_not_rising"] += 1
                    continue

                logger.info(
                    f"  ✔ RSI rising, no divergence: "
                    f"{rsi_prev:.1f}→{rsi_val:.1f} | "
                    f"Price: ₹{close_prev:.2f}→₹{candle_close:.2f}"
                )

            # ── FILTER 10: EMA20 ──────────────────────────────────────────────────────
            if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")):
                ema20_val = float(latest["EMA20"])
                if candle_close < ema20_val:
                    logger.info(
                        f"  ❌ Below EMA20 (C={candle_close:.2f} < EMA20={ema20_val:.2f}): {symbol}"
                    )
                    rejection_counts["below_ema20"] += 1
                    continue

            # ── FILTER 11: SMA50 ──────────────────────────────────────────────────────
            if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")):
                sma50_val = float(latest["SMA50"])
                if candle_close < sma50_val:
                    logger.info(
                        f"  ❌ Below SMA50 (C={candle_close:.2f} < SMA50={sma50_val:.2f}): {symbol}"
                    )
                    rejection_counts["below_sma50"] += 1
                    continue

            # ── FILTER 12: GOLDEN CROSS ───────────────────────────────────────────────
            if (
                "SMA50"  in ticker.columns and "SMA200" in ticker.columns and
                not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200"))
            ):
                sma50_val  = float(latest["SMA50"])
                sma200_val = float(latest["SMA200"])
                if sma50_val < sma200_val:
                    logger.info(
                        f"  ❌ No golden cross (SMA50={sma50_val:.2f} < SMA200={sma200_val:.2f}): {symbol}"
                    )
                    rejection_counts["no_golden_cross"] += 1
                    continue

            # ── FILTER 13: MACD ───────────────────────────────────────────────────────
            if (
                "MACD" in ticker.columns and "MACD_SIGNAL" in ticker.columns and
                not pd.isna(latest.get("MACD")) and not pd.isna(latest.get("MACD_SIGNAL"))
            ):
                macd_val = float(latest["MACD"])
                macd_sig = float(latest["MACD_SIGNAL"])
                if macd_val < macd_sig:
                    logger.info(
                        f"  ❌ MACD bearish (MACD={macd_val:.4f} < Signal={macd_sig:.4f}): {symbol}"
                    )
                    rejection_counts["macd_bearish"] += 1
                    continue
                logger.info(f"  ✔ MACD bullish: {macd_val:.4f} > {macd_sig:.4f}")

            # ── FILTER 14: 52-WEEK HIGH PROXIMITY ────────────────────────────────────
            if "HIGH_52W" in ticker.columns and not pd.isna(latest.get("HIGH_52W")):
                high_52w = float(latest["HIGH_52W"])
                if high_52w > 0:
                    pct_from_high = (high_52w - candle_close) / high_52w * 100
                    if pct_from_high > MAX_DISTANCE_FROM_52W_HIGH_PCT:
                        logger.info(
                            f"  ❌ Too far from 52W high "
                            f"({pct_from_high:.1f}% below ₹{high_52w:.2f}, max {MAX_DISTANCE_FROM_52W_HIGH_PCT}%): {symbol}"
                        )
                        rejection_counts["far_from_52w_high"] += 1
                        continue
                    logger.info(f"  ✔ Near 52W high: {pct_from_high:.1f}% below ₹{high_52w:.2f}")

            # ── FILTER 15: ATR-ADJUSTED MOVE CAP ─────────────────────────────────────
            atr_val = compute_atr(ticker, period=14)

            if len(ticker) >= 2:
                if atr_val is not None and atr_val > 0:
                    prev_close      = float(ticker["Close"].iloc[-2])
                    single_move_abs = abs(candle_close - prev_close)
                    atr_move_limit  = ATR_MOVE_MULTIPLIER * atr_val
                    single_move_pct = single_move_abs / prev_close * 100 if prev_close > 0 else 0

                    if single_move_abs > atr_move_limit:
                        logger.info(
                            f"  ❌ Exhaustion move ({single_move_pct:.1f}% / "
                            f"₹{single_move_abs:.2f} > {ATR_MOVE_MULTIPLIER}× ATR={atr_val:.2f}): {symbol}"
                        )
                        rejection_counts["exhaustion_move"] += 1
                        continue

                    logger.info(
                        f"  ✔ Move within ATR limit: ₹{single_move_abs:.2f} vs "
                        f"limit ₹{atr_move_limit:.2f} ({ATR_MOVE_MULTIPLIER}× ATR)"
                    )
                else:
                    logger.warning(f"  ⚠️ ATR unavailable, skipping move cap filter: {symbol}")
                    atr_val = None

            # ── ALL FILTERS PASSED — LOG SUMMARY ─────────────────────────────────────
            logger.info(
                f"  ✔ Daily candle OK | Body={body_ratio:.0%} | ClosePos={close_position:.0%} "
                f"| Wick={wick_ratio:.0%} | Vol={volume_ratio:.2f}x | RSI={rsi_val:.1f} "
                f"| Price=₹{candle_close:.2f}"
            )

            # ── DELIVERY DATA LOOKUP ──────────────────────────────────────────────────
            delivery_pct = delivery_map.get(symbol, None)

            if delivery_pct is not None:
                logger.info(
                    f"  📦 Delivery: {delivery_pct:.1f}% | "
                    f"{'High conviction' if delivery_pct >= 60 else 'Solid' if delivery_pct >= 40 else 'Moderate' if delivery_pct >= 25 else 'Low — intraday churn'}"
                )
            else:
                logger.info(f"  📦 Delivery: N/A (not in bhavcopy or bhavcopy unavailable)")

            # ── DEDUP KEY ─────────────────────────────────────────────────────────────
            # Includes today_str so the same setup can re-alert on a future day,
            # but NOT the attempt number — prevents multi-alert within one day.
            breakout_type = ", ".join(signals)
            dedup_key     = f"{breakout_type}|{today_str}|EOD"

            # ── SCORE ─────────────────────────────────────────────────────────────────
            score = calculate_score(
                category=category,
                breakout_count=len(signals),
                rsi=rsi_val,
                volume_ratio=volume_ratio,
                breakout_signals=signals,
                ticker=ticker,
                latest=latest,
                symbol=symbol,
                timeframe="1d",
                atr_val=atr_val,
                delivery_pct=delivery_pct,
            )

            logger.info(f"  📊 Score={score} | Threshold={MIN_SCORE}")

            if score < MIN_SCORE:
                logger.info(f"  ❌ Score too low ({score} < {MIN_SCORE}): {symbol}")
                rejection_counts["low_score"] += 1
                continue

            # ── DEDUP CHECK ───────────────────────────────────────────────────────────
            saved = save_alert_if_new(
                symbol,
                dedup_key,
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            )

            if not saved:
                logger.info(f"  ⚠️ Duplicate suppressed (already alerted today): {symbol}")
                rejection_counts["duplicate"] += 1
                continue

            # ── BUILD ALERT PAYLOAD ───────────────────────────────────────────────────
            above_sma50 = (
                bool(candle_close >= float(latest["SMA50"]))
                if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50"))
                else None
            )
            golden_cross = (
                bool(float(latest["SMA50"]) >= float(latest["SMA200"]))
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
                "price":            round(candle_close, 2),
                "open":             round(candle_open, 2),
                "day_high":         round(candle_high, 2),
                "day_low":          round(candle_low, 2),
                "rsi":              round(rsi_val, 1),
                "volume_ratio":     round(volume_ratio, 2),
                "body_ratio":       round(body_ratio * 100),
                "close_position":   round(close_position * 100),
                "score":            score,
                "delivery_pct":     round(delivery_pct, 1) if delivery_pct is not None else None,
                "above_ema20":      bool(candle_close >= float(latest["EMA20"])) if "EMA20" in ticker.columns else None,
                "above_sma50":      above_sma50,
                "golden_cross":     golden_cross,
            })
            total_alerts += 1

            logger.info(
                f"  ✅ ALERT COLLECTED | {symbol} | Score={score} | "
                f"Vol={volume_ratio:.2f}x | RSI={rsi_val:.1f} | "
                f"Delivery={f'{delivery_pct:.1f}%' if delivery_pct is not None else 'N/A'} | "
                f"Signals={len(signals)}"
            )

        except Exception:
            logger.exception(f"❌ UNHANDLED ERROR processing {symbol}")

    # ── SECTOR CONFLUENCE PASS ────────────────────────────────────────────────────────
    hot_sectors = [
        cat for cat, alerts in alerts_by_category.items()
        if len(alerts) >= SECTOR_CONFLUENCE_THRESHOLD
    ]

    if hot_sectors:
        logger.info(
            f"🔥 Sector confluence detected in {len(hot_sectors)} categories: "
            f"{', '.join(hot_sectors)}"
        )
        for cat in hot_sectors:
            for alert in alerts_by_category[cat]:
                old_score      = alert["score"]
                alert["score"] = old_score + SECTOR_CONFLUENCE_BONUS
                logger.info(
                    f"  📈 Confluence boost [{cat}] {alert['symbol']}: "
                    f"score {old_score} → {alert['score']} (+{SECTOR_CONFLUENCE_BONUS})"
                )

    # ── SEND ALERTS ──────────────────────────────────────────────────────────────────
    scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    if total_alerts == 0:
        logger.info(
            f"📭 No new EOD alerts | Attempt {scan_attempt_num}/{MAX_SCAN_ATTEMPTS} | "
            f"(Stocks already alerted today will not re-fire)"
        )
    else:
        for cat in sorted(alerts_by_category.keys()):
            cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)
            chunks     = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

            for chunk_num, chunk in enumerate(chunks, start=1):
                msg = build_message("EOD", cat, chunk, chunk_num, len(chunks), scan_time)
                send_telegram_message(msg, scan_type="EOD")
                logger.info(
                    f"📨 Telegram sent | Category={cat} | Chunk={chunk_num}/{len(chunks)} | Stocks={len(chunk)}"
                )

    # ── SCAN SUMMARY ──────────────────────────────────────────────────────────────────
    duration       = (datetime.now(IST) - scan_start).total_seconds()
    last_scan_date = today_str
    last_scan_time = datetime.now(IST)

    remaining_attempts = MAX_SCAN_ATTEMPTS - scan_attempt_num

    logger.info("=" * 80)
    logger.info(
        f"✅ EOD SCAN ATTEMPT {scan_attempt_num}/{MAX_SCAN_ATTEMPTS} COMPLETE | "
        f"{round(duration, 2)}s | "
        f"NewAlerts={total_alerts}/{len(watchlist)} | "
        f"DeliveryCoverage={len(delivery_map):,} symbols | "
        f"RemainingAttempts={remaining_attempts}"
    )
    logger.info("── Rejection breakdown ──────────────────────────────────────────────────")
    for reason, count in rejection_counts.items():
        if count > 0:
            logger.info(f"   {reason:<28}: {count}")

    if remaining_attempts > 0:
        next_attempt_time = (last_scan_time + timedelta(minutes=SCAN_INTERVAL_MINUTES)).strftime("%H:%M:%S")
        logger.info(
            f"⏭ Next attempt in {SCAN_INTERVAL_MINUTES}m (~{next_attempt_time} IST) | "
            f"Dedup prevents re-alerting stocks already sent"
        )
    else:
        sleep_secs = seconds_until_eod()
        logger.info(
            f"💤 All {MAX_SCAN_ATTEMPTS} attempts done | "
            f"Next window: tomorrow 18:00 IST | "
            f"Sleeping {sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
        )
    logger.info("=" * 80)

    # Short sleep before re-entering the loop — the interval gate above handles pacing
    time.sleep(60)

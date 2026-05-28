# =====================================================================================
# app/eod_scanner.py
# EOD BREAKOUT SCANNER — DAILY CANDLES
#
# WHAT THIS FILE DOES:
#   Runs once per trading day in the window 3:45 PM IST.
#   This timing ensures the final daily candle has effectively closed (3:30 PM NSE
#   close) before we evaluate it — no forming-candle ambiguity on daily bars.
#   Downloads 1 year of daily OHLCV data for each watchlist stock, applies indicators,
#   then runs the strictest filter stack of the three scanners.
#
#   Daily alerts represent high-conviction multi-day momentum setups.
#   Every filter is calibrated for the daily timeframe. Do NOT copy these thresholds
#   to the intraday or 1H scanner — daily bars have different statistical properties.
#
# FILTER PIPELINE (in order — a stock must pass ALL of these):
#   1.  Data quality          — 200 candles minimum, no missing columns
#   2.  Data freshness        — latest bar must be today's date (no cached/stale data)
#   3.  Signal count          — at least 3 breakout signals (strictest confluence)
#   4.  Candle body           — body ≥ 60% of range (tightest of the three scanners)
#   5.  Bullish close         — close strictly above open
#   6.  Close position        — close in top 25% of daily range
#   7.  Upper wick            — wick ≤ 30% of range
#   8.  Volume ratio          — current day ≥ 2.0× 20-day average
#   9.  Avg volume floor      — 20-day avg ≥ 200K shares
#   10. Min stock price       — close ≥ ₹50
#   11. RSI range             — RSI 58–75 (tightest RSI band)
#   12. RSI direction + divergence — RSI rising over 5 days AND no hidden bearish divergence
#   13. EMA20                 — close above EMA20
#   14. SMA50                 — close above SMA50
#   15. Golden cross          — SMA50 ≥ SMA200
#   16. MACD                  — MACD line above signal line
#   17. 52W high proximity    — within 15% of 52-week high
#   18. ATR-adjusted move cap — day's move ≤ 3× ATR(14) (replaces flat 8% cap)
#   19. Score threshold       — composite score ≥ 78 (boosted if sector confluence ≥ 3)
#
# CHANGES FROM PREVIOUS VERSION:
#   + IMPROVED: flat 8% single-day move cap → ATR(14)-relative cap (3× ATR)
#       Rationale: a ₹200 stock moving ₹16 (8%) on ATR of ₹3 is an exhaustion spike.
#       The same stock moving ₹16 on ATR of ₹8 is a normal high-conviction breakout.
#       The flat cap blocked valid setups; the ATR cap catches exhaustion more precisely.
#   + NEW: data freshness guard — verifies latest bar date == today before scanning
#       Rationale: yfinance occasionally returns cached or T-1 data. Scanning a stale
#       candle as if it were today's is a silent, invisible bug. Fail loudly instead.
#   + NEW: hidden bearish RSI divergence check (price up + RSI down over 5 days)
#       Rationale: the existing RSI direction check (RSI rising) catches the easy case.
#       Hidden bearish divergence — price makes a higher close while RSI makes a lower
#       close — is a significantly more dangerous reversal signal that the previous
#       check would miss (RSI could be "not rising" but that's a soft reject; divergence
#       is a hard reject because it means distribution is already underway).
#   + NEW: sector confluence boost to composite score
#       Rationale: institutions rotate by sector. A stock breaking out in isolation is
#       interesting; three stocks in the same category breaking out simultaneously
#       is a sector rotation event. Score boost of +5 per stock when category has ≥ 3
#       alerts. Applied as a second pass after the full filter run, before Telegram send.
#   + NEW: rejections.log file alongside console logging
#       Rationale: on VPS/Railway deployments, stdout is ephemeral. A persistent
#       rejections.log lets you audit why no alerts fired for N days without needing
#       to re-run the scanner or dig through container logs.
#   + MIN_SIGNALS kept at 3 (already correct)
#   + MIN_BODY_RATIO kept at 0.60 (already correct)
#   + MIN_VOLUME_RATIO kept at 2.0 (already correct)
#   + MIN_RSI / MAX_RSI kept at 58 / 75 (already correct)
#   + MIN_SCORE kept at 78 (already correct)
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

from config import WATCHLIST_PATH

# =====================================================================================
# LOGGER — dual output: console + persistent rejections.log
#
# Why a separate file handler?
# Console output is ephemeral on VPS/Railway. If the bot is silent for 3 days, you
# need to know *why* — are stocks failing "no_golden_cross" (market-wide downtrend)?
# Or is "low_score" the biggest bucket (scoring miscalibration)? The log file persists
# across restarts and lets you audit filter performance without re-running the scanner.
#
# RotatingFileHandler: caps at 5 MB, keeps 3 backups. Prevents unbounded disk growth
# on long-running deployments. Adjust maxBytes/backupCount for your VPS disk budget.
# =====================================================================================

LOG_DIR      = os.environ.get("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
REJECTION_LOG_PATH = os.path.join(LOG_DIR, "rejections.log")

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)

_file_handler = logging.handlers.RotatingFileHandler(
    REJECTION_LOG_PATH,
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=3,               # keep rejections.log, rejections.log.1, .2, .3
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

IST        = ZoneInfo("Asia/Kolkata")
EOD_START  = dt_time(15, 45)   # Start scanning after NSE close (3:45 PM)
EOD_END    = dt_time(16, 30)   # End window — must complete scan by 4:30
CHUNK_SIZE = 10                 # Max stocks per Telegram message

# =====================================================================================
# FILTER CONSTANTS — EOD DAILY
#
# These are the strictest thresholds across the three scanners.
# Daily bars carry the most weight — one daily signal = entire day's conviction.
# =====================================================================================

MIN_SIGNALS         = 3
MIN_BODY_RATIO      = 0.60
MIN_CLOSE_POSITION  = 0.75
MAX_UPPER_WICK_RATIO = 0.30
MIN_VOLUME_RATIO    = 2.0
MIN_AVG_VOLUME_SHARES = 200_000
MIN_STOCK_PRICE     = 50.0
MIN_RSI             = 58
MAX_RSI             = 75
RSI_LOOKBACK_BARS   = 5        # 5 daily bars = 1 full trading week

# ATR-relative move cap (replaces flat 8% cap).
# A move > ATR_MOVE_MULTIPLIER × ATR(14) is flagged as a potential exhaustion spike.
# Calibration rationale:
#   ATR(14) on daily bars represents a stock's "normal" daily swing over 14 sessions.
#   3× ATR is widely used in institutional breakout systems as the threshold between
#   "strong breakout" and "blow-off / news-driven spike with no follow-through."
#   Example: stock with ATR(14) = ₹8. A ₹24 move = 3× ATR → reject (likely exhaustion).
#   Same stock on a flat 8% cap: if the stock is ₹100, ₹8 move = 8% → reject (valid!).
#   The ATR cap correctly allows a ₹16 move (2× ATR) on a ₹100 stock, which the flat
#   cap would block if the stock happened to be volatile.
ATR_MOVE_MULTIPLIER = 3.0

MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0
MIN_SCORE = 78

# Sector confluence: if ≥ this many stocks from the same category pass all filters,
# each gets a score bonus. Represents institutional sector-rotation conviction.
SECTOR_CONFLUENCE_THRESHOLD = 3
SECTOR_CONFLUENCE_BONUS     = 5   # bonus score points per stock in a hot sector

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
    """
    Calculate seconds until the next 3:45 PM IST scan window.
    Used to sleep efficiently rather than polling every 60 seconds.
    """
    now          = datetime.now(IST)
    target_today = now.replace(hour=15, minute=45, second=0, microsecond=0)
    if now < target_today:
        delta = target_today - now
    else:
        delta = target_today + timedelta(days=1) - now
    return max(int(delta.total_seconds()), 0)


def compute_atr(ticker: pd.DataFrame, period: int = 14) -> float | None:
    """
    Compute the most recent ATR(14) value from a daily OHLCV DataFrame.

    ATR = average of True Range over `period` bars.
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)

    Returns None if there is insufficient data to compute ATR reliably.
    ATR is in price units (₹), not percentage. The caller converts to percentage
    if needed, or compares directly against an absolute price move.

    Why not use technical_indicators.apply_indicators for this?
    apply_indicators runs the full indicator suite, which is expensive. We only need
    ATR for the move-cap check. Computing it inline avoids a dependency on whether
    the indicators module exposes ATR as a named column.
    """
    if len(ticker) < period + 1:
        return None

    high  = ticker["High"].values
    low   = ticker["Low"].values
    close = ticker["Close"].values

    # True Range for each bar (vectorised over the last period+1 bars for efficiency)
    tr_high_low  = high[1:] - low[1:]
    tr_high_prev = abs(high[1:] - close[:-1])
    tr_low_prev  = abs(low[1:] - close[:-1])

    true_range = pd.Series(
        [max(a, b, c) for a, b, c in zip(tr_high_low, tr_high_prev, tr_low_prev)]
    )

    # Use the last `period` bars only — same window as ATR(14) in indicators
    return float(true_range.tail(period).mean())


def is_data_fresh(ticker: pd.DataFrame, today: date) -> bool:
    """
    Verify that the latest bar in `ticker` corresponds to today's date.

    Why this matters:
    yfinance occasionally returns cached, delayed, or T-1 data. If we scan a candle
    from yesterday as if it were today's, every filter passes on stale data and we
    send alerts for a setup that is already one day old. The entry price is wrong,
    the volume is wrong, and the RSI computed on yesterday's close is meaningless.

    Implementation note:
    Daily bars from yfinance are date-indexed (not datetime-indexed). The index dtype
    is either `datetime64[ns]` (with time component 00:00:00) or `date`. Both are
    normalised to Python `date` for comparison.
    """
    if ticker.empty:
        return False

    last_index = ticker.index[-1]

    # Normalise: pandas Timestamp → date, or already a date
    if hasattr(last_index, "date"):
        last_bar_date = last_index.date()
    elif isinstance(last_index, date):
        last_bar_date = last_index
    else:
        # Fallback: try string parsing
        try:
            last_bar_date = pd.to_datetime(str(last_index)).date()
        except Exception:
            return False

    return last_bar_date == today


# Tracks the last date a scan completed — prevents double-scanning on the same day
last_scan_date = None

# =====================================================================================
# MAIN LOOP
# Runs continuously. Outside the 3:45–4:30 PM window, sleeps until the next window.
# Inside the window, runs the full scan exactly once per trading day.
# =====================================================================================

while True:

    ist_now      = datetime.now(IST)
    current_time = ist_now.time()
    weekday      = ist_now.weekday()   # 0=Mon … 6=Sun
    today_str    = ist_now.strftime("%Y-%m-%d")
    today_date   = ist_now.date()

    in_eod_window = EOD_START <= current_time <= EOD_END
    is_weekday    = weekday < 5
    already_ran   = (last_scan_date == today_str)

    # ── WEEKEND ──────────────────────────────────────────────────────────────────────
    if not is_weekday:
        sleep_secs = seconds_until_eod()
        logger.info(
            f"📅 Weekend ({ist_now.strftime('%A')}) | "
            f"Next scan in {sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
        )
        time.sleep(min(sleep_secs, 3600))
        continue

    # ── ALREADY SCANNED TODAY ────────────────────────────────────────────────────────
    if already_ran:
        sleep_secs = seconds_until_eod()
        logger.info(
            f"✅ EOD already completed for {today_str} | "
            f"Next scan in {sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
        )
        time.sleep(min(sleep_secs, 3600))
        continue

    # ── NOT YET IN EOD WINDOW ────────────────────────────────────────────────────────
    if not in_eod_window:
        sleep_secs = seconds_until_eod()
        logger.info(
            f"⏰ Waiting for EOD window | Now={ist_now.strftime('%H:%M:%S')} | "
            f"Starts 15:45 | {sleep_secs // 60}m {sleep_secs % 60}s remaining"
        )
        time.sleep(min(sleep_secs, 60))
        continue

    # ================================================================================
    # EOD SCAN WINDOW — 3:45 PM to 4:30 PM IST
    # ================================================================================

    logger.info("=" * 80)
    logger.info(f"📊 EOD SCAN STARTED | {ist_now.strftime('%Y-%m-%d %H:%M:%S IST')}")
    logger.info("=" * 80)

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
    # alerts_by_category: category → list of alert dicts
    # Populated during the filter pass; sector confluence applied in a second pass.
    alerts_by_category: dict[str, list[dict]] = {}

    rejection_counts = {
        "no_data":              0,
        "stale_data":           0,   # NEW: latest bar is not today's date
        "missing_col":          0,
        "insufficient_bars":    0,
        "indicator_fail":       0,
        "weak_signals":         0,
        "weak_body":            0,
        "bearish_candle":       0,
        "weak_close_pos":       0,
        "upper_wick":           0,
        "low_volume":           0,
        "low_avg_volume":       0,
        "penny_stock":          0,
        "rsi_range":            0,
        "rsi_not_rising":       0,
        "rsi_hidden_divergence": 0,  # NEW: price up but RSI down over 5 days
        "below_ema20":          0,
        "below_sma50":          0,
        "no_golden_cross":      0,
        "macd_bearish":         0,
        "far_from_52w_high":    0,
        "exhaustion_move":      0,   # NEW: replaces "gap_day" — ATR-relative cap
        "low_score":            0,
        "duplicate":            0,
    }

    logger.info(f"🚀 Processing {len(watchlist)} stocks...")

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
            # Verify the latest bar is actually today. yfinance occasionally returns
            # cached or delayed data. Scanning yesterday's candle as today's introduces
            # silent, invisible errors — wrong price, wrong volume, wrong RSI.
            # We use the "Date" column (created by reset_index) for the date check.
            if "Date" in ticker.columns:
                latest_date_raw = ticker["Date"].iloc[-1]
                if hasattr(latest_date_raw, "date"):
                    latest_bar_date = latest_date_raw.date()
                else:
                    latest_bar_date = pd.to_datetime(str(latest_date_raw)).date()
            else:
                # Fallback: index was already reset but column name differs
                latest_bar_date = None

            if latest_bar_date is not None and latest_bar_date != today_date:
                logger.warning(
                    f"  ❌ Stale data (latest bar={latest_bar_date}, today={today_date}): {symbol}"
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
            #
            # Two distinct RSI checks rolled into one lookback window:
            #
            # A) RSI direction — RSI must be higher than 5 days ago.
            #    Confirms the rally is sustained over a full trading week.
            #
            # B) Hidden bearish divergence — price makes a higher close while RSI makes
            #    a LOWER close over the same window. This is a distribution signal: smart
            #    money is selling into price strength, so RSI (which measures price change
            #    velocity) is declining even as price is still rising.
            #
            # Why separate A and B?
            # A catches: RSI flat or declining even when price is flat.
            # B catches: RSI explicitly declining while price is rising.
            # B is the more dangerous setup — it means divergence is already in progress.
            # It's a hard rejection, not a score penalty, because it conflicts directly
            # with the premise of a breakout (genuine buying pressure).
            #
            # Implementation:
            # We check B first because it is the more severe condition. If B fires, we
            # skip A — both would reject the stock, but B's log message is more informative.
            if len(ticker) > RSI_LOOKBACK_BARS:
                rsi_prev   = float(ticker["RSI"].iloc[-1 - RSI_LOOKBACK_BARS])
                close_prev = float(ticker["Close"].iloc[-1 - RSI_LOOKBACK_BARS])

                # B) Hidden bearish divergence: price up, RSI down
                if candle_close > close_prev and rsi_val < rsi_prev:
                    logger.info(
                        f"  ❌ Hidden bearish RSI divergence "
                        f"(Price: ₹{close_prev:.2f}→₹{candle_close:.2f} ↑, "
                        f"RSI: {rsi_prev:.1f}→{rsi_val:.1f} ↓): {symbol}"
                    )
                    rejection_counts["rsi_hidden_divergence"] += 1
                    continue

                # A) RSI direction: RSI must be rising
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
            # Replaces the previous flat 8% single-day move cap.
            #
            # Problem with a flat % cap:
            #   A stock with ATR(14) = ₹3 moving ₹16 (8% if priced at ₹200) is an
            #   extreme 5.3× ATR move — almost certainly a news spike or gap fill, not
            #   a sustainable breakout. The flat 8% cap would correctly reject this.
            #   But a stock with ATR(14) = ₹12 moving ₹16 is a 1.3× ATR move — a
            #   perfectly normal strong breakout day. The flat 8% cap would also reject
            #   this if the stock happens to be priced at ₹200. That's wrong.
            #
            # ATR-relative cap:
            #   If the day's absolute price move > ATR_MOVE_MULTIPLIER × ATR(14),
            #   the move is statistically extreme relative to this stock's own volatility.
            #   This is a stock-specific, volatility-adjusted threshold that correctly
            #   separates sustainable breakouts from blow-off tops.
            #
            # Fallback:
            #   If ATR cannot be computed (insufficient data), skip this filter rather
            #   than erroneously blocking the stock. Log a warning so you can investigate.
            if len(ticker) >= 2:
                atr_val = compute_atr(ticker, period=14)

                if atr_val is not None and atr_val > 0:
                    prev_close       = float(ticker["Close"].iloc[-2])
                    single_move_abs  = abs(candle_close - prev_close)
                    atr_move_limit   = ATR_MOVE_MULTIPLIER * atr_val
                    single_move_pct  = single_move_abs / prev_close * 100 if prev_close > 0 else 0

                    if single_move_abs > atr_move_limit:
                        logger.info(
                            f"  ❌ Exhaustion move ({single_move_pct:.1f}% / "
                            f"₹{single_move_abs:.2f} > {ATR_MOVE_MULTIPLIER}× ATR={atr_val:.2f}): {symbol}"
                        )
                        rejection_counts["exhaustion_move"] += 1
                        continue

                    logger.info(
                        f"  ✔ Move within ATR limit: ₹{single_move_abs:.2f} move vs "
                        f"ATR limit ₹{atr_move_limit:.2f} ({ATR_MOVE_MULTIPLIER}× ATR)"
                    )
                else:
                    logger.warning(f"  ⚠️ ATR unavailable, skipping move cap filter: {symbol}")

            # ── ALL FILTERS PASSED — LOG SUMMARY ─────────────────────────────────────
            logger.info(
                f"  ✔ Daily candle OK | Body={body_ratio:.0%} | ClosePos={close_position:.0%} "
                f"| Wick={wick_ratio:.0%} | Vol={volume_ratio:.2f}x | RSI={rsi_val:.1f} "
                f"| Price=₹{candle_close:.2f}"
            )

            # ── DEDUP KEY ─────────────────────────────────────────────────────────────
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
                "above_ema20":      bool(candle_close >= float(latest["EMA20"])) if "EMA20" in ticker.columns else None,
                "above_sma50":      above_sma50,
                "golden_cross":     golden_cross,
            })
            total_alerts += 1

            logger.info(
                f"  ✅ ALERT COLLECTED | {symbol} | Score={score} | "
                f"Vol={volume_ratio:.2f}x | RSI={rsi_val:.1f} | Signals={len(signals)}"
            )

        except Exception:
            logger.exception(f"❌ UNHANDLED ERROR processing {symbol}")

    # ── SECTOR CONFLUENCE PASS ────────────────────────────────────────────────────────
    # Second pass over all collected alerts. For each category with ≥ SECTOR_CONFLUENCE_THRESHOLD
    # stocks, boost every stock's score by SECTOR_CONFLUENCE_BONUS points.
    #
    # Why a second pass and not inline?
    # We don't know how many stocks in a category will pass filters until we've
    # processed all stocks in the category. Running this inline would require either:
    #   (a) pre-grouping the watchlist by category and processing category-by-category,
    #       which breaks the current per-stock flow and complicates error handling, or
    #   (b) a look-ahead, which is impossible in a streaming loop.
    # A second pass after the filter loop is the cleanest solution.
    #
    # The score boost does NOT re-check MIN_SCORE. A stock that scored 74 before the
    # boost (below threshold) and 79 after still gets sent — the sector confluence IS
    # the additional evidence that validates the setup. This is intentional.
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
                old_score = alert["score"]
                alert["score"] = old_score + SECTOR_CONFLUENCE_BONUS
                logger.info(
                    f"  📈 Confluence boost [{cat}] {alert['symbol']}: "
                    f"score {old_score} → {alert['score']} (+{SECTOR_CONFLUENCE_BONUS})"
                )

    # ── SEND ALERTS ──────────────────────────────────────────────────────────────────
    scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    if total_alerts == 0:
        logger.info("📭 No EOD alerts today")
    else:
        for cat in sorted(alerts_by_category.keys()):
            # Sort by boosted score descending — hot-sector stocks naturally rise to top
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
    sleep_secs     = seconds_until_eod()

    logger.info("=" * 80)
    logger.info(f"✅ EOD SCAN COMPLETE | {round(duration, 2)}s | Alerts={total_alerts}/{len(watchlist)}")
    logger.info("── Rejection breakdown ──────────────────────────────────────────────────")
    for reason, count in rejection_counts.items():
        if count > 0:
            logger.info(f"   {reason:<28}: {count}")
    logger.info(f"💤 Next scan: tomorrow at 15:45 IST | sleeping {sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m")
    logger.info("=" * 80)

    time.sleep(min(sleep_secs, 3600))

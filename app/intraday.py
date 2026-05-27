# =====================================================================================
# app/intraday.py
# EARLY MOMENTUM SCANNER — 15M BARS
#
# WHAT THIS FILE DOES:
#   Runs every 5 minutes during market hours (9:32 AM – 3:30 PM IST).
#   For each stock in the watchlist, downloads the last 5 days of 15-minute OHLCV
#   data, applies technical indicators, runs breakout detection, then passes every
#   candidate through a layered filter stack before scoring and alerting via Telegram.
#
# FILTER PIPELINE (in order — a stock must pass ALL of these):
#   1.  Data quality     — enough candles, no missing columns, no forming bar
#   2.  Signal count     — at least 2 breakout signals (confluence required)
#   3.  Candle body      — body ≥ 55% of range (no doji or spinning tops)
#   4.  Bullish close    — close must be strictly above open
#   5.  Close position   — close in top 30% of bar range (buyers held control)
#   6.  Upper wick       — wick ≤ 25% of range (no rejection candles)
#   7.  Volume ratio     — current bar ≥ 1.8× 20-bar average
#   8.  Avg volume floor — 20-bar average ≥ 100K shares (liquidity gate)
#   9.  RSI range        — RSI between 55 and 75 (momentum sweet spot)
#   10. RSI direction    — RSI now > RSI 3 bars ago (momentum must be rising)
#   11. EMA20            — close above EMA20 (short-term trend is up)
#   12. SMA50            — close above SMA50 (medium-term trend is up)
#   13. Golden cross     — SMA50 ≥ SMA200 (long-term structure is bullish)
#   14. MACD             — MACD line above signal line (trend momentum confirmed)
#   15. Single-bar move  — bar move ≤ 4% from previous close (no gap chases)
#   16. Score threshold  — composite score ≥ 72 (hard quality gate)
#
# CHANGES FROM PREVIOUS VERSION:
#   + MIN_SIGNALS raised from 1 → 2 (single-signal setups were too noisy)
#   + MIN_VOLUME_RATIO changed from 1.5 → 1.8 (better institutional filter)
#   + MIN_RSI changed from 52 → 55 (avoid pre-momentum base-building phase)
#   + MAX_RSI changed from 78 → 75 (avoid overbought chase setups)
#   + MIN_SCORE changed from 75 → 72 (balanced: strict filters already cut noise)
#   + NEW: close position check (top 30% of bar)
#   + NEW: upper wick ratio check (≤25% of range)
#   + NEW: RSI direction check (RSI must be rising over 3 bars)
#   + NEW: MACD bullish confirmation
#   + NEW: single-bar move cap (≤4%)
#   + NEW: avg volume floor (100K shares minimum)
#   + IMPROVED: all filter rejections now log the exact reason + value
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
# Logs to stdout with timestamp, level, and message.
# Set level=DEBUG to see every per-bar decision during development.
# =====================================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

IST        = ZoneInfo("Asia/Kolkata")
CHUNK_SIZE = 10   # Max stocks per Telegram message (avoids message truncation)

# =====================================================================================
# FILTER CONSTANTS — 15M INTRADAY
#
# These values are tuned for 15-minute bars on NSE stocks.
# Do NOT share these constants with the EOD or 1H scanner —
# each timeframe has different volatility characteristics.
# =====================================================================================

# Minimum number of independent breakout signals required.
# 1 signal = noise. 2 signals = confluence. 3+ = conviction.
MIN_SIGNALS = 2

# Candle body must be at least 55% of the full High–Low range.
# Eliminates doji, spinning tops, and high-wick indecision bars.
MIN_BODY_RATIO = 0.55

# Close must be in the top 30% of the bar's range.
# Formula: (Close - Low) / (High - Low) >= 0.70
# Ensures buyers held control into the bar close — not a distribution candle.
MIN_CLOSE_POSITION = 0.70

# Upper wick must be ≤ 25% of the full candle range.
# Upper wick = sellers pushing price back down from the high.
# >25% upper wick = meaningful rejection — skip this bar.
MAX_UPPER_WICK_RATIO = 0.25

# Current bar volume must be at least 1.8× the 20-bar rolling average.
# 1.8× on 15m isolates genuine institutional participation from routine spikes
# that naturally occur at market open, lunch, and close.
MIN_VOLUME_RATIO = 1.8

# Hard liquidity floor: the 20-bar average volume must be ≥ 100,000 shares.
# Below this, spreads are wide and fills are unreliable.
MIN_AVG_VOLUME_SHARES = 100_000

# RSI must be between 55 and 75 on the 15-minute chart.
# < 55: stock hasn't entered momentum territory yet
# 55–65: early momentum — best reward:risk zone, highest priority
# 65–75: strong momentum — still valid, but size accordingly
# > 75: overbought on 15m — high chance of 1–2 bar mean reversion
MIN_RSI = 55
MAX_RSI = 75

# RSI direction: current RSI must be strictly greater than RSI 3 bars ago.
# A stock at RSI 68 but falling from 74 is decelerating — not a momentum entry.
# A stock at RSI 60 and rising from 54 is accelerating — take it.
RSI_LOOKBACK_BARS = 3

# Maximum allowed single-bar move from the previous close.
# >4% in a single 15m bar = news event, halt resumption, or gap.
# These have already moved — chasing them destroys reward:risk.
MAX_SINGLE_CANDLE_MOVE_PCT = 4.0

# Minimum composite score to generate an alert.
# Score is 0–100, calculated by scoring_engine.py.
# 72+ with the above filters in place = genuinely strong momentum setup.
MIN_SCORE = 72

# =====================================================================================
# INIT
# =====================================================================================

init_db()
cleanup_old_alerts(days=7)   # Remove stale dedup keys older than 7 days
logger.info("✅ Database initialized | Stale alerts cleaned (7-day window)")

# =====================================================================================
# MAIN LOOP
# Runs continuously. Sleeps 5 minutes between scans during market hours.
# Sleeps 5 minutes when outside market hours and re-checks.
# =====================================================================================

while True:

    ist_now      = datetime.now(IST)
    current_time = ist_now.time()
    weekday      = ist_now.weekday()   # 0=Mon … 4=Fri, 5=Sat, 6=Sun

    # Market window: 9:32 AM to 3:30 PM IST, weekdays only.
    # Start at 9:32 (not 9:15) to let the opening auction settle —
    # the first 15 minutes of NSE trading are extremely noisy.
    market_open  = dt_time(9, 32) <= current_time <= dt_time(15, 30)
    weekday_open = weekday < 5

    if not (market_open and weekday_open):
        logger.info(
            f"⏰ Outside market hours | {ist_now.strftime('%H:%M:%S')} "
            f"({'Weekend' if not weekday_open else 'Pre-open or closed'}) | sleeping 5m"
        )
        time.sleep(300)
        continue

    # ── LOAD WATCHLIST ──────────────────────────────────────────────────────────────
    # Watchlist is a parquet file with columns: Stock, Category.
    # If it fails to load, rebuild it automatically from daily_builder.
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
    alerts_by_category = {}

    # Per-scan filter rejection counters — logged at scan end for tuning
    rejection_counts = {
        "no_data":          0,
        "missing_col":      0,
        "forming_candle":   0,
        "insufficient_bars":0,
        "indicator_fail":   0,
        "weak_signals":     0,
        "weak_body":        0,
        "bearish_candle":   0,
        "weak_close_pos":   0,
        "upper_wick":       0,
        "low_volume":       0,
        "low_avg_volume":   0,
        "rsi_range":        0,
        "rsi_not_rising":   0,
        "below_ema20":      0,
        "below_sma50":      0,
        "no_golden_cross":  0,
        "macd_bearish":     0,
        "gap_candle":       0,
        "low_score":        0,
        "duplicate":        0,
    }

    logger.info("=" * 80)
    logger.info(f"⚡ INTRADAY SCAN STARTED | Stocks={len(watchlist)} | {scan_start.strftime('%H:%M:%S IST')}")
    logger.info("=" * 80)

    for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

        symbol = "UNKNOWN"

        try:

            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 [{idx}/{len(watchlist)}] {symbol} | Category={category}")

            # ── DOWNLOAD DATA ───────────────────────────────────────────────────────
            # 5 days of 15m bars = ~100 candles (26 bars/day × 5 days).
            # period="5d" is the minimum for reliable 15m data from yfinance.
            ticker = yf.download(
                f"{symbol}.NS",
                period="5d",
                interval="15m",
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

            # yfinance sometimes returns MultiIndex columns for single-ticker downloads
            if isinstance(ticker.columns, pd.MultiIndex):
                ticker.columns = ticker.columns.get_level_values(0)

            # Remove duplicate columns (can happen with auto_adjust=True)
            ticker = ticker.loc[:, ~ticker.columns.duplicated()]

            # ── COLUMN VALIDATION ───────────────────────────────────────────────────
            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            missing_col   = False

            for col_name in required_cols:
                if col_name not in ticker.columns:
                    logger.warning(f"  ❌ Missing column '{col_name}': {symbol}")
                    missing_col = True
                    break
                # Flatten if yfinance returned a DataFrame inside a column
                if isinstance(ticker[col_name], pd.DataFrame):
                    ticker[col_name] = ticker[col_name].iloc[:, 0]
                ticker[col_name] = pd.Series(ticker[col_name]).astype(float)

            if missing_col:
                rejection_counts["missing_col"] += 1
                continue

            ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

            # ── FORMING CANDLE CHECK ────────────────────────────────────────────────
            # yfinance returns the currently-forming (incomplete) bar as the last row.
            # Signals on an incomplete candle are unreliable — drop the last bar if
            # the 15-minute window for it hasn't closed yet.
            datetime_col = None
            for col in ["Datetime", "Date", "index"]:
                if col in ticker.columns:
                    datetime_col = col
                    break

            if datetime_col is not None:
                try:
                    candle_start = pd.Timestamp(ticker.iloc[-1][datetime_col]).replace(tzinfo=None)
                    candle_end   = candle_start + pd.Timedelta(minutes=15)
                    now_naive    = datetime.now(IST).replace(tzinfo=None)
                    if now_naive < candle_end:
                        logger.warning(
                            f"  ⚠️ Dropping forming candle | closes at {candle_end.strftime('%H:%M')} | {symbol}"
                        )
                        ticker = ticker.iloc[:-1].copy()
                        rejection_counts["forming_candle"] += 1
                except Exception:
                    logger.warning(f"  ⚠️ Candle age check failed (skipping drop): {symbol}")

            # ── MINIMUM CANDLE COUNT ────────────────────────────────────────────────
            # Need at least 50 bars for reliable indicator calculation.
            # 50 bars = ~2 full trading days of 15m data.
            if len(ticker) < 50:
                logger.warning(f"  ❌ Insufficient bars ({len(ticker)} < 50): {symbol}")
                rejection_counts["insufficient_bars"] += 1
                continue

            # ── INDICATORS ─────────────────────────────────────────────────────────
            # Applies RSI, EMA9, EMA20, SMA50, SMA200, MACD, ADX, BB, HIGH_52W.
            # Pass timeframe="15m" so HIGH_52W uses all available bars (not 252-day window).
            ticker = apply_indicators(ticker, timeframe="15m")

            if ticker is None or ticker.empty:
                logger.warning(f"  ❌ Indicator calculation failed: {symbol}")
                rejection_counts["indicator_fail"] += 1
                continue

            # ── BREAKOUT SIGNALS ────────────────────────────────────────────────────
            # detect_breakouts() returns a list of signal name strings.
            # Examples: ["52W High Breakout", "BB Squeeze Breakout", "Volume Surge"]
            signals = detect_breakouts(ticker)

            if len(signals) < MIN_SIGNALS:
                logger.info(f"  ❌ Weak signals ({len(signals)} < {MIN_SIGNALS}): {symbol}")
                rejection_counts["weak_signals"] += 1
                continue

            logger.info(f"  ✔ Signals ({len(signals)}): {', '.join(signals)}")

            latest = ticker.iloc[-1]

            # ── RSI AVAILABILITY ────────────────────────────────────────────────────
            if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
                logger.warning(f"  ❌ RSI unavailable: {symbol}")
                continue

            # ── VOLUME CALCULATIONS ─────────────────────────────────────────────────
            latest_volume = float(latest["Volume"])
            avg_volume    = float(ticker["Volume"].tail(20).mean())   # 20-bar rolling avg

            if avg_volume <= 0:
                logger.warning(f"  ❌ Zero average volume: {symbol}")
                continue

            volume_ratio = latest_volume / avg_volume

            # ── CANDLE GEOMETRY ─────────────────────────────────────────────────────
            candle_high  = float(latest["High"])
            candle_low   = float(latest["Low"])
            candle_open  = float(latest["Open"])
            candle_close = float(latest["Close"])
            candle_range = candle_high - candle_low
            candle_body  = abs(candle_close - candle_open)
            upper_wick   = candle_high - candle_close   # distance from close to high

            if candle_range <= 0:
                logger.warning(f"  ❌ Zero candle range (flat bar): {symbol}")
                continue

            body_ratio     = candle_body / candle_range
            close_position = (candle_close - candle_low) / candle_range
            wick_ratio     = upper_wick / candle_range

            # ── FILTER 1: CANDLE BODY ────────────────────────────────────────────────
            # A strong momentum bar has a large, decisive body.
            # Doji and spinning tops (small bodies) indicate indecision — skip them.
            if body_ratio < MIN_BODY_RATIO:
                logger.info(
                    f"  ❌ Weak body ratio ({body_ratio:.0%} < {MIN_BODY_RATIO:.0%}): {symbol}"
                )
                rejection_counts["weak_body"] += 1
                continue

            # ── FILTER 2: BULLISH CANDLE ─────────────────────────────────────────────
            # Close must be strictly above open — no bearish or neutral candles.
            if candle_close <= candle_open:
                logger.info(f"  ❌ Bearish or doji candle (C={candle_close:.2f} ≤ O={candle_open:.2f}): {symbol}")
                rejection_counts["bearish_candle"] += 1
                continue

            # ── FILTER 3: CLOSE POSITION ─────────────────────────────────────────────
            # Buyers must have held control into the close.
            # A bar that rallied to the high but closed near the middle = distribution.
            if close_position < MIN_CLOSE_POSITION:
                logger.info(
                    f"  ❌ Weak close position ({close_position:.0%} in range, need ≥{MIN_CLOSE_POSITION:.0%}): {symbol}"
                )
                rejection_counts["weak_close_pos"] += 1
                continue

            # ── FILTER 4: UPPER WICK ─────────────────────────────────────────────────
            # A large upper wick = sellers pushed price back hard from the high.
            # This is a rejection candle — momentum is not convincingly bullish.
            if wick_ratio > MAX_UPPER_WICK_RATIO:
                logger.info(
                    f"  ❌ Upper wick rejection ({wick_ratio:.0%} of range, max {MAX_UPPER_WICK_RATIO:.0%}): {symbol}"
                )
                rejection_counts["upper_wick"] += 1
                continue

            # ── FILTER 5: VOLUME RATIO ───────────────────────────────────────────────
            # Momentum requires institutional participation — not just retail activity.
            # Volume must be meaningfully above the stock's recent baseline.
            if volume_ratio < MIN_VOLUME_RATIO:
                logger.info(
                    f"  ❌ Low volume ratio ({volume_ratio:.2f}x < {MIN_VOLUME_RATIO}x avg): {symbol}"
                )
                rejection_counts["low_volume"] += 1
                continue

            # ── FILTER 6: AVERAGE VOLUME FLOOR ───────────────────────────────────────
            # Even if the volume ratio is high, reject illiquid stocks.
            # A 3× surge on 30K average = 90K total — still too thin to trade safely.
            if avg_volume < MIN_AVG_VOLUME_SHARES:
                logger.info(
                    f"  ❌ Illiquid stock (avg vol {avg_volume:,.0f} < {MIN_AVG_VOLUME_SHARES:,}): {symbol}"
                )
                rejection_counts["low_avg_volume"] += 1
                continue

            # ── FILTER 7: RSI RANGE ───────────────────────────────────────────────────
            # RSI must be in the momentum sweet spot — not too weak, not overbought.
            rsi_val = float(latest["RSI"])
            if not (MIN_RSI <= rsi_val <= MAX_RSI):
                logger.info(
                    f"  ❌ RSI out of range ({rsi_val:.1f}, need {MIN_RSI}–{MAX_RSI}): {symbol}"
                )
                rejection_counts["rsi_range"] += 1
                continue

            # ── FILTER 8: RSI DIRECTION ───────────────────────────────────────────────
            # RSI value alone doesn't tell you if momentum is building or fading.
            # RSI must be trending upward over the last N bars.
            if len(ticker) > RSI_LOOKBACK_BARS:
                rsi_prev = float(ticker["RSI"].iloc[-1 - RSI_LOOKBACK_BARS])
                if rsi_val <= rsi_prev:
                    logger.info(
                        f"  ❌ RSI not rising ({rsi_val:.1f} ≤ {rsi_prev:.1f} from {RSI_LOOKBACK_BARS} bars ago): {symbol}"
                    )
                    rejection_counts["rsi_not_rising"] += 1
                    continue
                logger.info(f"  ✔ RSI rising: {rsi_prev:.1f} → {rsi_val:.1f}")

            # ── FILTER 9: EMA20 ────────────────────────────────────────────────────────
            # Price below its own 20-bar EMA is not in short-term uptrend.
            if "EMA20" not in ticker.columns or pd.isna(latest.get("EMA20")):
                logger.warning(f"  ⚠️ EMA20 unavailable: {symbol}")
            elif candle_close < float(latest["EMA20"]):
                logger.info(
                    f"  ❌ Below EMA20 (C={candle_close:.2f} < EMA20={float(latest['EMA20']):.2f}): {symbol}"
                )
                rejection_counts["below_ema20"] += 1
                continue

            # ── FILTER 10: SMA50 ───────────────────────────────────────────────────────
            # Price below SMA50 = medium-term downtrend. Momentum trades need the
            # medium-term structure to be bullish before entering.
            if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")):
                sma50_val = float(latest["SMA50"])
                if candle_close < sma50_val:
                    logger.info(
                        f"  ❌ Below SMA50 (C={candle_close:.2f} < SMA50={sma50_val:.2f}): {symbol}"
                    )
                    rejection_counts["below_sma50"] += 1
                    continue

            # ── FILTER 11: GOLDEN CROSS ────────────────────────────────────────────────
            # SMA50 must be above SMA200. This confirms the long-term trend is up.
            # Stocks below their 200-day MA are in structural downtrends — skip them.
            if (
                "SMA50" in ticker.columns and "SMA200" in ticker.columns and
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

            # ── FILTER 12: MACD ────────────────────────────────────────────────────────
            # MACD line above signal line = bullish momentum confirmed on this timeframe.
            # MACD crossing below signal often precedes a multi-bar decline.
            if (
                "MACD" in ticker.columns and "MACD_SIGNAL" in ticker.columns and
                not pd.isna(latest.get("MACD")) and not pd.isna(latest.get("MACD_SIGNAL"))
            ):
                macd_val   = float(latest["MACD"])
                macd_sig   = float(latest["MACD_SIGNAL"])
                if macd_val < macd_sig:
                    logger.info(
                        f"  ❌ MACD bearish (MACD={macd_val:.4f} < Signal={macd_sig:.4f}): {symbol}"
                    )
                    rejection_counts["macd_bearish"] += 1
                    continue
                logger.info(f"  ✔ MACD bullish: {macd_val:.4f} > {macd_sig:.4f}")

            # ── FILTER 13: SINGLE-BAR MOVE CAP ────────────────────────────────────────
            # A 15m bar that moved >4% from the previous close is an event bar —
            # news spike, halt resumption, or opening gap. These are already done moves.
            if len(ticker) >= 2:
                prev_close = float(ticker["Close"].iloc[-2])
                if prev_close > 0:
                    single_move_pct = abs(candle_close - prev_close) / prev_close * 100
                    if single_move_pct > MAX_SINGLE_CANDLE_MOVE_PCT:
                        logger.info(
                            f"  ❌ Event/gap candle ({single_move_pct:.1f}% move > {MAX_SINGLE_CANDLE_MOVE_PCT}%): {symbol}"
                        )
                        rejection_counts["gap_candle"] += 1
                        continue

            # ── ALL CANDLE FILTERS PASSED — LOG SUMMARY ───────────────────────────────
            logger.info(
                f"  ✔ Candle OK | Body={body_ratio:.0%} | ClosePos={close_position:.0%} "
                f"| Wick={wick_ratio:.0%} | Vol={volume_ratio:.2f}x | RSI={rsi_val:.1f}"
            )

            # ── DEDUP KEY ──────────────────────────────────────────────────────────────
            # Prevents the same signal from being alerted multiple times on the same day.
            breakout_type = ", ".join(signals)
            today_str     = datetime.now(IST).strftime("%Y-%m-%d")
            dedup_key     = f"{breakout_type}|{today_str}"

            # ── SCORE ──────────────────────────────────────────────────────────────────
            # scoring_engine.py returns 0–100. Returns 0 immediately if any hard
            # disqualifier fires (illiquid, distribution candle, wick >40%, ADX<22,
            # RSI divergence, BB overextension, exhaustion, isolated volume spike).
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

            # ── DEDUP CHECK ────────────────────────────────────────────────────────────
            # save_alert_if_new() returns False if this dedup_key was already saved today.
            saved = save_alert_if_new(
                symbol,
                dedup_key,
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            )

            if not saved:
                logger.info(f"  ⚠️ Duplicate alert suppressed (already sent today): {symbol}")
                rejection_counts["duplicate"] += 1
                continue

            # ── BUILD ALERT PAYLOAD ────────────────────────────────────────────────────
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

    # ── SEND ALERTS ─────────────────────────────────────────────────────────────────
    # Sort categories alphabetically, sort stocks within each category by score DESC.
    # Split into chunks of CHUNK_SIZE to avoid Telegram message length limits.

    scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    if total_alerts == 0:
        logger.info("📭 No alerts this scan cycle")
    else:
        for cat in sorted(alerts_by_category.keys()):
            cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)
            chunks     = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

            for chunk_num, chunk in enumerate(chunks, start=1):
                msg = build_message("INTRADAY", cat, chunk, chunk_num, len(chunks), scan_time)
                send_telegram_message(msg, scan_type="INTRADAY")
                logger.info(
                    f"📨 Telegram sent | Category={cat} | Chunk={chunk_num}/{len(chunks)} | Stocks={len(chunk)}"
                )

    # ── SCAN SUMMARY ─────────────────────────────────────────────────────────────────
    duration = (datetime.now(IST) - scan_start).total_seconds()

    logger.info("=" * 80)
    logger.info(f"✅ INTRADAY SCAN COMPLETE | {round(duration, 2)}s | Alerts={total_alerts}/{len(watchlist)}")
    logger.info("── Rejection breakdown ──────────────────────────────────────────────────")
    for reason, count in rejection_counts.items():
        if count > 0:
            logger.info(f"   {reason:<22}: {count}")
    logger.info(f"💤 Next scan in 5 minutes")
    logger.info("=" * 80)

    time.sleep(300)

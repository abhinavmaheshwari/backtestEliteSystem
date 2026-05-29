# =====================================================================================
# app/live_scanner.py
# TREND CONFIRMATION SCANNER — 1H BARS
#
# WHAT THIS FILE DOES:
#   Runs every 5 minutes during market hours (10:17 AM – 3:30 PM IST).
#   Starts at 10:17 (not 9:15) because a full hour bar needs at least one complete
#   60-minute candle — the 9:15 bar doesn't close until 10:14 AM.
#   Downloads 60 days of 1H OHLCV data, applies indicators, and runs candidates
#   through a stricter filter stack than intraday — 1H signals represent larger,
#   more committed moves that are worth swing-trading over 1–3 days.
#
# FILTER PIPELINE (in order — a stock must pass ALL of these):
#   1.  Data quality       — enough candles, no missing columns, no forming bar
#   2.  Signal count       — at least 2 breakout signals required
#   3.  Candle body        — body ≥ 55% of range
#   4.  Bullish close      — close strictly above open
#   5.  Close position     — close in top 30% of bar range
#   6.  Upper wick         — wick ≤ 30% of range (slightly looser: daily bars have more wick)
#   7.  Volume ratio       — current bar ≥ 1.8× 20-bar average
#   8.  Avg volume floor   — 20-bar average ≥ 200K shares (stricter than intraday)
#   9.  Min stock price    — close ≥ ₹50 (no penny stocks)
#   10. RSI range          — RSI 55–75
#   11. RSI direction      — RSI now > RSI 3 bars ago
#   12. EMA20              — close above EMA20
#   13. SMA50              — close above SMA50
#   14. Golden cross       — SMA50 ≥ SMA200
#   15. MACD               — MACD line above signal line
#   16. 52W high proximity — within 15% of 52-week high (no overhead supply)
#   17. Single-bar move    — bar move ≤ 6% (slightly wider than 15m)
#   18. Score threshold    — composite score ≥ 75
#
# CHANGES FROM PREVIOUS VERSION:
#   + MIN_SIGNALS kept at 2 (was already correct)
#   + MIN_VOLUME_RATIO changed from 1.8 → 1.8 (no change, was already right)
#   + MIN_RSI changed from 55 → 55 (no change)
#   + MAX_RSI changed from 80 → 75 (tighter — avoid overbought)
#   + MIN_SCORE changed from 80 → 75 (balanced with stricter upstream filters)
#   + NEW: close position check (top 30% of bar)
#   + NEW: upper wick ratio check (≤30% of range)
#   + NEW: RSI direction check (RSI must be rising over 3 bars)
#   + NEW: MACD bullish confirmation
#   + NEW: 52W high proximity check (within 15%)
#   + NEW: avg volume floor (200K shares)
#   + NEW: min stock price (₹50)
#   + NEW: single-bar move cap (≤6%)
#   + IMPROVED: all rejections log the exact reason + value
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
from delivery_data import fetch_previous_day_delivery

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
CHUNK_SIZE = 10   # Max stocks per Telegram message

# =====================================================================================
# FILTER CONSTANTS — 1H SWING
#
# These are tuned for 60-minute bars on NSE stocks.
# 1H bars represent larger, more committed price moves than 15m bars.
# Filters are stricter on price/liquidity (₹50 min, 200K avg vol),
# slightly looser on candle structure (more wick is normal on hourly bars).
# =====================================================================================

# Minimum independent breakout signals. 2 = minimum confluence for swing entries.
MIN_SIGNALS = 2

# Candle body ≥ 55% of full range. Filters indecision bars.
MIN_BODY_RATIO = 0.55

# Close must be in top 30% of the hour's price range.
# Confirms buyers held control into the hourly close.
MIN_CLOSE_POSITION = 0.70

# Upper wick ≤ 30% of range. Hourly bars naturally develop more wick
# as price probes resistance intraday — 30% is the practical rejection threshold.
MAX_UPPER_WICK_RATIO = 0.30

# Volume must be ≥ 1.8× the 20-bar average on the 1H chart.
MIN_VOLUME_RATIO = 1.8

# Hard liquidity floor: 20-bar avg volume ≥ 200K shares.
# Swing trades need higher liquidity than intraday — you need to exit over days.
MIN_AVG_VOLUME_SHARES = 200_000

# Minimum stock price: ₹50. Penny stocks are manipulation targets.
MIN_STOCK_PRICE = 50.0

# RSI sweet spot on 1H bars: 55–75.
# Same logic as 15m but the RSI on 1H is slower and more reliable.
MIN_RSI = 55
MAX_RSI = 75

# RSI must be higher than it was 3 bars (3 hours) ago.
# Confirms momentum is building, not fading, over the recent session.
RSI_LOOKBACK_BARS = 3

# Stock must be within 15% of its 52-week high.
# Near 52W highs = no overhead supply = cleanest breakout conditions.
# >15% below 52W high = still in recovery mode, not momentum mode.
MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0

# Maximum single-bar move from previous close: 6% on 1H bars.
# A 6%+ hourly bar = major event (earnings, news). The move is already done.
MAX_SINGLE_CANDLE_MOVE_PCT = 6.0

# Minimum composite score to generate a swing alert.
MIN_SCORE = 75

# =====================================================================================
# INIT
# =====================================================================================

init_db()
cleanup_old_alerts(days=7)
logger.info("✅ Database initialized | Stale alerts cleaned (7-day window)")

# =====================================================================================
# MAIN LOOP
# =====================================================================================

while True:

    ist_now      = datetime.now(IST)
    current_time = ist_now.time()
    weekday      = ist_now.weekday()

    # Start at 10:17 — gives time for the first complete 1H bar (9:15–10:14) to close
    # plus a small buffer to ensure yfinance has the data available.
    market_open  = dt_time(10, 17) <= current_time <= dt_time(15, 30)
    weekday_open = weekday < 5

    if not (market_open and weekday_open):
        logger.info(
            f"⏰ Outside 1H scan window | {ist_now.strftime('%H:%M:%S')} "
            f"({'Weekend' if not weekday_open else 'Pre-10:17 or closed'}) | sleeping 5m"
        )
        time.sleep(300)
        continue

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
    alerts_by_category = {}

    # ── PREVIOUS-DAY DELIVERY DATA ───────────────────────────────────────────────────
    # Fetched once per scan cycle. Previous session's NSE delivery % is a meaningful
    # proxy for positional conviction — high delivery on prior day = institutions held
    # overnight, which supports today's intraday/swing momentum reads.
    # Returns {} if unavailable — delivery_pct will be None, bonus skipped silently.
    prev_delivery_map: dict[str, float] = fetch_previous_day_delivery()
    if prev_delivery_map:
        logger.info(f"📦 Previous-day delivery loaded | {len(prev_delivery_map)} symbols")
    else:
        logger.info("📦 Previous-day delivery unavailable — delivery scoring skipped this cycle")

    # Per-scan rejection counters — printed at the end of each scan cycle.
    # Use these to identify which filter is killing the most candidates.
    rejection_counts = {
        "no_data":              0,
        "missing_col":          0,
        "forming_candle":       0,
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
        "below_ema20":          0,
        "below_sma50":          0,
        "no_golden_cross":      0,
        "macd_bearish":         0,
        "far_from_52w_high":    0,
        "gap_candle":           0,
        "low_score":            0,
        "duplicate":            0,
    }

    logger.info("=" * 80)
    logger.info(f"🚀 1H SCAN STARTED | Stocks={len(watchlist)} | {scan_start.strftime('%H:%M:%S IST')}")
    logger.info("=" * 80)

    for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

        symbol = "UNKNOWN"

        try:

            symbol   = row["Stock"]
            category = row["Category"]

            logger.info(f"🔍 [{idx}/{len(watchlist)}] {symbol} | Category={category}")

            # ── DOWNLOAD DATA ────────────────────────────────────────────────────────
            # 60 days of 1H data = ~360 bars (6 bars/day × 60 days).
            # 60d is the maximum yfinance supports for 1H resolution.
            # Need ≥100 bars for reliable indicator calculation.
            ticker = yf.download(
                f"{symbol}.NS",
                period="60d",
                interval="1h",
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

            # ── FORMING CANDLE CHECK ──────────────────────────────────────────────────
            # Drop the last bar if the 60-minute window hasn't closed yet.
            # An incomplete hourly candle can look like a massive breakout mid-bar
            # and then finish as a doji — always wait for the close.
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
                        logger.warning(
                            f"  ⚠️ Dropping forming 1H candle | closes at {candle_end.strftime('%H:%M')} | {symbol}"
                        )
                        ticker = ticker.iloc[:-1].copy()
                        rejection_counts["forming_candle"] += 1
                except Exception:
                    logger.warning(f"  ⚠️ Candle age check failed (skipping drop): {symbol}")

            # Need ≥100 bars for EMA20, SMA50, RSI, ADX to be reliable.
            if len(ticker) < 100:
                logger.warning(f"  ❌ Insufficient bars ({len(ticker)} < 100): {symbol}")
                rejection_counts["insufficient_bars"] += 1
                continue

            # ── INDICATORS ───────────────────────────────────────────────────────────
            ticker = apply_indicators(ticker, timeframe="1h")

            if ticker is None or ticker.empty:
                logger.warning(f"  ❌ Indicator calculation failed: {symbol}")
                rejection_counts["indicator_fail"] += 1
                continue

            # ── BREAKOUT SIGNALS ──────────────────────────────────────────────────────
            signals = detect_breakouts(ticker, timeframe="1h")

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
                logger.info(f"  ❌ Bearish candle (C={candle_close:.2f} ≤ O={candle_open:.2f}): {symbol}")
                rejection_counts["bearish_candle"] += 1
                continue

            # ── FILTER 3: CLOSE POSITION ──────────────────────────────────────────────
            if close_position < MIN_CLOSE_POSITION:
                logger.info(
                    f"  ❌ Weak close position ({close_position:.0%} < {MIN_CLOSE_POSITION:.0%}): {symbol}"
                )
                rejection_counts["weak_close_pos"] += 1
                continue

            # ── FILTER 4: UPPER WICK ──────────────────────────────────────────────────
            if wick_ratio > MAX_UPPER_WICK_RATIO:
                logger.info(
                    f"  ❌ Upper wick rejection ({wick_ratio:.0%} > {MAX_UPPER_WICK_RATIO:.0%}): {symbol}"
                )
                rejection_counts["upper_wick"] += 1
                continue

            # ── FILTER 5: VOLUME RATIO ────────────────────────────────────────────────
            if volume_ratio < MIN_VOLUME_RATIO:
                logger.info(
                    f"  ❌ Low volume ({volume_ratio:.2f}x < {MIN_VOLUME_RATIO}x): {symbol}"
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
            # Penny stocks have erratic volume and are manipulation targets.
            if candle_close < MIN_STOCK_PRICE:
                logger.info(f"  ❌ Penny stock (₹{candle_close:.2f} < ₹{MIN_STOCK_PRICE}): {symbol}")
                rejection_counts["penny_stock"] += 1
                continue

            # ── FILTER 8: RSI RANGE ───────────────────────────────────────────────────
            if not (MIN_RSI <= rsi_val <= MAX_RSI):
                logger.info(f"  ❌ RSI out of range ({rsi_val:.1f}, need {MIN_RSI}–{MAX_RSI}): {symbol}")
                rejection_counts["rsi_range"] += 1
                continue

            # ── FILTER 9: RSI DIRECTION ───────────────────────────────────────────────
            if len(ticker) > RSI_LOOKBACK_BARS:
                rsi_prev = float(ticker["RSI"].iloc[-1 - RSI_LOOKBACK_BARS])
                if rsi_val <= rsi_prev:
                    logger.info(
                        f"  ❌ RSI not rising ({rsi_val:.1f} ≤ {rsi_prev:.1f} from {RSI_LOOKBACK_BARS} bars ago): {symbol}"
                    )
                    rejection_counts["rsi_not_rising"] += 1
                    continue
                logger.info(f"  ✔ RSI rising: {rsi_prev:.1f} → {rsi_val:.1f}")

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
            # Stocks near their 52W high have no overhead supply — the cleanest
            # breakout condition. Stocks >15% below their 52W high are in recovery,
            # not momentum.
            if "HIGH_52W" in ticker.columns and not pd.isna(latest.get("HIGH_52W")):
                high_52w = float(latest["HIGH_52W"])
                if high_52w > 0:
                    pct_from_high = (high_52w - candle_close) / high_52w * 100
                    if pct_from_high > MAX_DISTANCE_FROM_52W_HIGH_PCT:
                        logger.info(
                            f"  ❌ Too far from 52W high ({pct_from_high:.1f}% below, max {MAX_DISTANCE_FROM_52W_HIGH_PCT}%): {symbol}"
                        )
                        rejection_counts["far_from_52w_high"] += 1
                        continue
                    logger.info(f"  ✔ Near 52W high ({pct_from_high:.1f}% below)")

            # ── FILTER 15: SINGLE-BAR MOVE CAP ───────────────────────────────────────
            if len(ticker) >= 2:
                prev_close = float(ticker["Close"].iloc[-2])
                if prev_close > 0:
                    single_move_pct = abs(candle_close - prev_close) / prev_close * 100
                    if single_move_pct > MAX_SINGLE_CANDLE_MOVE_PCT:
                        logger.info(
                            f"  ❌ Event candle ({single_move_pct:.1f}% > {MAX_SINGLE_CANDLE_MOVE_PCT}%): {symbol}"
                        )
                        rejection_counts["gap_candle"] += 1
                        continue

            # ── ALL FILTERS PASSED — LOG SUMMARY ─────────────────────────────────────
            logger.info(
                f"  ✔ Candle OK | Body={body_ratio:.0%} | ClosePos={close_position:.0%} "
                f"| Wick={wick_ratio:.0%} | Vol={volume_ratio:.2f}x | RSI={rsi_val:.1f} "
                f"| Price=₹{candle_close:.2f}"
            )

            # ── DEDUP KEY ─────────────────────────────────────────────────────────────
            breakout_type = ", ".join(signals)
            today_str     = datetime.now(IST).strftime("%Y-%m-%d")
            dedup_key     = f"{breakout_type}|{today_str}|1H"

            # ── SCORE ─────────────────────────────────────────────────────────────────
            # delivery_pct: previous session NSE delivery % — rewards stocks where prior
            # day's volume was positional, not intraday churn. None → bonus skipped.
            delivery_pct = prev_delivery_map.get(symbol, None)
            if delivery_pct is not None:
                logger.info(f"  📦 Prev-day delivery: {delivery_pct:.1f}%")

            score = calculate_score(
                category=category,
                breakout_count=len(signals),
                rsi=rsi_val,
                volume_ratio=volume_ratio,
                breakout_signals=signals,
                ticker=ticker,
                latest=latest,
                symbol=symbol,
                timeframe="1h",
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
                logger.info(f"  ⚠️ Duplicate suppressed (already sent today): {symbol}")
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

    # ── SEND ALERTS ──────────────────────────────────────────────────────────────────
    scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    if total_alerts == 0:
        logger.info("📭 No alerts this scan cycle")
    else:
        for cat in sorted(alerts_by_category.keys()):
            cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)
            chunks     = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

            for chunk_num, chunk in enumerate(chunks, start=1):
                msg = build_message("1H", cat, chunk, chunk_num, len(chunks), scan_time)
                send_telegram_message(msg, scan_type="1H")
                logger.info(
                    f"📨 Telegram sent | Category={cat} | Chunk={chunk_num}/{len(chunks)} | Stocks={len(chunk)}"
                )

    # ── SCAN SUMMARY ──────────────────────────────────────────────────────────────────
    duration = (datetime.now(IST) - scan_start).total_seconds()

    logger.info("=" * 80)
    logger.info(f"✅ 1H SCAN COMPLETE | {round(duration, 2)}s | Alerts={total_alerts}/{len(watchlist)}")
    logger.info("── Rejection breakdown ──────────────────────────────────────────────────")
    for reason, count in rejection_counts.items():
        if count > 0:
            logger.info(f"   {reason:<24}: {count}")
    logger.info(f"💤 Next scan in 5 minutes")
    logger.info("=" * 80)

    time.sleep(300)

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
# DATA SOURCE:
#   yfinance — used for historical OHLCV bars only.
#   TradingView Screener is used by daily_builder.py to build the watchlist
#   (fundamentals, sector classification). Once that parquet is on disk,
#   the scanners fetch price bars from yfinance. The two sources are complementary:
#   TV for "which stocks", yfinance for "what did they do today".
#
# FIXES APPLIED (v4):
#   FIX 1 — Wrapped while-True loop in def start() — module-level loop blocked import
#   FIX 2 — fetch_watchlist_data() batch download — was 370 sequential API calls/cycle
#   FIX 3 — All thresholds imported from config.py — hardcoded constants removed
#   FIX 4 — init_db() / cleanup moved inside start() — was running at import time
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
from delivery_data import fetch_previous_day_delivery

from sector_rotation import get_sector_scores  # get_sector_score_bonus removed — use rotation_result.score_bonus_for()

# FIX 3: Centralized config — no more hardcoded constants
from config import (
    WATCHLIST_PATH,
    SCORE_THRESHOLDS,
    SCAN_CONFIG,
    BATCH_DOWNLOAD_SIZE,
    DEDUP_DAYS,
)

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
# THRESHOLD IMPORTS — sourced from config.py (1h section)
# Changing these values: edit config.py, not here.
# =====================================================================================

TIMEFRAME               = "1h"
MIN_SIGNALS             = SCAN_CONFIG["1h"]["MIN_SIGNALS"]
MIN_BODY_RATIO          = SCAN_CONFIG["1h"]["MIN_BODY_RATIO"]
MIN_CLOSE_POSITION      = SCAN_CONFIG["1h"]["MIN_CLOSE_POSITION"]
MAX_UPPER_WICK_RATIO    = SCAN_CONFIG["1h"]["MAX_UPPER_WICK"]
MIN_VOLUME_RATIO        = SCAN_CONFIG["1h"]["MIN_VOLUME_RATIO"]
MIN_AVG_VOLUME_SHARES   = SCAN_CONFIG["1h"]["MIN_VOLUME_AVG"]
MIN_RSI                 = SCAN_CONFIG["1h"]["MIN_RSI"]
MAX_RSI                 = SCAN_CONFIG["1h"]["MAX_RSI"]
MIN_SCORE               = SCORE_THRESHOLDS["1h"]

# These constants are 1H-specific — not in config.py (no intraday/EOD equivalent)
MIN_STOCK_PRICE             = 50.0
RSI_LOOKBACK_BARS           = 3
MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0
MAX_SINGLE_CANDLE_MOVE_PCT  = 6.0


# =====================================================================================
# BATCH DATA DOWNLOAD — FIX 2
# Downloads all watchlist symbols in batches instead of one-by-one.
# 370 symbols × 1 call = 370 API hits per cycle → rate-limit ban in ~30 min.
# 370 symbols ÷ 30 per batch = 13 API calls per cycle → no ban risk.
# =====================================================================================

def fetch_watchlist_data(
    watchlist: pd.DataFrame,
    period: str = "60d",
    interval: str = "1h"
) -> dict[str, pd.DataFrame]:
    """
    Downloads OHLCV data for all watchlist symbols in batches via yfinance.

    Returns
    -------
    dict[str, pd.DataFrame]  — {symbol: ohlcv_df}, only successfully downloaded symbols

    Each DataFrame has columns: Datetime, Open, High, Low, Close, Volume (reset index).
    Symbols with no data or download errors are silently omitted from the dict.
    Callers check `if symbol not in all_data` to handle missing entries.
    """
    symbols    = watchlist["Stock"].tolist()
    all_data   = {}
    total      = len(symbols)
    batch_size = BATCH_DOWNLOAD_SIZE

    for i in range(0, total, batch_size):
        batch       = symbols[i : i + batch_size]
        tickers_str = " ".join(f"{sym}.NS" for sym in batch)
        batch_end   = min(i + batch_size, total)

        logger.info(f"📥 Batch {i // batch_size + 1} | symbols {i+1}–{batch_end}/{total}")

        try:
            # group_by='ticker' locks MultiIndex to (Ticker, OHLCV) — prevents breakage
            # when yfinance changes its default column layout between versions.
            raw = yf.download(
                tickers_str,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
                threads=False,
                group_by="ticker",
            )

            if raw is None or raw.empty:
                logger.warning(f"⚠️ Empty response for batch {i // batch_size + 1}")
                continue

            # Detect actual structure returned — don't trust len(batch).
            # If 29 of 30 tickers are suspended/delisted, yfinance returns a flat
            # DataFrame for the one survivor instead of a MultiIndex.
            if not isinstance(raw.columns, pd.MultiIndex):
                # Flat DataFrame — yfinance returned a single-ticker result.
                if len(batch) == 1:
                    # We only requested 1 ticker, so batch[0] is safely the correct stock.
                    sym = batch[0]
                    df  = raw.reset_index().copy()
                    if not df.empty:
                        all_data[sym] = df
                else:
                    # We requested a multi-ticker batch but only 1 survived (others
                    # delisted/suspended). Assigning to batch[0] would map the wrong
                    # symbol to a survivor's data — skip the batch entirely instead.
                    logger.warning(
                        f"⚠️ YF returned flat DF for multi-ticker batch "
                        f"(batch {i // batch_size + 1}, {len(batch)} requested). "
                        f"Skipping to prevent symbol→data mismatch."
                    )
                    continue

            else:
                # Multi-ticker: MultiIndex columns (Ticker, OHLCV) with group_by='ticker'
                for sym in batch:
                    ns_sym = f"{sym}.NS"
                    try:
                        level0 = raw.columns.get_level_values(0)
                        key    = ns_sym if ns_sym in level0 else (sym if sym in level0 else None)
                        if key is None:
                            logger.warning(f"⚠️ Symbol not in batch response: {sym}")
                            continue
                        df = raw[key].reset_index().copy()
                        if not df.empty:
                            all_data[sym] = df
                    except Exception:
                        logger.exception(f"❌ Slice error extracting {sym} from batch")

        except Exception:
            logger.exception(f"❌ Batch download failed (batch {i // batch_size + 1})")

    logger.info(f"📥 Download complete | {len(all_data)}/{total} symbols fetched")
    return all_data


# =====================================================================================
# START — FIX 1: entire scanning loop inside a function
# Called from main.py via:  import live_scanner; live_scanner.start()
# =====================================================================================

def start():
    """
    Main 1H scanning loop. Runs every 5 minutes during market hours.
    Wrapped in start() so importing this module does NOT block main.py.
    """

    # FIX 4: DB init inside start(), not at module level
    init_db()
    cleanup_old_alerts(days=DEDUP_DAYS)
    logger.info(f"✅ 1H scanner ready | DB initialized | {DEDUP_DAYS}-day dedup window active")

    while True:

        ist_now      = datetime.now(IST)
        current_time = ist_now.time()
        weekday      = ist_now.weekday()

        # GAP 3 FIX: Extended to 15:35 (was 15:30).
        # The final 1H bar closes at 15:30. Without this extension the scanner wakes
        # at 15:31, sees current_time > 15:30, and misses the last BTST sweep entirely.
        # Start at 10:17 — first complete 1H bar (9:15–10:14) must have closed
        market_open = dt_time(10, 17) <= current_time <= dt_time(15, 35)

        if weekday >= 5 or not market_open:
            logger.info(
                f"⏰ Outside 1H window | {ist_now.strftime('%H:%M')} "
                f"({'Weekend' if weekday >= 5 else 'Pre/post market'}) | sleep 5m"
            )
            time.sleep(300)
            continue

        scan_start         = datetime.now(IST)
        total_alerts       = 0
        alerts_by_category = {}

        logger.info("=" * 70)
        logger.info(f"🚀 1H SCAN | {scan_start.strftime('%Y-%m-%d %H:%M:%S IST')}")
        logger.info("=" * 70)

        sleep_time = 300  # default; replaced with precise dynamic value after scan completes
        try:
            # ── LOAD WATCHLIST ──────────────────────────────────────────────────────
            try:
                watchlist = pd.read_parquet(WATCHLIST_PATH)
                logger.info(f"📋 Watchlist | {len(watchlist)} stocks")
            except Exception:
                logger.exception("❌ Watchlist load failed — attempting rebuild")
                try:
                    from daily_builder import build_watchlist
                    build_watchlist()
                    watchlist = pd.read_parquet(WATCHLIST_PATH)
                    logger.info(f"📋 Watchlist rebuilt | {len(watchlist)} stocks")
                except Exception:
                    logger.exception("❌ Watchlist rebuild also failed — aborting scan cycle")
                    time.sleep(300)
                    continue

            # ── BATCH DOWNLOAD (FIX 2) ──────────────────────────────────────────────
            all_ticker_data = fetch_watchlist_data(watchlist, period="60d", interval="1h")

            # ── DELIVERY DATA ───────────────────────────────────────────────────────
            prev_delivery_map = fetch_previous_day_delivery()
            if prev_delivery_map:
                logger.info(f"📦 Delivery data | {len(prev_delivery_map)} symbols")
            else:
                logger.info("📦 Delivery data unavailable — bonus skipped")

            # ── SECTOR ROTATION (once per scan, cached 30 min) ──────────────────────
            try:
                rotation_result = get_sector_scores()
                if rotation_result.scores:
                    logger.info(
                        f"🔄 Sector rotation loaded | "
                        f"{len(rotation_result.scores)} sectors | "
                        f"leading={len(rotation_result.strong_sectors)}"
                    )
                else:
                    logger.info("🔄 Sector rotation unavailable — bonus skipped")
            except Exception:
                logger.exception("⚠️ Sector rotation fetch failed — continuing without it")
                from sector_rotation import SectorRotationResult
                from datetime import date as _date
                rotation_result = SectorRotationResult({}, set(), set(), "", _date.today(), 0.0)

            # Per-scan rejection counters
            rejection_counts = {
                "no_data":           0,
                "missing_col":       0,
                "forming_candle":    0,
                "insufficient_bars": 0,
                "indicator_fail":    0,
                "weak_signals":      0,
                "weak_body":         0,
                "bearish_candle":    0,
                "weak_close_pos":    0,
                "upper_wick":        0,
                "low_volume":        0,
                "low_avg_volume":    0,
                "penny_stock":       0,
                "rsi_range":         0,
                "rsi_not_rising":    0,
                "below_ema20":       0,
                "below_sma50":       0,
                "no_golden_cross":   0,
                "macd_bearish":      0,
                "far_from_52w_high": 0,
                "gap_candle":        0,
                "low_score":         0,
                "duplicate":         0,
                "stale_data":        0,
            }

            # ── PER-STOCK PROCESSING ────────────────────────────────────────────────
            for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

                symbol = "UNKNOWN"

                try:
                    symbol   = row["Stock"]
                    category = row["Category"]
                    sector   = row.get("Sector", None)

                    # FIX 2: use pre-downloaded batch data
                    if symbol not in all_ticker_data:
                        rejection_counts["no_data"] += 1
                        continue

                    ticker = all_ticker_data[symbol].copy()

                    if ticker.empty:
                        rejection_counts["no_data"] += 1
                        continue

                    # ── COLUMN NORMALISATION ────────────────────────────────────────
                    if isinstance(ticker.columns, pd.MultiIndex):
                        ticker.columns = ticker.columns.get_level_values(0)

                    ticker = ticker.loc[:, ~ticker.columns.duplicated()]

                    required_cols = ["Open", "High", "Low", "Close", "Volume"]
                    missing_col   = False

                    for col_name in required_cols:
                        if col_name not in ticker.columns:
                            logger.warning(f"  ❌ Missing col '{col_name}': {symbol}")
                            missing_col = True
                            break
                        if isinstance(ticker[col_name], pd.DataFrame):
                            ticker[col_name] = ticker[col_name].iloc[:, 0]
                        ticker[col_name] = pd.Series(ticker[col_name]).astype(float)

                    if missing_col:
                        rejection_counts["missing_col"] += 1
                        continue

                    ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

                    # ── FORMING CANDLE CHECK ────────────────────────────────────────
                    datetime_col = next(
                        (c for c in ["Datetime", "Date", "index"] if c in ticker.columns),
                        None
                    )
                    if datetime_col is not None:
                        try:
                            raw_ts = pd.Timestamp(ticker.iloc[-1][datetime_col])
                            # Convert to IST before stripping tz — yfinance may return
                            # UTC or IST timestamps. Normalising ensures the naive
                            # comparison is always in IST wall-clock time.
                            if raw_ts.tzinfo is not None:
                                raw_ts = raw_ts.tz_convert("Asia/Kolkata")
                            candle_start = raw_ts.replace(tzinfo=None)
                            candle_end   = candle_start + pd.Timedelta(minutes=60)
                            now_naive    = datetime.now(IST).replace(tzinfo=None)
                            if now_naive < candle_end:
                                ticker = ticker.iloc[:-1].copy()
                                rejection_counts["forming_candle"] += 1
                        except Exception:
                            logger.exception(f"  ⚠️ Candle age check error {symbol}")

                    if len(ticker) < 100:
                        rejection_counts["insufficient_bars"] += 1
                        continue

                    # ── INDICATORS ──────────────────────────────────────────────────
                    ticker = apply_indicators(ticker, timeframe="1h")

                    if ticker is None or ticker.empty:
                        rejection_counts["indicator_fail"] += 1
                        continue

                    # ── BREAKOUT SIGNALS ────────────────────────────────────────────
                    signals = detect_breakouts(ticker, timeframe="1h")

                    if len(signals) < MIN_SIGNALS:
                        rejection_counts["weak_signals"] += 1
                        continue

                    latest = ticker.iloc[-1]

                    # ── STALE DATA GUARD ─────────────────────────────────────────────
                    # Halted / illiquid stocks return data ending on the last day they
                    # traded. 1H fetches 60 days of history so stale bars are especially
                    # likely to survive all filters and fire a false alert.
                    # We compare the last bar's date against today's IST date and skip
                    # the stock entirely if it doesn't match.
                    _stale_col = next(
                        (c for c in ["Datetime", "Date", "index"] if c in ticker.columns),
                        None
                    )
                    if _stale_col:
                        try:
                            _last_ts = pd.to_datetime(latest[_stale_col])
                            if _last_ts.tzinfo is not None:
                                _last_ts = _last_ts.tz_convert("Asia/Kolkata")
                            if _last_ts.date() != ist_now.date():
                                rejection_counts["stale_data"] += 1
                                continue
                        except Exception:
                            pass  # unparseable timestamp — allow through, don't crash

                    if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
                        logger.warning(f"  ❌ RSI unavailable: {symbol}")
                        continue

                    # ── VOLUME ──────────────────────────────────────────────────────
                    latest_volume = float(latest["Volume"])
                    # GAP 1 FIX: exclude the current bar from the baseline average.
                    # Using tail(20) includes today's breakout candle, deflating the ratio.
                    avg_volume    = float(ticker["Volume"].iloc[-21:-1].mean())

                    if avg_volume <= 0:
                        logger.warning(f"  ❌ Zero avg volume: {symbol}")
                        continue

                    volume_ratio = latest_volume / avg_volume

                    # ── CANDLE GEOMETRY ─────────────────────────────────────────────
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

                    # ── FILTER 1: CANDLE BODY ───────────────────────────────────────
                    if body_ratio < MIN_BODY_RATIO:
                        rejection_counts["weak_body"] += 1
                        continue

                    # ── FILTER 2: BULLISH CANDLE ────────────────────────────────────
                    if candle_close <= candle_open:
                        rejection_counts["bearish_candle"] += 1
                        continue

                    # ── FILTER 3: CLOSE POSITION ────────────────────────────────────
                    if close_position < MIN_CLOSE_POSITION:
                        rejection_counts["weak_close_pos"] += 1
                        continue

                    # ── FILTER 4: UPPER WICK ────────────────────────────────────────
                    if wick_ratio > MAX_UPPER_WICK_RATIO:
                        rejection_counts["upper_wick"] += 1
                        continue

                    # ── FILTER 5: VOLUME RATIO ──────────────────────────────────────
                    if volume_ratio < MIN_VOLUME_RATIO:
                        rejection_counts["low_volume"] += 1
                        continue

                    # ── FILTER 6: AVG VOLUME FLOOR ──────────────────────────────────
                    if avg_volume < MIN_AVG_VOLUME_SHARES:
                        rejection_counts["low_avg_volume"] += 1
                        continue

                    # ── FILTER 7: MINIMUM PRICE ─────────────────────────────────────
                    if candle_close < MIN_STOCK_PRICE:
                        rejection_counts["penny_stock"] += 1
                        continue

                    # ── FILTER 8: RSI RANGE ─────────────────────────────────────────
                    if not (MIN_RSI <= rsi_val <= MAX_RSI):
                        rejection_counts["rsi_range"] += 1
                        continue

                    # ── FILTER 9: RSI DIRECTION ─────────────────────────────────────
                    if len(ticker) > RSI_LOOKBACK_BARS:
                        rsi_prev = float(ticker["RSI"].iloc[-1 - RSI_LOOKBACK_BARS])
                        if rsi_val <= rsi_prev:
                            rejection_counts["rsi_not_rising"] += 1
                            continue

                    # ── FILTER 10: EMA20 ────────────────────────────────────────────
                    if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")):
                        if candle_close < float(latest["EMA20"]):
                            rejection_counts["below_ema20"] += 1
                            continue

                    # ── FILTER 11: SMA50 ────────────────────────────────────────────
                    if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")):
                        if candle_close < float(latest["SMA50"]):
                            rejection_counts["below_sma50"] += 1
                            continue

                    # ── FILTER 12: GOLDEN CROSS ─────────────────────────────────────
                    if (
                        "SMA50" in ticker.columns and "SMA200" in ticker.columns and
                        not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200"))
                    ):
                        if float(latest["SMA50"]) < float(latest["SMA200"]):
                            rejection_counts["no_golden_cross"] += 1
                            continue

                    # ── FILTER 13: MACD ─────────────────────────────────────────────
                    if (
                        "MACD" in ticker.columns and "MACD_SIGNAL" in ticker.columns and
                        not pd.isna(latest.get("MACD")) and not pd.isna(latest.get("MACD_SIGNAL"))
                    ):
                        if float(latest["MACD"]) < float(latest["MACD_SIGNAL"]):
                            rejection_counts["macd_bearish"] += 1
                            continue

                    # ── FILTER 14: 52W HIGH PROXIMITY ───────────────────────────────
                    if "HIGH_52W" in ticker.columns and not pd.isna(latest.get("HIGH_52W")):
                        high_52w = float(latest["HIGH_52W"])
                        if high_52w > 0:
                            pct_from_high = (high_52w - candle_close) / high_52w * 100
                            if pct_from_high > MAX_DISTANCE_FROM_52W_HIGH_PCT:
                                rejection_counts["far_from_52w_high"] += 1
                                continue

                    # ── FILTER 15: SINGLE-BAR MOVE CAP ──────────────────────────────
                    if len(ticker) >= 2:
                        prev_close = float(ticker["Close"].iloc[-2])
                        if prev_close > 0:
                            single_move_pct = abs(candle_close - prev_close) / prev_close * 100
                            if single_move_pct > MAX_SINGLE_CANDLE_MOVE_PCT:
                                rejection_counts["gap_candle"] += 1
                                continue

                    # ── SCORE ────────────────────────────────────────────────────────
                    delivery_pct = prev_delivery_map.get(symbol, None)

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
                        min_vol=MIN_AVG_VOLUME_SHARES,
                    )

                    if score > 0:
                        # ISOLATED TRY/EXCEPT: a sector error will NOT kill the alert
                        try:
                            safe_sector  = "Unknown" if (sector is None or (isinstance(sector, float) and pd.isna(sector))) else str(sector).strip()
                            sector_bonus = rotation_result.score_bonus_for(safe_sector)
                            score = max(0, min(score + sector_bonus, 100))
                        except Exception as e:
                            logger.warning(f"  ⚠️ Sector bonus skipped for {symbol}: {e}")
                            # base score survives — alert still fires

                    logger.info(
                        f"  ✅ {symbol} | Score={score} | "
                        f"Vol={volume_ratio:.1f}x | RSI={rsi_val:.1f} | Sig={len(signals)}"
                    )

                    if score < MIN_SCORE:
                        rejection_counts["low_score"] += 1
                        continue

                    # ── DEDUP ────────────────────────────────────────────────────────
                    # Key encodes Category + Signals + Date + Timeframe.
                    # If ANY of these change (e.g. stock upgrades to a stronger category
                    # or fires a new signal mid-day), the key changes and a fresh alert
                    # fires. The date component ensures full eligibility resets each day.
                    signal_str = ", ".join(signals.keys() if isinstance(signals, dict) else signals)
                    today_str  = datetime.now(IST).strftime("%Y-%m-%d")
                    dedup_key  = f"{category}|{signal_str}|{today_str}|1H"

                    saved = save_alert_if_new(
                        symbol,
                        dedup_key,
                        datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
                    )

                    if not saved:
                        rejection_counts["duplicate"] += 1
                        continue

                    # ── BUILD ALERT PAYLOAD ──────────────────────────────────────────
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
                        "breakout_signals": list(signals.keys()) if isinstance(signals, dict) else signals,
                        "price":            round(candle_close, 2),
                        "open":             round(candle_open, 2),
                        "day_high":         round(candle_high, 2),
                        "day_low":          round(candle_low, 2),
                        "rsi":              round(rsi_val, 1),
                        "volume_ratio":     round(volume_ratio, 2),
                        "body_ratio":       round(body_ratio * 100),
                        "close_position":   round(close_position * 100),
                        "score":            score,
                        "above_ema20":      bool(candle_close >= float(latest["EMA20"])) if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")) else None,
                        "above_sma50":      above_sma50,
                        "golden_cross":     golden_cross,
                    })
                    total_alerts += 1

                except Exception:
                    logger.exception(f"❌ Error processing {symbol}")

            # ── SEND ALERTS ──────────────────────────────────────────────────────────
            scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

            if total_alerts == 0:
                logger.info("📭 No 1H alerts this cycle")
            else:
                for cat in sorted(alerts_by_category.keys()):
                    cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)
                    chunks     = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

                    for chunk_num, chunk in enumerate(chunks, start=1):
                        msg = build_message("1H", cat, chunk, chunk_num, len(chunks), scan_time)
                        send_telegram_message(msg, scan_type="1H")
                        logger.info(f"📨 Sent | {cat} | chunk {chunk_num}/{len(chunks)} | {len(chunk)} stocks")

            # ── SCAN SUMMARY ─────────────────────────────────────────────────────────
            duration = (datetime.now(IST) - scan_start).total_seconds()

            logger.info("=" * 70)
            logger.info(f"✅ 1H DONE | {round(duration, 1)}s | alerts={total_alerts}/{len(watchlist)}")

            # Only log rejection reasons that actually fired (Railway log economy)
            fired = {k: v for k, v in rejection_counts.items() if v > 0}
            if fired:
                logger.info("   Rejections: " + " | ".join(f"{k}={v}" for k, v in fired.items()))

            # Dynamic sleep: keep cycle cadence at exactly 300s regardless of scan duration.
            # Without this, a 45s scan causes the loop to fire every 345s — a full
            # 5-minute lag accumulates over a 6-hour trading day.
            elapsed    = (datetime.now(IST) - scan_start).total_seconds()
            sleep_time = max(0, 300 - elapsed)
            logger.info(f"💤 Scan took {elapsed:.1f}s — sleeping {sleep_time:.1f}s to hit 5-min cadence")
            logger.info("=" * 70)

        except Exception:
            logger.exception("❌ CRITICAL 1H SCAN ERROR — will retry next cycle")
            elapsed    = (datetime.now(IST) - scan_start).total_seconds()
            sleep_time = max(0, 300 - elapsed)

        time.sleep(sleep_time)

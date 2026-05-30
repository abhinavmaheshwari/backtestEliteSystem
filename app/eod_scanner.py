# =====================================================================================
# app/eod_scanner.py
# EOD BREAKOUT SCANNER — DAILY CANDLES
#
# WHAT THIS FILE DOES:
#   Runs once per trading day at 6:30 PM IST.
#   This timing ensures the NSE bhavcopy (delivery data) is published and available,
#   while the final daily candle has long settled (NSE close is 3:30 PM).
#   Downloads 1 year of daily OHLCV data for each watchlist stock, applies indicators,
#   then runs the strictest filter stack of the three scanners.
#
# DATA SOURCE:
#   yfinance — used for historical OHLCV bars only.
#   TradingView Screener is used by daily_builder.py to build the watchlist
#   (fundamentals, sector classification). Once that parquet is on disk,
#   the scanners fetch price bars from yfinance. The two sources are complementary:
#   TV for "which stocks", yfinance for "what did they do today".
#
# FILTER PIPELINE (in order — a stock must pass ALL of these):
#   1.  Data quality       — 200 candles minimum, no missing columns
#   2.  Signal count       — at least 3 breakout signals (strictest confluence)
#   3.  Candle body        — body ≥ 60% of range (tightest of the three scanners)
#   4.  Bullish close      — close strictly above open
#   5.  Close position     — close in top 25% of daily range
#   6.  Upper wick         — wick ≤ 30% of range
#   7.  Volume ratio       — current day ≥ 2.0× 20-day average
#   8.  Avg volume floor   — 20-day avg ≥ 200K shares
#   9.  Min stock price    — close ≥ ₹50
#   10. RSI range          — RSI 58–75 (tightest RSI band)
#   11. RSI direction      — RSI now > RSI 5 days ago (1 week of rising momentum)
#   12. EMA20              — close above EMA20
#   13. SMA50              — close above SMA50
#   14. Golden cross       — SMA50 ≥ SMA200
#   15. MACD               — MACD line above signal line
#   16. 52W high proximity — within 15% of 52-week high
#   17. Single-day move    — day move ≤ 8% from previous close (no gap chases)
#   18. Score threshold    — composite score ≥ 78
#
# FIXES APPLIED (v4):
#   FIX 1 — Wrapped while-True loop in def start() — module-level loop blocked import
#   FIX 2 — fetch_watchlist_data() batch download — was N sequential API calls per scan
#   FIX 3 — All thresholds imported from config.py — hardcoded constants removed
#   FIX 4 — init_db() / cleanup moved inside start() — was running at import time
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
from message_formatter import build_message
from database import init_db, save_alert_if_new, cleanup_old_alerts
from delivery_data import fetch_delivery_data

from sector_rotation import get_sector_scores  # get_sector_score_bonus removed — use rotation_result.score_bonus_for()

# FIX 3: Centralized config — no more hardcoded constants
from config import (
    WATCHLIST_PATH,
    SCORE_THRESHOLDS,
    SCAN_CONFIG,
    BATCH_DOWNLOAD_SIZE,
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
EOD_START  = dt_time(18, 30)   # 6:30 PM — NSE bhavcopy reliably published by then
EOD_END    = dt_time(20, 0)    # 8:00 PM — scan window closes
CHUNK_SIZE = 10

# Delivery data retry settings (NSE sometimes publishes bhavcopy late)
DELIVERY_FETCH_RETRIES    = 5
DELIVERY_RETRY_INTERVAL_S = 600   # 10 minutes between retries

# =====================================================================================
# THRESHOLD IMPORTS — sourced from config.py (1d section)
# Changing these values: edit config.py, not here.
# =====================================================================================

TIMEFRAME               = "1d"
MIN_SIGNALS             = SCAN_CONFIG["1d"]["MIN_SIGNALS"]
MIN_BODY_RATIO          = SCAN_CONFIG["1d"]["MIN_BODY_RATIO"]
MIN_CLOSE_POSITION      = SCAN_CONFIG["1d"]["MIN_CLOSE_POSITION"]
MAX_UPPER_WICK_RATIO    = SCAN_CONFIG["1d"]["MAX_UPPER_WICK"]
MIN_VOLUME_RATIO        = SCAN_CONFIG["1d"]["MIN_VOLUME_RATIO"]
MIN_AVG_VOLUME_SHARES   = SCAN_CONFIG["1d"]["MIN_VOLUME_AVG"]
MIN_RSI                 = SCAN_CONFIG["1d"]["MIN_RSI"]
MAX_RSI                 = SCAN_CONFIG["1d"]["MAX_RSI"]
MIN_SCORE               = SCORE_THRESHOLDS["1d"]

# These constants are EOD-specific — no intraday/1H equivalent
MIN_STOCK_PRICE             = 50.0
RSI_LOOKBACK_BARS           = 5      # 5 days = 1 week of rising momentum
MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0
MAX_SINGLE_DAY_MOVE_PCT     = 8.0


# =====================================================================================
# HELPERS
# =====================================================================================

def seconds_until_eod() -> int:
    """
    Seconds until the next 6:30 PM IST scan window.
    Used to sleep efficiently — avoids polling every 60s all day.
    """
    now          = datetime.now(IST)
    target_today = now.replace(hour=18, minute=30, second=0, microsecond=0)
    if now < target_today:
        delta = target_today - now
    else:
        delta = target_today + timedelta(days=1) - now
    return max(int(delta.total_seconds()), 0)


# =====================================================================================
# BATCH DATA DOWNLOAD — FIX 2
# EOD scanner runs once per day so batching is less critical than for live_scanner,
# but it still prevents Yahoo IP bans if the watchlist is large (300+ stocks).
# =====================================================================================

def fetch_watchlist_data(
    watchlist: pd.DataFrame,
    period: str = "1y",
    interval: str = "1d"
) -> dict[str, pd.DataFrame]:
    """
    Downloads OHLCV data for all watchlist symbols in batches via yfinance.

    Returns
    -------
    dict[str, pd.DataFrame]  — {symbol: ohlcv_df}, only successfully downloaded symbols

    Each DataFrame has columns: Date, Open, High, Low, Close, Volume (reset index).
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

            if len(batch) == 1:
                # Single ticker: plain DataFrame (group_by has no effect here)
                sym = batch[0]
                df  = raw.reset_index().copy()
                if not df.empty:
                    all_data[sym] = df

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
# Called from main.py via:  import eod_scanner; eod_scanner.start()
# =====================================================================================

def start():
    """
    Main EOD scanning loop. Runs once per trading day at 6:30 PM IST.
    Wrapped in start() so importing this module does NOT block main.py.
    """

    # FIX 4: DB init inside start(), not at module level
    init_db()
    cleanup_old_alerts(days=7)
    logger.info("✅ EOD scanner ready | DB initialized | 7-day dedup window active")

    # Tracks the last date a scan completed — prevents double-scanning on the same day
    last_scan_date = None

    while True:

        ist_now      = datetime.now(IST)
        current_time = ist_now.time()
        weekday      = ist_now.weekday()
        today_str    = ist_now.strftime("%Y-%m-%d")

        in_eod_window = EOD_START <= current_time <= EOD_END
        is_weekday    = weekday < 5
        already_ran   = (last_scan_date == today_str)

        # ── WEEKEND ─────────────────────────────────────────────────────────────────
        if not is_weekday:
            sleep_secs = seconds_until_eod()
            logger.info(
                f"📅 {ist_now.strftime('%A')} | Next EOD scan in "
                f"{sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
            )
            time.sleep(min(sleep_secs, 3600))
            continue

        # ── ALREADY RAN TODAY ────────────────────────────────────────────────────────
        if already_ran:
            sleep_secs = seconds_until_eod()
            logger.info(
                f"✅ EOD done for {today_str} | Next in "
                f"{sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
            )
            time.sleep(min(sleep_secs, 3600))
            continue

        # ── NOT YET IN WINDOW ────────────────────────────────────────────────────────
        if not in_eod_window:
            sleep_secs = seconds_until_eod()
            logger.info(
                f"⏰ Waiting for EOD window | Now={ist_now.strftime('%H:%M')} | "
                f"{sleep_secs // 60}m remaining"
            )
            time.sleep(min(sleep_secs, 60))
            continue

        # ════════════════════════════════════════════════════════════════════════════
        # EOD SCAN WINDOW — 6:30–8:00 PM IST
        # Weekday, in window, haven't scanned today → run full scan.
        # ════════════════════════════════════════════════════════════════════════════

        logger.info("=" * 70)
        logger.info(f"📊 EOD SCAN | {ist_now.strftime('%Y-%m-%d %H:%M:%S IST')}")
        logger.info("=" * 70)

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

            # ── FETCH DELIVERY DATA (with retry) ─────────────────────────────────────
            # NSE bhavcopy typically published by 5–6 PM; we start at 6:30 PM.
            # Retry up to 5× (10-min gaps) in case of NSE delays.
            delivery_map: dict[str, float] = {}
            for attempt in range(1, DELIVERY_FETCH_RETRIES + 1):
                delivery_map = fetch_delivery_data(ist_now.date())
                if delivery_map:
                    logger.info(f"📦 Delivery data | {len(delivery_map)} symbols | attempt {attempt}")
                    break
                if attempt < DELIVERY_FETCH_RETRIES:
                    logger.warning(
                        f"⚠️ Delivery fetch empty (attempt {attempt}/{DELIVERY_FETCH_RETRIES}) "
                        f"| retry in {DELIVERY_RETRY_INTERVAL_S // 60}m"
                    )
                    time.sleep(DELIVERY_RETRY_INTERVAL_S)
                else:
                    logger.warning(
                        f"⚠️ Delivery data unavailable after {DELIVERY_FETCH_RETRIES} attempts "
                        "| proceeding without delivery scoring"
                    )

            # ── BATCH DOWNLOAD (FIX 2) ──────────────────────────────────────────────
            all_ticker_data = fetch_watchlist_data(watchlist, period="1y", interval="1d")

            # ── SECTOR ROTATION (once per scan, cached 30 min) ──────────────────────
            # Fetches sector RS scores for all NSE sectors vs Nifty 50.
            # Used as a score modifier (+4 LEADING → -4 LAGGING) per stock.
            # Fully graceful: if sector data unavailable, rotation_result.scores = {}
            # and get_sector_score_bonus() returns 0 — no impact on existing logic.
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

            scan_start         = datetime.now(IST)
            total_alerts       = 0
            alerts_by_category = {}

            rejection_counts = {
                "no_data":           0,
                "missing_col":       0,
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
                "gap_day":           0,
                "low_score":         0,
                "duplicate":         0,
            }

            logger.info(f"🔍 Scanning {len(watchlist)} stocks...")

            for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

                symbol = "UNKNOWN"

                try:
                    symbol   = row["Stock"]
                    category = row["Category"]
                    sector   = row.get("Sector", None)   # from daily_builder.py parquet

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

                    if len(ticker) < 200:
                        rejection_counts["insufficient_bars"] += 1
                        continue

                    # ── INDICATORS ──────────────────────────────────────────────────
                    ticker = apply_indicators(ticker, timeframe="1d")

                    if ticker is None or ticker.empty:
                        rejection_counts["indicator_fail"] += 1
                        continue

                    # ── BREAKOUT SIGNALS ────────────────────────────────────────────
                    signals = detect_breakouts(ticker, timeframe="1d")

                    if len(signals) < MIN_SIGNALS:
                        rejection_counts["weak_signals"] += 1
                        continue

                    latest = ticker.iloc[-1]

                    if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
                        logger.warning(f"  ❌ RSI unavailable: {symbol}")
                        continue

                    # ── VOLUME ──────────────────────────────────────────────────────
                    latest_volume = float(latest["Volume"])
                    avg_volume    = float(ticker["Volume"].tail(20).mean())

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

                    # ── FILTER 15: SINGLE-DAY MOVE CAP ──────────────────────────────
                    if len(ticker) >= 2:
                        prev_close = float(ticker["Close"].iloc[-2])
                        if prev_close > 0:
                            single_move_pct = abs(candle_close - prev_close) / prev_close * 100
                            if single_move_pct > MAX_SINGLE_DAY_MOVE_PCT:
                                rejection_counts["gap_day"] += 1
                                continue

                    # ── DELIVERY DATA ────────────────────────────────────────────────
                    delivery_pct = delivery_map.get(symbol, None)

                    # ── SCORE ────────────────────────────────────────────────────────
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
                        delivery_pct=delivery_pct,
                    )

                    # Sector rotation modifier — applied after base score
                    if score > 0:
                        # ISOLATED TRY/EXCEPT: a sector error will NOT kill the alert
                        try:
                            safe_sector  = str(sector) if sector else "Unknown"
                            sector_bonus = rotation_result.score_bonus_for(symbol=symbol, sector=safe_sector)
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
                    breakout_type = ", ".join(signals)
                    dedup_key     = f"{breakout_type}|{today_str}|EOD"

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
                        "delivery_pct":     round(delivery_pct, 1) if delivery_pct is not None else None,
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
                logger.info("📭 No EOD alerts today")
            else:
                for cat in sorted(alerts_by_category.keys()):
                    cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)
                    chunks     = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

                    for chunk_num, chunk in enumerate(chunks, start=1):
                        msg = build_message("EOD", cat, chunk, chunk_num, len(chunks), scan_time)
                        send_telegram_message(msg, scan_type="EOD")
                        logger.info(f"📨 Sent | {cat} | chunk {chunk_num}/{len(chunks)} | {len(chunk)} stocks")

            # ── SCAN SUMMARY ─────────────────────────────────────────────────────────
            duration       = (datetime.now(IST) - scan_start).total_seconds()
            last_scan_date = today_str   # mark complete for today
            sleep_secs     = seconds_until_eod()

            logger.info("=" * 70)
            logger.info(f"✅ EOD DONE | {round(duration, 1)}s | alerts={total_alerts}/{len(watchlist)}")

            # Only log rejection reasons that actually fired (Railway log economy)
            fired = {k: v for k, v in rejection_counts.items() if v > 0}
            if fired:
                logger.info("   Rejections: " + " | ".join(f"{k}={v}" for k, v in fired.items()))

            logger.info(
                f"💤 Next scan: tomorrow 18:30 | sleeping "
                f"{sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
            )
            logger.info("=" * 70)

            time.sleep(min(sleep_secs, 3600))

        except Exception:
            logger.exception("❌ CRITICAL EOD SCAN ERROR — will retry next cycle")
            time.sleep(300)   # brief pause before retrying the outer loop

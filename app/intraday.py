# =====================================================================================
# app/intraday.py (FIXED)
# EARLY MOMENTUM SCANNER — 15M BARS
#
# FIXES APPLIED:
# 1. Wrap infinite loop in start() function instead of module-level code
# 2. Batch yf.download calls to reduce API requests from ~200 per scan to ~5-10
# 3. Import centralized thresholds from config.py instead of hardcoding
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

# FIX #3: Import config centrally instead of hardcoding
from config import WATCHLIST_PATH, SCORE_THRESHOLDS, SCAN_CONFIG

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

IST        = ZoneInfo("Asia/Kolkata")
CHUNK_SIZE = 10   # Max stocks per Telegram message

# Import thresholds for this timeframe from centralized config
TIMEFRAME        = "15m"
MIN_SIGNALS      = SCAN_CONFIG["15m"]["MIN_SIGNALS"]
MIN_BODY_RATIO   = SCAN_CONFIG["15m"]["MIN_BODY_RATIO"]
MIN_CLOSE_POSITION = SCAN_CONFIG["15m"]["MIN_CLOSE_POSITION"]
MAX_UPPER_WICK   = SCAN_CONFIG["15m"]["MAX_UPPER_WICK"]
MIN_VOLUME_RATIO = SCAN_CONFIG["15m"]["MIN_VOLUME_RATIO"]
MIN_VOLUME_AVG   = SCAN_CONFIG["15m"]["MIN_VOLUME_AVG"]
MIN_RSI          = SCAN_CONFIG["15m"]["MIN_RSI"]
MAX_RSI          = SCAN_CONFIG["15m"]["MAX_RSI"]
MIN_SCORE        = SCORE_THRESHOLDS["15m"]

# =====================================================================================
# BATCH DATA DOWNLOAD — FIX #2
# Download all watchlist symbols in a single/few batches instead of 200 individual calls
# =====================================================================================

def fetch_watchlist_data(watchlist: pd.DataFrame, period: str = "5d", interval: str = "15m") -> dict:
    """
    Download OHLCV data for all watchlist symbols in batches.
    
    Returns dict: {symbol: DataFrame}
    
    This replaces 200 individual yf.download calls with ~5-10 batched calls.
    Reduces API rate-limiting risk by ~95%.
    """
    
    symbols = watchlist["Stock"].tolist()
    batch_size = 30  # Download 30 symbols at a time (yfinance handles this well)
    all_data = {}
    
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        tickers_str = " ".join([f"{sym}.NS" for sym in batch])
        
        logger.info(f"📥 Batch downloading {len(batch)} symbols ({i}–{min(i + batch_size, len(symbols))}/{len(symbols)})")
        
        try:
            # Download entire batch in ONE request.
            # group_by='ticker' locks the MultiIndex structure to (Ticker, OHLCV) so
            # xs(sym, level=0) is always correct regardless of yfinance version changes.
            batch_data = yf.download(
                tickers_str,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
                threads=False,
                group_by="ticker",
            )

            if batch_data is None or batch_data.empty:
                logger.warning(f"⚠️ Empty response for batch {i // batch_size + 1}")
                continue

            # Detect actual structure returned — don't trust len(batch).
            # If 29 of 30 tickers are suspended/delisted, yfinance returns a flat
            # DataFrame for the one survivor instead of a MultiIndex.
            if not isinstance(batch_data.columns, pd.MultiIndex):
                # Flat DataFrame — yfinance returned a single-ticker result.
                if len(batch) == 1:
                    # We only requested 1 ticker, so batch[0] is safely the correct stock.
                    sym = batch[0]
                    df  = batch_data.reset_index().copy()
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
                        # Try .NS form first (what we passed to yf.download), then bare sym
                        level0 = batch_data.columns.get_level_values(0)
                        key    = ns_sym if ns_sym in level0 else (sym if sym in level0 else None)
                        if key is None:
                            logger.warning(f"⚠️ Symbol not in batch response: {sym}")
                            continue
                        ticker_df      = batch_data[key].reset_index()
                        all_data[sym]  = ticker_df
                    except Exception as e:
                        logger.exception(f"❌ Slice error extracting {sym} from batch: {e}")

        except Exception as e:
            logger.exception(f"❌ Batch download failed (batch {i // batch_size + 1}): {e}")
    
    return all_data

# =====================================================================================
# MAIN SCANNING LOOP — NOW INSIDE start() FUNCTION (FIX #1)
# =====================================================================================

def start():
    """
    Main scanning loop. Wrapped in a function so it doesn't execute on import.
    Called from main.py via: intraday.start()
    """
    
    init_db()
    cleanup_old_alerts(days=7)
    
    while True:
        
        ist_now      = datetime.now(IST)
        current_time = ist_now.time()
        weekday      = ist_now.weekday()
        
        market_open  = dt_time(9, 32) <= current_time <= dt_time(15, 30)
        
        if weekday >= 5 or not market_open:
            logger.info("📅 Outside market hours | Sleeping 5 minutes")
            time.sleep(300)
            continue
        
        scan_start = datetime.now(IST)
        logger.info("=" * 80)
        logger.info(f"⚡ INTRADAY SCAN START | {scan_start.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 80)

        sleep_time = 300  # default; always defined before the try so except can use it safely
        try:
            try:
                watchlist = pd.read_parquet(WATCHLIST_PATH)
                logger.info(f"📋 Watchlist loaded | {len(watchlist)} stocks")
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
            
            # FIX #2: Batch download instead of 200 individual calls
            all_ticker_data = fetch_watchlist_data(watchlist, period="5d", interval="15m")
            logger.info(f"📥 Data downloaded for {len(all_ticker_data)}/{len(watchlist)} symbols")
            
            # Fetch delivery conviction (previous day)
            prev_delivery_map = fetch_previous_day_delivery()
            if prev_delivery_map:
                logger.info(f"📦 Previous-day delivery loaded | {len(prev_delivery_map)} symbols")
            else:
                logger.info("📦 Previous-day delivery unavailable — delivery scoring skipped this cycle")

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
            
            alerts_by_category = {}
            rejection_counts   = {
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
                "penny_stock":       0,   # FIX GAP 2: added — sub-₹50 gate was missing
                "rsi_range":         0,
                "rsi_not_rising":    0,
                "low_score":         0,
                "duplicate":         0,
            }
            total_alerts = 0

            # ── PER-STOCK PROCESSING ────────────────────────────────────────────────
            for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):

                symbol = "UNKNOWN"

                try:
                    symbol   = row["Stock"]
                    category = row["Category"]
                    sector   = row.get("Sector", None)

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

                    # ── FORMING CANDLE CHECK ─────────────────────────────────────────
                    datetime_col = next(
                        (c for c in ["Datetime", "Date", "index"] if c in ticker.columns),
                        None
                    )
                    if datetime_col is not None:
                        try:
                            raw_ts = pd.Timestamp(ticker.iloc[-1][datetime_col])
                            # Convert to IST before stripping tz — yfinance may return
                            # UTC or IST timestamps depending on version. Normalising to
                            # IST wall-clock before stripping ensures the naive comparison
                            # is always correct (UTC naive would be 5.5h behind IST naive).
                            if raw_ts.tzinfo is not None:
                                raw_ts = raw_ts.tz_convert("Asia/Kolkata")
                            candle_start = raw_ts.replace(tzinfo=None)
                            candle_end   = candle_start + pd.Timedelta(minutes=15)
                            now_naive    = datetime.now(IST).replace(tzinfo=None)
                            if now_naive < candle_end:
                                ticker = ticker.iloc[:-1].copy()
                                rejection_counts["forming_candle"] += 1
                        except Exception:
                            logger.exception(f"  ⚠️ Candle age check error {symbol}")

                    if len(ticker) < 26:
                        rejection_counts["insufficient_bars"] += 1
                        continue

                    # ── INDICATORS ──────────────────────────────────────────────────
                    ticker = apply_indicators(ticker, timeframe=TIMEFRAME)

                    if ticker is None or ticker.empty:
                        rejection_counts["indicator_fail"] += 1
                        continue

                    # ── BREAKOUT SIGNALS ────────────────────────────────────────────
                    signals = detect_breakouts(ticker, timeframe=TIMEFRAME)

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

                    # ── FILTER 1: CANDLE BODY ────────────────────────────────────────
                    if body_ratio < MIN_BODY_RATIO:
                        rejection_counts["weak_body"] += 1
                        continue

                    # ── FILTER 2: BULLISH CANDLE ─────────────────────────────────────
                    if candle_close <= candle_open:
                        rejection_counts["bearish_candle"] += 1
                        continue

                    # ── FILTER 3: CLOSE POSITION ─────────────────────────────────────
                    if close_position < MIN_CLOSE_POSITION:
                        rejection_counts["weak_close_pos"] += 1
                        continue

                    # ── FILTER 4: UPPER WICK ─────────────────────────────────────────
                    if wick_ratio > MAX_UPPER_WICK:
                        rejection_counts["upper_wick"] += 1
                        continue

                    # ── FILTER 5: VOLUME RATIO ───────────────────────────────────────
                    if volume_ratio < MIN_VOLUME_RATIO:
                        rejection_counts["low_volume"] += 1
                        continue

                    # ── FILTER 6: AVG VOLUME FLOOR ───────────────────────────────────
                    if avg_volume < MIN_VOLUME_AVG:
                        rejection_counts["low_avg_volume"] += 1
                        continue

                    # ── FILTER 6B: MINIMUM PRICE (penny stock gate) ──────────────────
                    # FIX GAP 2: live_scanner and eod_scanner both hard-reject stocks
                    # below ₹50. intraday.py was missing this gate entirely — a ₹12
                    # micro-cap could pass all 15m filters and fire an alert.
                    # The ₹50 floor matches daily_builder's MIN_PRICE and both other
                    # scanners, keeping the universe consistent across all timeframes.
                    if candle_close < 50.0:
                        rejection_counts["penny_stock"] += 1
                        continue

                    # ── FILTER 7: RSI RANGE ──────────────────────────────────────────
                    if not (MIN_RSI <= rsi_val <= MAX_RSI):
                        rejection_counts["rsi_range"] += 1
                        continue

                    # ── FILTER 8: RSI DIRECTION ──────────────────────────────────────
                    RSI_LOOKBACK_BARS = 3
                    if len(ticker) > RSI_LOOKBACK_BARS:
                        rsi_prev = float(ticker["RSI"].iloc[-1 - RSI_LOOKBACK_BARS])
                        if rsi_val <= rsi_prev:
                            rejection_counts["rsi_not_rising"] += 1
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
                        timeframe=TIMEFRAME,
                        delivery_pct=delivery_pct,
                    )

                    if score > 0:
                        # ISOLATED TRY/EXCEPT: a sector error will NOT kill the alert
                        try:
                            safe_sector  = str(sector) if sector else "Unknown"
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
                    breakout_type = ", ".join(signals.keys() if isinstance(signals, dict) else signals)
                    today_str     = datetime.now(IST).strftime("%Y-%m-%d")
                    dedup_key     = f"{breakout_type}|{today_str}|INTRADAY"

                    saved = save_alert_if_new(
                        symbol,
                        dedup_key,
                        datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
                    )

                    if not saved:
                        rejection_counts["duplicate"] += 1
                        continue

                    # ── BUILD ALERT PAYLOAD ──────────────────────────────────────────
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
                        "above_sma50":      bool(candle_close >= float(latest["SMA50"])) if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")) else None,
                        "golden_cross":     bool(float(latest["SMA50"]) >= float(latest["SMA200"])) if "SMA50" in ticker.columns and "SMA200" in ticker.columns and not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200")) else None,
                    })
                    total_alerts += 1

                except Exception:
                    logger.exception(f"❌ UNHANDLED ERROR processing {symbol}")
            
            # ── SEND ALERTS ──────────────────────────────────────────────────────────
            scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            
            if total_alerts == 0:
                logger.info("📭 No INTRADAY alerts this cycle")
            else:
                for cat in sorted(alerts_by_category.keys()):
                    cat_alerts = sorted(
                        alerts_by_category[cat],
                        key=lambda x: x["score"],
                        reverse=True
                    )
                    chunks = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]
                    
                    for chunk_num, chunk in enumerate(chunks, 1):
                        msg = build_message("INTRADAY", cat, chunk, chunk_num, len(chunks), scan_time)
                        send_telegram_message(msg, scan_type="INTRADAY")
                        logger.info(f"📨 Sent | {cat} | chunk {chunk_num}/{len(chunks)} | {len(chunk)} stocks")

            # ── SCAN SUMMARY ─────────────────────────────────────────────────────────
            duration = (datetime.now(IST) - scan_start).total_seconds()

            logger.info("=" * 80)
            logger.info(f"✅ INTRADAY SCAN COMPLETE | {round(duration, 2)}s | Alerts={total_alerts}/{len(watchlist)}")

            fired = {k: v for k, v in rejection_counts.items() if v > 0}
            if fired:
                logger.info("   Rejections: " + " | ".join(f"{k}={v}" for k, v in fired.items()))

            # Dynamic sleep: keep cycle cadence at exactly 300s regardless of scan duration.
            # Without this, a 45s scan causes the loop to fire every 345s, drifting 45s
            # per cycle — a full 5-minute lag accumulates over a 6-hour trading day.
            elapsed     = (datetime.now(IST) - scan_start).total_seconds()
            sleep_time  = max(0, 300 - elapsed)
            logger.info(f"💤 Scan took {elapsed:.1f}s — sleeping {sleep_time:.1f}s to hit 5-min cadence")
            logger.info("=" * 80)

        except Exception:
            logger.exception("❌ CRITICAL SCAN ERROR")
            elapsed    = (datetime.now(IST) - scan_start).total_seconds()
            sleep_time = max(0, 300 - elapsed)

        time.sleep(sleep_time)

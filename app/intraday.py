# =====================================================================================
# app/intraday.py (ULTIMATE EDITION)
# EARLY MOMENTUM SCANNER — 15M BARS + MULTI-TIMEFRAME ALIGNMENT + MORNING FILTER
# =====================================================================================

import pandas as pd
import yfinance as yf
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from zoneinfo import ZoneInfo
from datetime import datetime, date, time as dt_time

from technical_indicators import apply_indicators
from breakout_engine import detect_breakouts
from scoring_engine import calculate_score
from telegram_engine import send_telegram_message
from message_formatter import build_message
from database import init_db, save_alert_if_new, cleanup_old_alerts
from delivery_data import fetch_previous_day_delivery
from price_cache import fetch_watchlist_data  
from sl_target_helper import compute_sl_and_target

from sector_rotation import get_sector_scores  

from config import (
    WATCHLIST_PATH,
    SCORE_THRESHOLDS,
    SCAN_CONFIG,
    DEDUP_DAYS,
    ADX_MIN_THRESHOLD,   
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

IST        = ZoneInfo("Asia/Kolkata")
CHUNK_SIZE = 10   

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

MIN_STOCK_PRICE   = 50.0
RSI_LOOKBACK_BARS = 5      
MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0
MAX_SINGLE_BAR_MOVE_PCT        = 6.0
MAX_GAP_FROM_PRIOR_HIGH_PCT = 3.0   
GAP_LOOKBACK_BARS           = 10    


def start():
    init_db()
    cleanup_old_alerts(days=DEDUP_DAYS)

    prev_delivery_map    = fetch_previous_day_delivery()
    _delivery_fetch_date = datetime.now(IST).date()
    if prev_delivery_map:
        logger.info(f"📦 Previous-day delivery loaded | {len(prev_delivery_map)} symbols")

    while True:
        ist_now      = datetime.now(IST)
        current_time = ist_now.time()
        weekday      = ist_now.weekday()
        
        market_open  = dt_time(9, 32) <= current_time <= dt_time(15, 35)
        
        if weekday >= 5 or not market_open:
            logger.info("📅 Outside market hours | Sleeping 5 minutes")
            time.sleep(300)
            continue

        # ✅ FIX: Macro regime check removed — alerts fire irrespective of market trend.
        
        scan_start = datetime.now(IST)
        logger.info("=" * 80)
        logger.info(f"⚡ INTRADAY SCAN START | {scan_start.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 80)

        if datetime.now(IST).date() != _delivery_fetch_date:
            prev_delivery_map    = fetch_previous_day_delivery()
            _delivery_fetch_date = datetime.now(IST).date()

        sleep_time = 300  
        try:
            try:
                watchlist = pd.read_parquet(WATCHLIST_PATH)
            except Exception:
                try:
                    from daily_builder import build_watchlist
                    build_watchlist()
                    watchlist = pd.read_parquet(WATCHLIST_PATH)
                except Exception:
                    time.sleep(300)
                    continue
            
            # ── BATCH DOWNLOAD: INTRADAY + DAILY CONTEXT (MTA) ──────────────────────
            all_ticker_data = {}
            daily_context_data = {}
            
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_15m = pool.submit(fetch_watchlist_data, watchlist, "10d", "15m")
                future_1d  = pool.submit(fetch_watchlist_data, watchlist, "60d", "1d")
                all_ticker_data = future_15m.result()
                daily_context_data = future_1d.result()
                
            logger.info(f"📥 Data downloaded | 15m: {len(all_ticker_data)} | Daily: {len(daily_context_data)}")
            
            try:
                rotation_result = get_sector_scores()
            except Exception:
                from sector_rotation import SectorRotationResult
                rotation_result = SectorRotationResult({}, set(), set(), "", date.today(), 0.0)
            
            alerts_by_category = {}
            rejection_counts   = {k: 0 for k in [
                "no_data", "missing_col", "forming_candle_stripped", "insufficient_bars", 
                "indicator_fail", "weak_signals", "weak_body", "bearish_candle", 
                "weak_close_pos", "upper_wick", "low_volume", "low_avg_volume", 
                "penny_stock", "rsi_range", "rsi_not_rising", "weak_adx", "below_ema20", 
                "below_sma50", "no_golden_cross", "macd_bearish", "far_from_52w_high", 
                "gap_bar", "extended_breakout", "below_daily_ema20", "low_score", "duplicate", "stale_data"
            ]}
            total_alerts = 0

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

                    if isinstance(ticker.columns, pd.MultiIndex):
                        ticker.columns = ticker.columns.get_level_values(0)

                    ticker = ticker.loc[:, ~ticker.columns.duplicated()]

                    required_cols = ["Open", "High", "Low", "Close", "Volume"]
                    missing_col   = False

                    for col_name in required_cols:
                        if col_name not in ticker.columns:
                            missing_col = True
                            break
                        if isinstance(ticker[col_name], pd.DataFrame):
                            ticker[col_name] = ticker[col_name].iloc[:, 0]
                        ticker[col_name] = pd.Series(ticker[col_name]).astype(float)

                    if missing_col:
                        rejection_counts["missing_col"] += 1
                        continue

                    ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

                    if ticker.empty:
                        rejection_counts["no_data"] += 1
                        continue

                    datetime_col = next((c for c in ["Datetime", "Date", "index"] if c in ticker.columns), None)
                    if datetime_col is not None:
                        try:
                            raw_ts = pd.Timestamp(ticker.iloc[-1][datetime_col])
                            if raw_ts.tzinfo is not None:
                                raw_ts = raw_ts.tz_convert("Asia/Kolkata")
                            candle_start = raw_ts.replace(tzinfo=None)
                            candle_end   = candle_start + pd.Timedelta(minutes=15)
                            now_naive    = datetime.now(IST).replace(tzinfo=None)
                            if now_naive < candle_end:
                                ticker = ticker.iloc[:-1].copy()
                                rejection_counts["forming_candle_stripped"] += 1
                        except Exception:
                            pass

                    if len(ticker) < 105:
                        rejection_counts["insufficient_bars"] += 1
                        continue

                    ticker = apply_indicators(ticker, timeframe=TIMEFRAME)

                    if ticker is None or ticker.empty:
                        rejection_counts["indicator_fail"] += 1
                        continue

                    signals = detect_breakouts(ticker, timeframe=TIMEFRAME)

                    if len(signals) < MIN_SIGNALS:
                        rejection_counts["weak_signals"] += 1
                        continue

                    latest = ticker.iloc[-1]

                    if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):
                        continue

                    _stale_col = next((c for c in ["Datetime", "Date"] if c in ticker.columns), None)
                    if _stale_col:
                        try:
                            _last_ts = pd.to_datetime(latest[_stale_col])
                            if _last_ts.tzinfo is not None:
                                _last_ts = _last_ts.tz_convert("Asia/Kolkata")
                            if _last_ts.date() != ist_now.date():
                                rejection_counts["stale_data"] += 1
                                continue
                        except Exception:
                            pass

                    latest_volume = float(latest["Volume"])
                    avg_volume    = float(ticker["Volume"].iloc[-21:-1].mean())

                    if avg_volume <= 0:
                        continue

                    volume_ratio = latest_volume / avg_volume

                    candle_high  = float(latest["High"])
                    candle_low   = float(latest["Low"])
                    candle_open  = float(latest["Open"])
                    candle_close = float(latest["Close"])
                    candle_range = candle_high - candle_low
                    candle_body  = abs(candle_close - candle_open)
                    upper_wick   = candle_high - candle_close

                    if candle_range <= 0:
                        continue

                    body_ratio     = candle_body / candle_range
                    close_position = (candle_close - candle_low) / candle_range
                    wick_ratio     = upper_wick / candle_range
                    rsi_val        = float(latest["RSI"])

                    if body_ratio < MIN_BODY_RATIO:
                        rejection_counts["weak_body"] += 1
                        continue
                    if candle_close <= candle_open:
                        rejection_counts["bearish_candle"] += 1
                        continue
                    if close_position < MIN_CLOSE_POSITION:
                        rejection_counts["weak_close_pos"] += 1
                        continue
                    if wick_ratio > MAX_UPPER_WICK:
                        rejection_counts["upper_wick"] += 1
                        continue
                        
                    # ── DYNAMIC MORNING VOLATILITY FILTER ────────────────────────────────────
                    is_morning_rush = current_time < dt_time(10, 0)
                    required_vol_ratio = 4.0 if is_morning_rush else MIN_VOLUME_RATIO
                    
                    if volume_ratio < required_vol_ratio:
                        rejection_counts["low_volume"] += 1
                        continue
                    # ────────────────────────────────────────────────────────────────────────

                    if avg_volume < MIN_VOLUME_AVG:
                        rejection_counts["low_avg_volume"] += 1
                        continue
                    if candle_close < MIN_STOCK_PRICE:
                        rejection_counts["penny_stock"] += 1
                        continue
                    if not (MIN_RSI <= rsi_val <= MAX_RSI):
                        rejection_counts["rsi_range"] += 1
                        continue

                    if len(ticker) > RSI_LOOKBACK_BARS:
                        rsi_prev = float(ticker["RSI"].iloc[-1 - RSI_LOOKBACK_BARS])
                        if rsi_val <= rsi_prev:
                            rejection_counts["rsi_not_rising"] += 1
                            continue

                    if "ADX" in ticker.columns and not pd.isna(latest.get("ADX")):
                        if float(latest["ADX"]) < ADX_MIN_THRESHOLD:
                            rejection_counts["weak_adx"] += 1
                            continue

                    if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")):
                        if candle_close < float(latest["EMA20"]):
                            rejection_counts["below_ema20"] += 1
                            continue

                    if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")):
                        if candle_close < float(latest["SMA50"]):
                            rejection_counts["below_sma50"] += 1
                            continue

                    if (
                        "SMA50" in ticker.columns and "SMA200" in ticker.columns and
                        not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200"))
                    ):
                        if float(latest["SMA50"]) < float(latest["SMA200"]):
                            rejection_counts["no_golden_cross"] += 1
                            continue

                    if (
                        "MACD" in ticker.columns and "MACD_SIGNAL" in ticker.columns and
                        not pd.isna(latest.get("MACD")) and not pd.isna(latest.get("MACD_SIGNAL"))
                    ):
                        if float(latest["MACD"]) < float(latest["MACD_SIGNAL"]):
                            rejection_counts["macd_bearish"] += 1
                            continue

                    # ── MULTI-TIMEFRAME ALIGNMENT (MTA) ─────────────────────────────────────
                    if symbol in daily_context_data and not daily_context_data[symbol].empty:
                        daily_df = daily_context_data[symbol].copy()
                        if len(daily_df) >= 20:
                            daily_df["EMA20_D"] = daily_df["Close"].ewm(span=20, adjust=False).mean()
                            latest_daily_close = float(daily_df["Close"].iloc[-1])
                            latest_daily_ema20 = float(daily_df["EMA20_D"].iloc[-1])
                            
                            if latest_daily_close < latest_daily_ema20:
                                rejection_counts["below_daily_ema20"] += 1
                                continue
                    # ────────────────────────────────────────────────────────────────────────

                    if "HIGH_52W" in ticker.columns and not pd.isna(latest.get("HIGH_52W")):
                        high_52w = float(latest["HIGH_52W"])
                        if high_52w > 0:
                            pct_from_high = (high_52w - candle_close) / high_52w * 100
                            if pct_from_high > MAX_DISTANCE_FROM_52W_HIGH_PCT:
                                rejection_counts["far_from_52w_high"] += 1
                                continue

                    if len(ticker) >= 2:
                        prev_close = float(ticker["Close"].iloc[-2])
                        if prev_close > 0:
                            single_move_pct = abs(candle_close - prev_close) / prev_close * 100
                            if single_move_pct > MAX_SINGLE_BAR_MOVE_PCT:
                                rejection_counts["gap_bar"] += 1
                                continue

                    if len(ticker) >= GAP_LOOKBACK_BARS + 1:
                        prior_high = float(ticker["High"].iloc[-(GAP_LOOKBACK_BARS + 1):-1].max())
                        if prior_high > 0:
                            gap_pct = (candle_open - prior_high) / prior_high * 100
                            if gap_pct > MAX_GAP_FROM_PRIOR_HIGH_PCT:
                                rejection_counts["extended_breakout"] += 1
                                continue

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
                        min_vol=MIN_VOLUME_AVG,
                    )

                    if score > 0:
                        try:
                            safe_sector  = "Unknown" if (sector is None or (isinstance(sector, float) and pd.isna(sector))) else str(sector).strip()
                            sector_bonus = rotation_result.score_bonus_for(safe_sector)
                            score = max(0, min(score + sector_bonus, 100))
                        except Exception:
                            pass

                    if score < MIN_SCORE:
                        rejection_counts["low_score"] += 1
                        continue

                    signal_str = ", ".join(signals.keys() if isinstance(signals, dict) else signals)
                    today_str  = datetime.now(IST).strftime("%Y-%m-%d")
                    dedup_key  = f"{category}|{signal_str}|{today_str}|INTRADAY"

                    # ── Dynamic S/R and Indicator-based SL + Target ──────────────
                    current_atr = float(latest["ATR"]) if "ATR" in ticker.columns and not pd.isna(latest.get("ATR")) else None
                    sl_result = compute_sl_and_target(
                        entry_price=candle_close,
                        atr=current_atr,
                        candle_range=candle_range,
                        timeframe="INTRADAY",
                        adx=latest.get("ADX") if "ADX" in latest else None,
                        rsi=rsi_val,
                        macd_hist=latest.get("MACD_HIST") if "MACD_HIST" in latest else None,
                        atr_pct=latest.get("ATR_PCT") if "ATR_PCT" in latest else None,
                        swing_low=latest.get("SWING_LOW") if "SWING_LOW" in latest else None,
                        swing_high=latest.get("SWING_HIGH") if "SWING_HIGH" in latest else None,
                        bb_upper=latest.get("BB_UPPER") if "BB_UPPER" in latest else None,
                        bb_lower=latest.get("BB_LOWER") if "BB_LOWER" in latest else None,
                        s1=latest.get("S1") if "S1" in latest else None,
                        s2=latest.get("S2") if "S2" in latest else None,
                        r1=latest.get("R1") if "R1" in latest else None,
                        r2=latest.get("R2") if "R2" in latest else None,
                    )
                    suggested_stop = sl_result["stop_loss"]
                    target_price = sl_result["target_1"]

                    saved = save_alert_if_new(
                        symbol,
                        dedup_key,
                        datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                        scanner="INTRADAY",
                        category=category,
                        entry_price=round(candle_close, 2),
                        signals=signal_str,
                        score=score,
                        rsi=round(float(latest["RSI"]), 1),
                        volume_ratio=round(volume_ratio, 2),
                        stop_loss=suggested_stop,
                        target_price=target_price,
                    )
                    if not saved:
                        rejection_counts["duplicate"] += 1
                        continue

                    above_ema20 = bool(candle_close >= float(latest["EMA20"])) if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")) else None
                    above_sma50 = bool(candle_close >= float(latest["SMA50"])) if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")) else None
                    golden_cross = bool(float(latest["SMA50"]) >= float(latest["SMA200"])) if "SMA50" in ticker.columns and "SMA200" in ticker.columns and not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200")) else None

                    alerts_by_category.setdefault(category, []).append({
                        "symbol":           symbol,
                        "category":         category,
                        "breakout_signals": list(signals.keys()) if isinstance(signals, dict) else signals,
                        "price":            round(candle_close, 2),
                        "open":             round(float(latest["Open"]), 2),
                        "day_high":         round(float(latest["High"]), 2),
                        "day_low":          round(float(latest["Low"]), 2),
                        "rsi":              round(float(latest["RSI"]), 1),
                        "volume_ratio":     round(volume_ratio, 2),
                        "body_ratio":       round(body_ratio, 1),
                        "score":            score,
                        "above_ema20":      above_ema20,
                        "above_sma50":      above_sma50,
                        "golden_cross":     golden_cross,
                        "atr_stop":         suggested_stop,
                        "target_price":     target_price,
                        "target_2":         sl_result.get("target_2"),
                        "target_3":         sl_result.get("target_3"),
                        "sl_method":        sl_result.get("sl_method"),
                        "t_method":         sl_result.get("t_method"),
                        "rr_ratio":         sl_result.get("rr_ratio"),
                        "peg":              row.get("PEG Ratio"),
                        "yoy_rev":          row.get("YOY Revenue %"),
                        "yoy_profit":       row.get("YOY Profit %"),
                        "roe":              row.get("ROE %")
                    })
                    total_alerts += 1

                except Exception:
                    logger.exception(f"❌ UNHANDLED ERROR processing {symbol}")
            
            scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            
            if total_alerts == 0:
                logger.info("📭 No INTRADAY alerts this cycle")
            else:
                for cat in sorted(alerts_by_category.keys()):
                    cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)
                    chunks = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]
                    for chunk_num, chunk in enumerate(chunks, 1):
                        msg = build_message("INTRADAY", cat, chunk, chunk_num, len(chunks), scan_time)
                        send_telegram_message(msg, scan_type="INTRADAY")

            duration = (datetime.now(IST) - scan_start).total_seconds()
            logger.info("=" * 80)
            logger.info(f"✅ INTRADAY SCAN COMPLETE | {round(duration, 2)}s | Alerts={total_alerts}/{len(watchlist)}")

            fired = {k: v for k, v in rejection_counts.items() if v > 0}
            if fired:
                logger.info("   Rejections: " + " | ".join(f"{k}={v}" for k, v in fired.items()))

            elapsed     = (datetime.now(IST) - scan_start).total_seconds()
            sleep_time  = max(0, 300 - elapsed)
            time.sleep(sleep_time)

        except Exception:
            logger.exception("❌ CRITICAL SCAN ERROR")
            elapsed    = (datetime.now(IST) - scan_start).total_seconds()
            time.sleep(max(0, 300 - elapsed))

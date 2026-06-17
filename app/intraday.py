# =====================================================================================
# app/intraday.py (ULTIMATE EDITION)
# EARLY MOMENTUM SCANNER — 15M BARS + MULTI-TIMEFRAME ALIGNMENT + MORNING FILTER
# =====================================================================================

import pandas as pd
# Ensure tzcache writable location before importing yfinance (robust import to support different cwd)
try:
    import app.yf_bootstrap
except Exception:
    try:
        import yf_bootstrap
    except Exception:
        pass
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
from database import init_db, save_alert_if_new, upsert_fetch_error
from delivery_data import fetch_previous_day_delivery
from price_cache import fetch_watchlist_data  
from sl_target_helper import compute_sl_and_target
from watchlist_cache import get_watchlist

from sector_rotation import get_sector_scores  

from config import (
    WATCHLIST_PATH,
    SCORE_THRESHOLDS,
    SCAN_CONFIG,
    ADX_MIN_THRESHOLD,
    MAX_PRE_BREAKOUT_RED_CANDLES,
    MIN_STOCK_PRICE,
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

# MIN_STOCK_PRICE imported from config (₹100)
RSI_LOOKBACK_BARS = 5      
MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0
MAX_SINGLE_BAR_MOVE_PCT        = 6.0
MAX_GAP_FROM_PRIOR_HIGH_PCT = 3.0   
GAP_LOOKBACK_BARS           = 10    


def start(run_once=False):
    init_db()

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
            watchlist = get_watchlist()
            if watchlist is None or watchlist.empty:
                raise ValueError("Watchlist is missing or empty. Cannot run scan.")
            
            # ── FETCH WEALTH ENGINE SIGNAL ──────────────────────────────────────────
            try:
                import os
                from config import DATA_DIR
                from database import download_parquet_from_db
                wealth_path = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
                download_parquet_from_db("elite_wealth_system", wealth_path)
                if os.path.exists(wealth_path):
                    wealth_df = pd.read_parquet(wealth_path)
                    if "Stock" in wealth_df.columns:
                        wealth_df = wealth_df.set_index("Stock")
                else:
                    wealth_df = pd.DataFrame()
            except Exception as e:
                logger.error(f"❌ Failed to load Wealth Engine data: {e}")
                wealth_df = pd.DataFrame()

            # ── BATCH DOWNLOAD: INTRADAY + DAILY CONTEXT (MTA) ──────────────────────
            all_ticker_data = {}
            daily_context_data = {}
            
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_15m = pool.submit(fetch_watchlist_data, watchlist, "10d", "15m")
                future_1d  = pool.submit(fetch_watchlist_data, watchlist, "60d", "1d")
                all_ticker_data = future_15m.result()
                daily_context_data = future_1d.result()

            if not all_ticker_data:
                raise Exception("YFinance returned 0 data. API might be down or rate-limited.")
                
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
                "gap_bar", "extended_breakout", "below_daily_ema20", "low_score", "duplicate", "stale_data",
                "prior_red_candles", "obv_divergence", "gap_fill_risk"
            ]}
            total_alerts = 0
            _last_ts = None  # Initialize before loop to prevent NameError

            for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):
                symbol = "UNKNOWN"
                try:
                    symbol   = row["Stock"]
                    category = row["Category"]
                    sector   = row.get("Sector", None)

                    if symbol not in all_ticker_data:
                        rejection_counts["no_data"] += 1
                        try:
                            upsert_fetch_error('yfinance', 'INTRADAY', symbol, '15m', 'no_data', 'missing_in_batch')
                        except Exception:
                            logger.exception('Failed to upsert fetch error')
                        continue

                    ticker = all_ticker_data[symbol].copy()

                    if ticker.empty:
                        rejection_counts["no_data"] += 1
                        try:
                            upsert_fetch_error('yfinance', 'INTRADAY', symbol, '15m', 'no_data', 'empty_dataframe')
                        except Exception:
                            logger.exception('Failed to upsert fetch error')
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

                        try:
                            if _last_ts is not None and _last_ts.date() != ist_now.date():
                                rejection_counts["stale_data"] += 1
                                try:
                                    upsert_fetch_error('yfinance', 'INTRADAY', symbol, '15m', 'stale_data', f'last_ts:{_last_ts.date()}')
                                except Exception:
                                    logger.exception('Failed to upsert fetch error')
                                continue
                        except Exception:
                            pass

                    if len(ticker) < 105:
                        rejection_counts["insufficient_bars"] += 1
                        continue

                    ticker = apply_indicators(ticker, timeframe=TIMEFRAME,
                                              daily_ohlc=daily_context_data.get(symbol))

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

                    # ADX floor is timeframe-aware: 15m bars naturally show much lower
                    # ADX than daily bars. 18 = directional movement on 15m timeframe.
                    if "ADX" in ticker.columns and not pd.isna(latest.get("ADX")):
                        if float(latest["ADX"]) < 18:
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

                    # NOTE: SMA200 on 15m bars is always NaN (need 200 bars, only
                    # have ~105 from 10 days). Golden cross is checked on the daily
                    # chart via daily_context_data in the MTA block below instead.
                    # The SMA50 check above IS valid on 15m (50 bars = ~2 trading days).

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

                    # ── v5: PREVIOUS CANDLE CONTEXT FILTER ─────────────────────────
                    # Reject if the 2 candles before breakout are BOTH bearish.
                    # Genuine breakouts build momentum; 2 red candles = fake pop.
                    if len(ticker) >= 3:
                        red_count = 0
                        for _ri in range(-3, -1):
                            if float(ticker["Close"].iloc[_ri]) < float(ticker["Open"].iloc[_ri]):
                                red_count += 1
                        if red_count >= MAX_PRE_BREAKOUT_RED_CANDLES:
                            rejection_counts["prior_red_candles"] += 1
                            continue

                    # ── v5: OBV DIVERGENCE FILTER ───────────────────────────────
                    # Reject if OBV trend is bearish (volume not confirming price).
                    # This is the #1 indicator of distribution masquerading as breakout.
                    if "OBV_TREND" in ticker.columns:
                        obv_trend = int(latest.get("OBV_TREND", 0) or 0)
                        if obv_trend == -1:
                            rejection_counts["obv_divergence"] += 1
                            continue

                    # ── v5: GAP-OPEN FILL RISK FILTER (INTRADAY ONLY) ───────────────
                    # If candle opened with gap > 1.5% above prior high AND has
                    # already filled 50%+ of the gap, it's a gap-fill trap.
                    if len(ticker) >= 2:
                        prev_high_gf = float(ticker["High"].iloc[-2])
                        if prev_high_gf > 0:
                            gap_open_pct = (candle_open - prev_high_gf) / prev_high_gf * 100
                            if gap_open_pct > 1.5:
                                gap_size = candle_open - prev_high_gf
                                gap_filled = candle_open - candle_low
                                if gap_size > 0 and gap_filled / gap_size > 0.50:
                                    rejection_counts["gap_fill_risk"] += 1
                                    continue

                    score, model_version = calculate_score(
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

                    # ── Dynamic S/R and Indicator-based SL + Target (INTRADAY mode) ──
                    current_atr = float(latest["ATR"]) if "ATR" in ticker.columns and not pd.isna(latest.get("ATR")) else None
                    sl_result = compute_sl_and_target(
                        entry_price=candle_close,
                        atr=current_atr,
                        candle_range=candle_range,
                        mode="INTRADAY",
                        adx=latest.get("ADX"),
                        rsi=rsi_val,
                        macd_hist=latest.get("MACD_HIST"),
                        atr_pct=latest.get("ATR_PCT"),
                        swing_low=latest.get("SWING_LOW"),
                        swing_high=latest.get("SWING_HIGH"),
                        bb_upper=latest.get("BB_UPPER"),
                        bb_lower=latest.get("BB_LOWER"),
                        bb_mid=latest.get("BB_MID"),
                        s1=latest.get("S1"),
                        s2=latest.get("S2"),
                        r1=latest.get("R1"),
                        r2=latest.get("R2"),
                        swing_low_raw=latest.get("SWING_LOW_RAW"),
                        swing_high_raw=latest.get("SWING_HIGH_RAW"),
                        candle_low=candle_low,
                        vwap=latest.get("VWAP"),
                    )
                    suggested_stop = sl_result["stop_loss"]
                    target_price   = sl_result["target_1"]

                    above_ema20 = bool(candle_close >= float(latest["EMA20"])) if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")) else None
                    above_sma50 = bool(candle_close >= float(latest["SMA50"])) if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")) else None
                    golden_cross = bool(float(latest["SMA50"]) >= float(latest["SMA200"])) if "SMA50" in ticker.columns and "SMA200" in ticker.columns and not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200")) else None

                    context = {
                        "technicals": {
                            "above_ema20":      above_ema20,
                            "above_sma50":      above_sma50,
                            "golden_cross":     golden_cross,
                            "body_ratio":       round(body_ratio, 2),
                            "delivery_pct":     round(delivery_pct, 1) if delivery_pct is not None else None,
                            "rsi":              round(rsi_val, 1),
                            "volume_ratio":     round(volume_ratio, 2)
                        },
                        "session": {
                            "open":             round(float(latest["Open"]), 2),
                            "day_high":         round(float(latest["High"]), 2),
                            "day_low":          round(float(latest["Low"]), 2)
                        },
                        "fundamentals": {
                            "peg":              row.get("PEG Ratio"),
                            "yoy_rev":          row.get("YOY Revenue %"),
                            "yoy_profit":       row.get("YOY Profit %"),
                            "roe":              row.get("ROE %")
                        },
                        "execution": {
                            "sl_method":        sl_result.get("sl_method"),
                            "t_method":         sl_result.get("t_method"),
                            "trail_note":       sl_result.get("trail_note")
                        }
                    }

                    saved, cap_alloc, shares = save_alert_if_new(
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
                        context=context,
                        model_version=model_version,
                    )
                    if not saved:
                        rejection_counts["duplicate"] += 1
                        continue

                    # Extract wealth signal for this stock
                    w_signal = None
                    w_bucket = None
                    if not wealth_df.empty and symbol in wealth_df.index:
                        w_signal = wealth_df.loc[symbol, "Signal"]
                        w_bucket = wealth_df.loc[symbol, "Portfolio_Bucket"]

                    alerts_by_category.setdefault(category, []).append({
                        "symbol":           symbol,
                        "wealth_signal":    w_signal,
                        "wealth_bucket":    w_bucket,
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
                        "trail_note":       sl_result.get("trail_note"),
                        "delivery_pct":     round(delivery_pct, 1) if delivery_pct is not None else None,
                        "peg":              row.get("PEG Ratio"),
                        "yoy_rev":          row.get("YOY Revenue %"),
                        "yoy_profit":       row.get("YOY Profit %"),
                        "roe":              row.get("ROE %"),
                        "capital_allocated": cap_alloc,
                        "shares_bought":     shares
                    })
                    total_alerts += 1

                except Exception as e:
                    logger.exception(f"❌ UNHANDLED ERROR processing {symbol}")
                    rejection_counts["indicator_fail"] = rejection_counts.get("indicator_fail", 0) + 1
                    try:
                        upsert_fetch_error('yfinance', 'INTRADAY', symbol, '15m', 'processing_error', str(e))
                    except Exception:
                        logger.exception(f'Failed to upsert fetch error for {symbol}')
                    continue
            
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
            
            # ✅ CRITICAL: Verify alerts were actually saved to database (2026-06-17)
            from database import upsert_scanner_health, verify_alerts_saved_today
            if total_alerts > 0:
                if not verify_alerts_saved_today("INTRADAY", total_alerts):
                    logger.critical(f"🚨 CRITICAL ERROR: Intraday generated {total_alerts} alerts but save failed!")
                    upsert_scanner_health(
                        scanner_name="INTRADAY",
                        status="DOWN",
                        error_msg=f"CRITICAL: {total_alerts} alerts failed to save to database"
                    )
                    raise RuntimeError("Alert save verification failed - database connectivity issue")
            
            try:
                upsert_scanner_health(
                    scanner_name="INTRADAY",
                    status="OK",
                    last_success=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                    today_alerts=total_alerts
                )
            except Exception:
                logger.exception("❌ Failed to update scanner health for INTRADAY")

            fired = {k: v for k, v in rejection_counts.items() if v > 0}
            if fired:
                logger.info("   Rejections: " + " | ".join(f"{k}={v}" for k, v in fired.items()))


            if run_once:
                logger.info("🧪 TEST RUN COMPLETE. Exiting loop.")
                break
            
            elapsed     = (datetime.now(IST) - scan_start).total_seconds()

            sleep_time  = max(0, 300 - elapsed)
            time.sleep(sleep_time)

        except Exception as e:
            if isinstance(e, RuntimeError) and "interpreter shutdown" in str(e).lower():
                logger.info("Interpreter shutting down, ignoring INTRADAY scan future error.")
                break
            logger.exception("❌ CRITICAL SCAN ERROR")
            try:
                from database import upsert_scanner_health
                upsert_scanner_health("INTRADAY", "DOWN", error_msg=str(e))
            except Exception:
                pass
            elapsed    = (datetime.now(IST) - scan_start).total_seconds()
            time.sleep(max(0, 300 - elapsed))

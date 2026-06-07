# =====================================================================================
# app/eod_scanner.py
# EOD BREAKOUT SCANNER WITH CONSOLIDATED MAIL AUTOMATION
# =====================================================================================

import pandas as pd
import yfinance as yf
import time
import logging
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed

from zoneinfo import ZoneInfo
from datetime import datetime, date, time as dt_time, timedelta

from technical_indicators import apply_indicators
from breakout_engine import detect_breakouts[cite: 1]
from scoring_engine import calculate_score[cite: 6]
from telegram_engine import send_telegram_message[cite: 6]
from message_formatter import build_message[cite: 6]
# Modified database functions for clean state execution[cite: 4]
from database import init_db, save_alert_if_new, cleanup_old_alerts[cite: 4, 6]
from delivery_data import fetch_delivery_data[cite: 6]

from sector_rotation import get_sector_scores[cite: 6]

from config import (
    WATCHLIST_PATH,[cite: 6]
    SCORE_THRESHOLDS,[cite: 6]
    SCAN_CONFIG,[cite: 6]
    BATCH_DOWNLOAD_SIZE,[cite: 6]
    DEDUP_DAYS,[cite: 6]
    ADX_MIN_THRESHOLD,  [cite: 6]
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

IST        = ZoneInfo("Asia/Kolkata")[cite: 6]
EOD_START  = dt_time(18, 30)  [cite: 6]
EOD_END    = dt_time(20, 0)   [cite: 6]
CHUNK_SIZE = 10[cite: 6]

DELIVERY_FETCH_RETRIES    = 5[cite: 6]
DELIVERY_RETRY_INTERVAL_S = 600  [cite: 6]

TIMEFRAME               = "1d"[cite: 6]
MIN_SIGNALS             = SCAN_CONFIG["1d"]["MIN_SIGNALS"][cite: 6]
MIN_BODY_RATIO          = SCAN_CONFIG["1d"]["MIN_BODY_RATIO"][cite: 6]
MIN_CLOSE_POSITION      = SCAN_CONFIG["1d"]["MIN_CLOSE_POSITION"][cite: 6]
MAX_UPPER_WICK_RATIO    = SCAN_CONFIG["1d"]["MAX_UPPER_WICK"][cite: 6]
MIN_VOLUME_RATIO        = SCAN_CONFIG["1d"]["MIN_VOLUME_RATIO"]     [cite: 6]
MIN_AVG_VOLUME_SHARES   = SCAN_CONFIG["1d"]["MIN_VOLUME_AVG"]       [cite: 6]
MIN_RSI                 = SCAN_CONFIG["1d"]["MIN_RSI"]              [cite: 6]
MAX_RSI                 = SCAN_CONFIG["1d"]["MAX_RSI"]              [cite: 6]
MIN_SCORE               = SCORE_THRESHOLDS["1d"]                    [cite: 6]

MIN_STOCK_PRICE             = 50.0[cite: 6]
RSI_LOOKBACK_BARS           = 5     [cite: 6]
MAX_DISTANCE_FROM_52W_HIGH_PCT = 15.0[cite: 6]
MAX_SINGLE_DAY_MOVE_PCT     = 15.0[cite: 6]
MAX_GAP_FROM_PRIOR_HIGH_PCT = 3.0  [cite: 6]
GAP_LOOKBACK_BARS           = 10   [cite: 6]


def seconds_until_eod() -> int:[cite: 6]
    now          = datetime.now(IST)[cite: 6]
    target_today = now.replace(hour=18, minute=30, second=0, microsecond=0)[cite: 6]
    if now < target_today:[cite: 6]
        delta = target_today - now[cite: 6]
    else:[cite: 6]
        delta = target_today + timedelta(days=1) - now[cite: 6]
    return max(int(delta.total_seconds()), 0)[cite: 6]


def fetch_watchlist_data([cite: 6]
    watchlist: pd.DataFrame,[cite: 6]
    period: str = "2y",[cite: 6]
    interval: str = "1d"[cite: 6]
) -> dict[str, pd.DataFrame]:[cite: 6]
    symbols    = watchlist["Stock"].tolist()[cite: 6]
    all_data   = {}[cite: 6]
    total      = len(symbols)[cite: 6]
    batch_size = BATCH_DOWNLOAD_SIZE[cite: 6]

    for i in range(0, total, batch_size):[cite: 6]
        batch       = symbols[i : i + batch_size][cite: 6]
        tickers_str = " ".join(f"{sym}.NS" for sym in batch)[cite: 6]
        batch_end   = min(i + batch_size, total)[cite: 6]

        logger.info(f"📥 Batch downloading {len(batch)} symbols ({i}–{batch_end}/{total})")[cite: 6]

        try:[cite: 6]
            raw = yf.download([cite: 6]
                tickers_str,[cite: 6]
                period=period,[cite: 6]
                interval=interval,[cite: 6]
                progress=False,[cite: 6]
                auto_adjust=True,[cite: 6]
                threads=False,[cite: 6]
                group_by="ticker",[cite: 6]
            )

            if raw is None or raw.empty:[cite: 6]
                continue[cite: 6]

            if not isinstance(raw.columns, pd.MultiIndex):[cite: 6]
                if len(batch) == 1:[cite: 6]
                    sym = batch[0][cite: 6]
                    df  = raw.reset_index().copy()[cite: 6]
                    if not df.empty:[cite: 6]
                        all_data[sym] = df[cite: 6]
                else:[cite: 6]
                    continue[cite: 6]
            else:[cite: 6]
                for sym in batch:[cite: 6]
                    ns_sym = f"{sym}.NS"[cite: 6]
                    try:[cite: 6]
                        level0 = raw.columns.get_level_values(0)[cite: 6]
                        key    = ns_sym if ns_sym in level0 else (sym if sym in level0 else None)[cite: 6]
                        if key is None:[cite: 6]
                            continue[cite: 6]
                        df = raw[key].reset_index().copy()[cite: 6]
                        if not df.empty:[cite: 6]
                            all_data[sym] = df[cite: 6]
                    except Exception:[cite: 6]
                        pass[cite: 6]
        except Exception:[cite: 6]
            logger.exception(f"❌ Batch download failed")[cite: 6]

    return all_data[cite: 6]


def start():[cite: 6]
    init_db()[cite: 6]
    cleanup_old_alerts(days=DEDUP_DAYS)[cite: 6]
    logger.info(f"✅ EOD scanner ready | DB initialized")[cite: 6]

    last_scan_date = None[cite: 6]
    last_rebuild_week: str = ""[cite: 6]

    while True:[cite: 6]
        ist_now      = datetime.now(IST)[cite: 6]
        current_time = ist_now.time()[cite: 6]
        weekday      = ist_now.weekday()[cite: 6]
        today_str    = ist_now.strftime("%Y-%m-%d")[cite: 6]

        in_eod_window = EOD_START <= current_time <= EOD_END[cite: 6]
        is_weekday    = weekday < 5[cite: 6]
        already_ran   = (last_scan_date == today_str)[cite: 6]

        if not is_weekday:[cite: 6]
            current_week = ist_now.strftime("%G-W%V")[cite: 6]
            is_sunday    = (weekday == 6)[cite: 6]
            in_rebuild_window = dt_time(19, 0) <= current_time <= dt_time(19, 30)[cite: 6]

            if is_sunday and in_rebuild_window and last_rebuild_week != current_week:[cite: 6]
                try:[cite: 6]
                    from daily_builder import build_watchlist[cite: 3, 6]
                    build_watchlist()[cite: 3, 6]
                    last_rebuild_week = current_week[cite: 6]
                except Exception:[cite: 6]
                    logger.exception(f"❌ Weekly rebuild failed")[cite: 6]

            sleep_secs = seconds_until_eod()[cite: 6]
            time.sleep(min(sleep_secs, 3600))[cite: 6]
            continue[cite: 6]

        if already_ran:[cite: 6]
            sleep_secs = seconds_until_eod()[cite: 6]
            time.sleep(min(sleep_secs, 3600))[cite: 6]
            continue[cite: 6]

        if not in_eod_window:[cite: 6]
            sleep_secs = seconds_until_eod()[cite: 6]
            time.sleep(min(sleep_secs, 60))[cite: 6]
            continue[cite: 6]

        from market_filter import is_market_regime_bullish
        if not is_market_regime_bullish():
            logger.info("🛑 Bearish EOD macro regime. Skipping daily breakout generation to avoid traps.")[cite: 6]
            last_scan_date = today_str[cite: 6]
            sleep_secs = seconds_until_eod()[cite: 6]
            time.sleep(min(sleep_secs, 3600))[cite: 6]
            continue[cite: 6]

        logger.info("=" * 70)[cite: 6]
        logger.info(f"📊 EOD SCAN | {ist_now.strftime('%Y-%m-%d %H:%M:%S IST')}")[cite: 6]
        logger.info("=" * 70)[cite: 6]

        scan_start = datetime.now(IST)[cite: 6]
        try:[cite: 6]
            try:[cite: 6]
                watchlist = pd.read_parquet(WATCHLIST_PATH)[cite: 6]
                logger.info(f"📋 Watchlist | {len(watchlist)} stocks")[cite: 6]
            except Exception:[cite: 6]
                try:[cite: 6]
                    from daily_builder import build_watchlist[cite: 3, 6]
                    build_watchlist()[cite: 3, 6]
                    watchlist = pd.read_parquet(WATCHLIST_PATH)[cite: 6]
                except Exception:[cite: 6]
                    time.sleep(300)[cite: 6]
                    continue[cite: 6]

            def _fetch_delivery_with_retry() -> dict:[cite: 6]
                for attempt in range(1, DELIVERY_FETCH_RETRIES + 1):[cite: 6]
                    result = fetch_delivery_data(ist_now.date())[cite: 6]
                    if result:[cite: 6]
                        return result[cite: 6]
                    if attempt < DELIVERY_FETCH_RETRIES:[cite: 6]
                        time.sleep(DELIVERY_RETRY_INTERVAL_S)[cite: 6]
                return {}[cite: 6]

            delivery_map:    dict[str, float] = {}[cite: 6]
            all_ticker_data = {}[cite: 6]

            with ThreadPoolExecutor(max_workers=2) as pool:[cite: 6]
                future_delivery = pool.submit(_fetch_delivery_with_retry)[cite: 6]
                future_prices   = pool.submit(fetch_watchlist_data, watchlist, "2y", "1d")[cite: 6]
                for future in as_completed([future_delivery, future_prices]):[cite: 6]
                    if future is future_delivery:[cite: 6]
                        delivery_map    = future.result()[cite: 6]
                    else:[cite: 6]
                        all_ticker_data = future.result()[cite: 6]

            try:[cite: 6]
                rotation_result = get_sector_scores()[cite: 6]
            except Exception:[cite: 6]
                from sector_rotation import SectorRotationResult[cite: 6]
                rotation_result = SectorRotationResult({}, set(), set(), "", date.today(), 0.0)[cite: 6]

            total_alerts       = 0[cite: 6]
            alerts_by_category = {}[cite: 6]

            rejection_counts = {k: 0 for k in [[cite: 6]
                "no_data", "missing_col", "insufficient_bars", "indicator_fail", "weak_signals",[cite: 6]
                "weak_body", "bearish_candle", "weak_close_pos", "upper_wick", "low_volume",[cite: 6]
                "low_avg_volume", "penny_stock", "rsi_range", "rsi_not_rising", "below_ema20",[cite: 6]
                "below_sma50", "no_golden_cross", "weak_adx", "macd_bearish", "far_from_52w_high",[cite: 6]
                "gap_day", "extended_breakout", "low_score", "duplicate", "stale_data"[cite: 6]
            ]}[cite: 6]

            for idx, (_, row) in enumerate(watchlist.iterrows(), start=1):[cite: 6]
                symbol = "UNKNOWN"[cite: 6]
                try:[cite: 6]
                    symbol   = row["Stock"][cite: 6]
                    category = row["Category"][cite: 6]
                    sector   = row.get("Sector", None)[cite: 6]

                    if symbol not in all_ticker_data:[cite: 6]
                        rejection_counts["no_data"] += 1[cite: 6]
                        continue[cite: 6]

                    ticker = all_ticker_data[symbol].copy()[cite: 6]

                    if ticker.empty:[cite: 6]
                        rejection_counts["no_data"] += 1[cite: 6]
                        continue[cite: 6]

                    if isinstance(ticker.columns, pd.MultiIndex):[cite: 6]
                        ticker.columns = ticker.columns.get_level_values(0)[cite: 6]

                    ticker = ticker.loc[:, ~ticker.columns.duplicated()][cite: 6]

                    required_cols = ["Open", "High", "Low", "Close", "Volume"][cite: 6]
                    missing_col   = False[cite: 6]

                    for col_name in required_cols:[cite: 6]
                        if col_name not in ticker.columns:[cite: 6]
                            missing_col = True[cite: 6]
                            break[cite: 6]
                        if isinstance(ticker[col_name], pd.DataFrame):[cite: 6]
                            ticker[col_name] = ticker[col_name].iloc[:, 0][cite: 6]
                        ticker[col_name] = pd.Series(ticker[col_name]).astype(float)[cite: 6]

                    if missing_col:[cite: 6]
                        rejection_counts["missing_col"] += 1[cite: 6]
                        continue[cite: 6]

                    ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])[cite: 6]

                    if ticker.empty:[cite: 6]
                        rejection_counts["no_data"] += 1[cite: 6]
                        continue[cite: 6]

                    if len(ticker) < 200:[cite: 6]
                        rejection_counts["insufficient_bars"] += 1[cite: 6]
                        continue[cite: 6]

                    ticker = apply_indicators(ticker, timeframe="1d")[cite: 6]

                    if ticker is None or ticker.empty:[cite: 6]
                        rejection_counts["indicator_fail"] += 1[cite: 6]
                        continue[cite: 6]

                    signals = detect_breakouts(ticker, timeframe="1d")[cite: 6]

                    if len(signals) < MIN_SIGNALS:[cite: 6]
                        rejection_counts["weak_signals"] += 1[cite: 6]
                        continue[cite: 6]

                    latest = ticker.iloc[-1][cite: 6]

                    if "RSI" not in ticker.columns or pd.isna(latest["RSI"]):[cite: 6]
                        continue[cite: 6]

                    _stale_col = next((c for c in ["Date", "Datetime"] if c in ticker.columns), None)[cite: 6]
                    if _stale_col:[cite: 6]
                        try:[cite: 6]
                            _last_ts = pd.to_datetime(latest[_stale_col])[cite: 6]
                            if _last_ts.tzinfo is not None:[cite: 6]
                                _last_ts = _last_ts.tz_convert("Asia/Kolkata")[cite: 6]
                            if _last_ts.date() != ist_now.date():[cite: 6]
                                rejection_counts["stale_data"] += 1[cite: 6]
                                continue[cite: 6]
                        except Exception:[cite: 6]
                            pass[cite: 6]

                    latest_volume = float(latest["Volume"])[cite: 6]
                    avg_volume    = float(ticker["Volume"].iloc[-21:-1].mean())[cite: 6]

                    if avg_volume <= 0:[cite: 6]
                        continue[cite: 6]

                    volume_ratio = latest_volume / avg_volume[cite: 6]

                    candle_high  = float(latest["High"])[cite: 6]
                    candle_low   = float(latest["Low"])[cite: 6]
                    candle_open  = float(latest["Open"])[cite: 6]
                    candle_close = float(latest["Close"])[cite: 6]
                    candle_range = candle_high - candle_low[cite: 6]
                    candle_body  = abs(candle_close - candle_open)[cite: 6]
                    upper_wick   = candle_high - candle_close[cite: 6]

                    if candle_range <= 0:[cite: 6]
                        continue[cite: 6]

                    body_ratio     = candle_body / candle_range[cite: 6]
                    close_position = (candle_close - candle_low) / candle_range[cite: 6]
                    wick_ratio     = upper_wick / candle_range[cite: 6]
                    rsi_val        = float(latest["RSI"])[cite: 6]

                    if body_ratio < MIN_BODY_RATIO:[cite: 6]
                        rejection_counts["weak_body"] += 1[cite: 6]
                        continue[cite: 6]
                    if candle_close <= candle_open:[cite: 6]
                        rejection_counts["bearish_candle"] += 1[cite: 6]
                        continue[cite: 6]
                    if close_position < MIN_CLOSE_POSITION:[cite: 6]
                        rejection_counts["weak_close_pos"] += 1[cite: 6]
                        continue[cite: 6]
                    if wick_ratio > MAX_UPPER_WICK_RATIO:[cite: 6]
                        rejection_counts["upper_wick"] += 1[cite: 6]
                        continue[cite: 6]
                    if volume_ratio < MIN_VOLUME_RATIO:[cite: 6]
                        rejection_counts["low_volume"] += 1[cite: 6]
                        continue[cite: 6]
                    if avg_volume < MIN_AVG_VOLUME_SHARES:[cite: 6]
                        rejection_counts["low_avg_volume"] += 1[cite: 6]
                        continue[cite: 6]
                    if candle_close < MIN_STOCK_PRICE:[cite: 6]
                        rejection_counts["penny_stock"] += 1[cite: 6]
                        continue[cite: 6]
                    if not (MIN_RSI <= rsi_val <= MAX_RSI):[cite: 6]
                        rejection_counts["rsi_range"] += 1[cite: 6]
                        continue[cite: 6]

                    if len(ticker) > RSI_LOOKBACK_BARS:[cite: 6]
                        rsi_prev = float(ticker["RSI"].iloc[-1 - RSI_LOOKBACK_BARS])[cite: 6]
                        if rsi_val <= rsi_prev:[cite: 6]
                            rejection_counts["rsi_not_rising"] += 1[cite: 6]
                            continue[cite: 6]

                    if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")):[cite: 6]
                        if candle_close < float(latest["EMA20"]):[cite: 6]
                            rejection_counts["below_ema20"] += 1[cite: 6]
                            continue[cite: 6]

                    if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")):[cite: 6]
                        if candle_close < float(latest["SMA50"]):[cite: 6]
                            rejection_counts["below_sma50"] += 1[cite: 6]
                            continue[cite: 6]

                    if ([cite: 6]
                        "SMA50" in ticker.columns and "SMA200" in ticker.columns and[cite: 6]
                        not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200"))[cite: 6]
                    ):[cite: 6]
                        if float(latest["SMA50"]) < float(latest["SMA200"]):[cite: 6]
                            rejection_counts["no_golden_cross"] += 1[cite: 6]
                            continue[cite: 6]

                    if "ADX" in ticker.columns and not pd.isna(latest.get("ADX")):[cite: 6]
                        if float(latest["ADX"]) < ADX_MIN_THRESHOLD:[cite: 6]
                            rejection_counts["weak_adx"] += 1[cite: 6]
                            continue[cite: 6]

                    if ([cite: 6]
                        "MACD" in ticker.columns and "MACD_SIGNAL" in ticker.columns and[cite: 6]
                        not pd.isna(latest.get("MACD")) and not pd.isna(latest.get("MACD_SIGNAL"))[cite: 6]
                    ):[cite: 6]
                        if float(latest["MACD"]) < float(latest["MACD_SIGNAL"]):[cite: 6]
                            rejection_counts["macd_bearish"] += 1[cite: 6]
                            continue[cite: 6]

                    if "HIGH_52W" in ticker.columns and not pd.isna(latest.get("HIGH_52W")):[cite: 6]
                        high_52w = float(latest["HIGH_52W"])[cite: 6]
                        if high_52w > 0:[cite: 6]
                            pct_from_high = (high_52w - candle_close) / high_52w * 100[cite: 6]
                            if pct_from_high > MAX_DISTANCE_FROM_52W_HIGH_PCT:[cite: 6]
                                rejection_counts["far_from_52w_high"] += 1[cite: 6]
                                continue[cite: 6]

                    if len(ticker) >= 2:[cite: 6]
                        prev_close = float(ticker["Close"].iloc[-2])[cite: 6]
                        if prev_close > 0:[cite: 6]
                            single_move_pct = abs(candle_close - prev_close) / prev_close * 100[cite: 6]
                            if single_move_pct > MAX_SINGLE_DAY_MOVE_PCT:[cite: 6]
                                rejection_counts["gap_day"] += 1[cite: 6]
                                continue[cite: 6]

                    if len(ticker) >= GAP_LOOKBACK_BARS + 1:[cite: 6]
                        prior_high = float(ticker["High"].iloc[-(GAP_LOOKBACK_BARS + 1):-1].max())[cite: 6]
                        if prior_high > 0:[cite: 6]
                            gap_pct = (candle_open - prior_high) / prior_high * 100[cite: 6]
                            if gap_pct > MAX_GAP_FROM_PRIOR_HIGH_PCT:[cite: 6]
                                rejection_counts["extended_breakout"] += 1[cite: 6]
                                continue[cite: 6]

                    delivery_pct = delivery_map.get(symbol, None)[cite: 6]

                    atr_val_eod = ([cite: 6]
                        float(latest["ATR"])[cite: 6]
                        if "ATR" in ticker.columns and not pd.isna(latest.get("ATR"))[cite: 6]
                        else None[cite: 6]
                    )[cite: 6]

                    score = calculate_score([cite: 6]
                        category=category,[cite: 6]
                        breakout_count=len(signals),[cite: 6]
                        rsi=rsi_val,[cite: 6]
                        volume_ratio=volume_ratio,[cite: 6]
                        breakout_signals=signals,[cite: 6]
                        ticker=ticker,[cite: 6]
                        latest=latest,[cite: 6]
                        symbol=symbol,[cite: 6]
                        timeframe="1d",[cite: 6]
                        atr_val=atr_val_eod,[cite: 6]
                        delivery_pct=delivery_pct,[cite: 6]
                        min_vol=MIN_AVG_VOLUME_SHARES,[cite: 6]
                    )[cite: 6]

                    if score > 0:[cite: 6]
                        try:[cite: 6]
                            safe_sector  = "Unknown" if (sector is None or (isinstance(sector, float) and pd.isna(sector))) else str(sector).strip()[cite: 6]
                            sector_bonus = rotation_result.score_bonus_for(safe_sector)[cite: 6]
                            score = max(0, min(score + sector_bonus, 100))[cite: 6]
                        except Exception:[cite: 6]
                            pass[cite: 6]

                    if score < MIN_SCORE:[cite: 6]
                        rejection_counts["low_score"] += 1[cite: 6]
                        continue[cite: 6]

                    signal_str = ", ".join(signals.keys() if isinstance(signals, dict) else signals)[cite: 6]
                    dedup_key  = f"{category}|{signal_str}|{today_str}|EOD"[cite: 6]

                    saved = save_alert_if_new(symbol, dedup_key, datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"))[cite: 6]
                    if not saved:[cite: 6]
                        rejection_counts["duplicate"] += 1[cite: 6]
                        continue[cite: 6]

                    current_atr = atr_val_eod if atr_val_eod is not None else (candle_range * 1.5)[cite: 6]
                    suggested_stop = candle_close - (1.5 * current_atr)[cite: 6]

                    above_sma50 = bool(candle_close >= float(latest["SMA50"])) if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")) else None[cite: 2, 6]
                    golden_cross = bool(float(latest["SMA50"]) >= float(latest["SMA200"])) if ("SMA50" in ticker.columns and "SMA200" in ticker.columns and not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200"))) else None[cite: 2, 6]

                    alerts_by_category.setdefault(category, []).append({[cite: 6]
                        "symbol":           symbol,[cite: 6]
                        "category":         category,[cite: 6]
                        "breakout_signals": list(signals.keys()) if isinstance(signals, dict) else signals,[cite: 6]
                        "price":            round(candle_close, 2),[cite: 6]
                        "open":             round(candle_open, 2),[cite: 6]
                        "day_high":         round(candle_high, 2),[cite: 6]
                        "day_low":          round(candle_low, 2),[cite: 6]
                        "rsi":              round(rsi_val, 1),[cite: 6]
                        "volume_ratio":     round(volume_ratio, 2),[cite: 6]
                        "body_ratio":       round(body_ratio * 100),[cite: 6]
                        "close_position":   round(close_position * 100),[cite: 6]
                        "score":            score,[cite: 6]
                        "delivery_pct":     round(delivery_pct, 1) if delivery_pct is not None else None,[cite: 6]
                        "above_ema20":      bool(candle_close >= float(latest["EMA20"])) if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")) else None,[cite: 2, 6]
                        "above_sma50":      above_sma50,[cite: 6]
                        "golden_cross":     golden_cross,[cite: 6]
                        "atr_stop":         round(suggested_stop, 2)[cite: 6]
                    })[cite: 6]
                    total_alerts += 1[cite: 6]

                except Exception:[cite: 6]
                    logger.exception(f"❌ Error processing {symbol}")[cite: 6]

            scan_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")[cite: 6]

            if total_alerts > 0:[cite: 6]
                for cat in sorted(alerts_by_category.keys()):[cite: 6]
                    cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["score"], reverse=True)[cite: 2, 6]
                    chunks     = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)][cite: 2, 6]
                    for chunk_num, chunk in enumerate(chunks, start=1):[cite: 6]
                        msg = build_message("EOD", cat, chunk, chunk_num, len(chunks), scan_time)[cite: 6]
                        send_telegram_message(msg, scan_type="EOD")[cite: 6]

            # ── SEND DAILY CONSOLIDATED SUMMARY EMAIL ────────────────────────────────
            try:
                from daily_report import generate_and_send_daily_summary
                logger.info("📧 Compiling daily comprehensive email report payload...")
                generate_and_send_daily_summary()
            except Exception as e:
                logger.error(f"❌ Failed to dispatch daily summary mail package: {e}")
            # ────────────────────────────────────────────────────────────────────────

            duration       = (datetime.now(IST) - scan_start).total_seconds()[cite: 6]
            last_scan_date = today_str[cite: 6]
            sleep_secs     = seconds_until_eod()[cite: 6]

            fired = {k: v for k, v in rejection_counts.items() if v > 0}[cite: 6]
            if fired:[cite: 6]
                logger.info("   Rejections: " + " | ".join(f"{k}={v}" for k, v in fired.items()))[cite: 6]

            time.sleep(min(sleep_secs, 3600))[cite: 6]

        except Exception:[cite: 6]
            logger.exception("❌ CRITICAL EOD SCAN ERROR — will retry next cycle")[cite: 6]
            time.sleep(300)[cite: 6]

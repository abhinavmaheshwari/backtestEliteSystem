# =====================================================================================
# app/price_cache.py (BULLETPROOF EDITION)
# =====================================================================================

import logging
import threading
import time
from datetime import time as dt_time
import pandas as pd
from typing import Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from data_fetch_status import mark_success, mark_failure
from database import upsert_fetch_error
from data_provider import get_fetcher
from config import BATCH_DOWNLOAD_SIZE, PRICE_CACHE_TTL_SECONDS

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_cache: dict[tuple, dict] = {}
_lock = threading.Lock()
CACHE_TTL_SECONDS = PRICE_CACHE_TTL_SECONDS

# Map interval string to required freshness cadence (seconds)
_INTERVAL_CADENCE = {
    '1m': 60,
    '15m': 900,
    '1h': 3600,
    '1d': 24 * 3600,
}

def _is_market_hours() -> bool:
    now = datetime.now(IST)
    return dt_time(9, 15) <= now.time() <= dt_time(15, 30) and now.weekday() < 5

def fetch_watchlist_data(watchlist: pd.DataFrame, period: str = "10d", interval: str = "15m") -> dict[str, pd.DataFrame]:
    cache_key = (interval, period)
    cadence = _INTERVAL_CADENCE.get(interval, CACHE_TTL_SECONDS)

    with _lock:
        entry = _cache.get(cache_key)
        if entry is not None:
            age = time.monotonic() - entry["ts"]
            if age < cadence:
                data_as_of = entry.get("data_as_of")
                stale = False
                if data_as_of and _is_market_hours():
                    if (datetime.now(timezone.utc) - data_as_of).total_seconds() > 120:
                        logger.warning(f"Cache stale: oldest data is {data_as_of}. Forcing refresh.")
                        stale = True
                
                if not stale:
                    logger.debug(f"📦 Price cache hit | {interval} | {period} | age={age:.1f}s < cadence={cadence}s")
                    return entry["data"]
            else:
                logger.info(f"Price cache stale for {interval} (age={age:.1f}s >= cadence={cadence}s). Forcing fresh download.")

    # Cache miss or stale — download fresh data
    result = _download_all_robust(watchlist, period=period, interval=interval)

    # Determine oldest timestamp in batch
    data_as_of = None
    if result:
        timestamps = []
        for df in result.values():
            if not df.empty:
                try:
                    ts = None
                    if "Datetime" in df.columns:
                        ts = df["Datetime"].iloc[-1]
                    elif "Date" in df.columns:
                        ts = df["Date"].iloc[-1]
                    else:
                        ts = df.index[-1]
                    ts = pd.to_datetime(ts)
                    if ts.tzinfo is None:
                        ts = ts.tz_localize(IST)
                    else:
                        ts = ts.tz_convert(timezone.utc)
                    timestamps.append(ts)
                except Exception:
                    pass
        if timestamps:
            data_as_of = min(timestamps)
            if data_as_of.tzinfo is None:
                data_as_of = data_as_of.replace(tzinfo=timezone.utc)
            else:
                data_as_of = data_as_of.astimezone(timezone.utc)

    with _lock:
        _cache[cache_key] = {
            "data": result,
            "ts": time.monotonic(),
            "data_as_of": data_as_of
        }

    return result

def _download_all_robust(watchlist: pd.DataFrame, period: str, interval: str) -> dict[str, pd.DataFrame]:
    symbols = watchlist["Stock"].tolist()
    all_data: dict[str, pd.DataFrame] = {}
    total = len(symbols)
    batch_size = BATCH_DOWNLOAD_SIZE
    fetcher = get_fetcher()

    for i in range(0, total, batch_size):
        batch = symbols[i : i + batch_size]
        batch_end = min(i + batch_size, total)
        logger.info(f"📥 Fetching Batch ({i}–{batch_end}/{total}) [{interval}]")
        
        batch_results = fetcher.get_batch_ohlcv(batch, interval=interval, period=period, retries=3)
        if batch_results:
            all_data.update(batch_results)
            try:
                mark_success(f"yfinance:{interval}")
            except Exception:
                pass
        else:
            logger.error(f"❌ Batch failed completely. Engaging single-ticker fallback for {len(batch)} symbols...")
            try:
                mark_failure(f"yfinance:{interval}", f"Batch failed for symbols {batch}.")
            except Exception:
                pass
            for sym in batch:
                single_df = fetcher.get_ohlcv(sym, interval=interval, period=period, retries=3)
                if single_df is not None:
                    all_data[sym] = single_df
                time.sleep(0.5)

    logger.info(f"✅ Data secured for {len(all_data)}/{total} symbols [{interval}]")

    # Record missing symbols
    for sym in symbols:
        if sym not in all_data:
            try:
                upsert_fetch_error('yfinance', 'PRICE_CACHE', sym, interval, 'no_data_after_fetch', 'no_data_returned')
            except Exception:
                pass

    try:
        if len(all_data) > 0:
            mark_success(f"yfinance:{interval}")
        else:
            mark_failure(f"yfinance:{interval}", "No symbols returned after batch + fallback")
    except Exception:
        pass
        
    return all_data

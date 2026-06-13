# =====================================================================================
# app/price_cache.py (BULLETPROOF EDITION)
# =====================================================================================

import logging
import threading
import time
import pandas as pd
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo
try:
    from config import BATCH_DOWNLOAD_SIZE, PRICE_CACHE_TTL_SECONDS
except ImportError:
    from config import BATCH_DOWNLOAD_SIZE
    PRICE_CACHE_TTL_SECONDS = 90

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_cache: dict[tuple, dict] = {}
_lock = threading.Lock()
CACHE_TTL_SECONDS = PRICE_CACHE_TTL_SECONDS
MAX_RETRIES = 3

def fetch_watchlist_data(watchlist: pd.DataFrame, period: str = "10d", interval: str = "15m") -> dict[str, pd.DataFrame]:
    cache_key = (interval, period)

    with _lock:
        entry = _cache.get(cache_key)
        if entry is not None:
            age = time.monotonic() - entry["ts"]
            if age < CACHE_TTL_SECONDS:
                logger.debug(f"📦 Price cache hit | {interval} | {period}")
                return entry["data"]

    # Cache miss — download fresh data with robust retries
    result = _download_all_robust(watchlist, period=period, interval=interval)

    with _lock:
        _cache[cache_key] = {"data": result, "ts": time.monotonic()}

    return result



def _download_single_ticker(sym: str, period: str, interval: str) -> pd.DataFrame | None:
    """Fallback mechanism if batch downloading repeatedly fails."""
    try:
        ns_sym = f"{sym}.NS"
        df = yf.download(ns_sym, period=period, interval=interval, progress=False, auto_adjust=True, threads=False)
        if df is not None and not df.empty:
            return df.reset_index().copy()
    except Exception as e:
        logger.debug(f"⚠️ Single download failed for {sym}: {e}")
    return None

def _download_all_robust(watchlist: pd.DataFrame, period: str, interval: str) -> dict[str, pd.DataFrame]:
    symbols = watchlist["Stock"].tolist()
    all_data: dict[str, pd.DataFrame] = {}
    total = len(symbols)
    batch_size = BATCH_DOWNLOAD_SIZE

    for i in range(0, total, batch_size):
        batch = symbols[i : i + batch_size]
        tickers_str = " ".join(f"{sym}.NS" for sym in batch)
        batch_end = min(i + batch_size, total)
        
        logger.info(f"📥 Fetching Batch ({i}–{batch_end}/{total}) [{interval}]")
        
        batch_success = False
        
        # ATTEMPT 1: Exponential Backoff Batch Download
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw = yf.download(tickers_str, period=period, interval=interval, progress=False, auto_adjust=True, threads=False, group_by="ticker")
                
                if raw is None or raw.empty:
                    raise ValueError("Empty dataframe returned by yfinance")
                
                # YFinance bug: Multi-ticker request returns flat dataframe
                if not isinstance(raw.columns, pd.MultiIndex) and len(batch) > 1:
                    raise ValueError("yfinance returned flat DF instead of MultiIndex for batch")
                
                if isinstance(raw.columns, pd.MultiIndex):
                    level0 = raw.columns.get_level_values(0)
                    for sym in batch:
                        ns_sym = f"{sym}.NS"
                        if ns_sym in level0 or sym in level0:
                            key = ns_sym if ns_sym in level0 else sym
                            sym_df = raw[key].reset_index().copy()
                            if not sym_df.empty:
                                all_data[sym] = sym_df
                else:
                    # Single ticker batch success
                    sym_df = raw.reset_index().copy()
                    if not sym_df.empty:
                        all_data[batch[0]] = sym_df
                
                batch_success = True
                break # Break retry loop on success
                
            except Exception as e:
                logger.warning(f"⚠️ Batch download error (Attempt {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(2 ** attempt) # Exponential backoff: 2s, 4s, 8s

        # ATTEMPT 2: Fallback to single downloads if the entire batch failed 3 times
        if not batch_success:
            logger.error(f"❌ Batch failed completely. Engaging single-ticker fallback for {len(batch)} symbols...")
            for sym in batch:
                single_df = _download_single_ticker(sym, period, interval)
                if single_df is not None:
                    all_data[sym] = single_df
                time.sleep(0.5) # Prevent aggressive rate limiting on single pulls

    logger.info(f"✅ Data secured for {len(all_data)}/{total} symbols [{interval}]")
    return all_data

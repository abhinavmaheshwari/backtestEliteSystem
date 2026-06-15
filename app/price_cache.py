# =====================================================================================
# app/price_cache.py (BULLETPROOF EDITION)
# =====================================================================================

import logging
import threading
import time
import pandas as pd
from typing import Optional
# Ensure tzcache writable location before importing yfinance (robust import to support different cwd)
try:
    import app.yf_bootstrap
except Exception:
    try:
        import yf_bootstrap
    except Exception:
        pass
import yfinance as yf
from data_fetch_status import mark_success, mark_failure
from database import upsert_fetch_error
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

# Map interval string to required freshness cadence (seconds)
_INTERVAL_CADENCE = {
    '1m': 60,
    '15m': 900,
    '1h': 3600,
    '1d': 24 * 3600,
}


def fetch_watchlist_data(watchlist: pd.DataFrame, period: str = "10d", interval: str = "15m") -> dict[str, pd.DataFrame]:
    cache_key = (interval, period)

    cadence = _INTERVAL_CADENCE.get(interval, CACHE_TTL_SECONDS)

    with _lock:
        entry = _cache.get(cache_key)
        if entry is not None:
            age = time.monotonic() - entry["ts"]
            # Use cached data only if it is fresher than the interval cadence.
            if age < cadence:
                logger.debug(f"📦 Price cache hit | {interval} | {period} | age={age:.1f}s < cadence={cadence}s")
                return entry["data"]
            else:
                logger.info(f"Price cache stale for {interval} (age={age:.1f}s >= cadence={cadence}s). Forcing fresh download.")

    # Cache miss or stale — download fresh data with robust retries
    result = _download_all_robust(watchlist, period=period, interval=interval)

    with _lock:
        _cache[cache_key] = {"data": result, "ts": time.monotonic()}

    return result



def to_yf_sym(sym: str) -> str:
    return sym.replace("_", "-") + ".NS"

def _download_single_ticker(sym: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    """Fallback mechanism if batch downloading repeatedly fails."""
    try:
        ns_sym = to_yf_sym(sym)
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
        tickers_str = " ".join(to_yf_sym(sym) for sym in batch)
        batch_end = min(i + batch_size, total)
        
        logger.info(f"📥 Fetching Batch ({i}–{batch_end}/{total}) [{interval}]")
        
        batch_success = False
        last_error = None
        
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
                        ns_sym = to_yf_sym(sym)
                        yf_base = sym.replace("_", "-")
                        if ns_sym in level0 or yf_base in level0:
                            key = ns_sym if ns_sym in level0 else yf_base
                            sym_df = raw[key].reset_index().copy()
                            if not sym_df.empty:
                                all_data[sym] = sym_df
                else:
                    # Single ticker batch success
                    sym_df = raw.reset_index().copy()
                    if not sym_df.empty:
                        all_data[batch[0]] = sym_df
                
                batch_success = True
                try:
                    # Report successful yfinance batch fetch for this interval
                    mark_success(f"yfinance:{interval}")
                except Exception:
                    logger.exception("Failed to report yfinance batch success")
                break # Break retry loop on success
                
            except Exception as e:
                last_error = e
                logger.warning(f"⚠️ Batch download error (Attempt {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(2 ** attempt) # Exponential backoff: 2s, 4s, 8s

        # ATTEMPT 2: Fallback to single downloads if the entire batch failed 3 times
        if not batch_success:
            logger.error(f"❌ Batch failed completely. Engaging single-ticker fallback for {len(batch)} symbols...")
            try:
                mark_failure(f"yfinance:{interval}", f"Batch failed for symbols {batch}. Last Error: {last_error}")
            except Exception:
                logger.exception("Failed to report yfinance batch failure")
            for sym in batch:
                single_df = _download_single_ticker(sym, period, interval)
                if single_df is not None:
                    all_data[sym] = single_df
                time.sleep(0.5) # Prevent aggressive rate limiting on single pulls

    logger.info(f"✅ Data secured for {len(all_data)}/{total} symbols [{interval}]")

    # Record missing symbols into fetch_errors for audit/triage
    try:
        for sym in symbols:
            if sym not in all_data:
                try:
                    upsert_fetch_error('yfinance', 'PRICE_CACHE', sym, interval, 'no_data_after_fetch', 'no_data_returned')
                except Exception:
                    logger.exception('Failed to upsert fetch error for symbol %s', sym)
    except Exception:
        logger.exception('Failed while recording missing symbols')

    try:
        if len(all_data) > 0:
            mark_success(f"yfinance:{interval}")
        else:
            mark_failure(f"yfinance:{interval}", "No symbols returned after batch + fallback")
    except Exception:
        logger.exception("Failed to report final yfinance fetch status")
    return all_data

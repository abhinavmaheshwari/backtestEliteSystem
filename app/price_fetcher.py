import os
import time
import logging
import threading
from datetime import datetime, timezone
import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from database import get_cache_metadata, upsert_cache_metadata, upsert_data_fetch_health, get_all_data_fetch_health
from data_registry import DATASETS

logger = logging.getLogger(__name__)

# In-memory run cache
_price_cache = {}

# Disk cache directory
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "price_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Retry wrapper for network calls
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True
)
def _fetch_history_with_retry(yf_symbol: str, period: str = "1y", auto_adjust: bool = True) -> pd.DataFrame:
    ticker = yf.Ticker(yf_symbol)
    hist = ticker.history(period=period, auto_adjust=auto_adjust)
    if hist is None or hist.empty:
        raise ValueError(f"Empty history returned for {yf_symbol}")
        
    # Validate with nsepython if auto_adjust is True
    if auto_adjust:
        try:
            from nsepython import nse_eq
            symbol_raw = yf_symbol.replace(".NS", "")
            nse_data = nse_eq(symbol_raw)
            if 'priceInfo' in nse_data and 'lastPrice' in nse_data['priceInfo']:
                nse_ltp = float(nse_data['priceInfo']['lastPrice'])
                yf_ltp = float(hist['Close'].iloc[-1])
                
                # If discrepancy > 2%, yfinance applied a bad corporate action adjustment
                if nse_ltp > 0 and abs(yf_ltp - nse_ltp) / nse_ltp > 0.02:
                    logger.warning(f"⚠️ {yf_symbol}: yfinance adjusted data is stale/corrupt. Discrepancy > 2%. Falling back to unadjusted.")
                    return ticker.history(period=period, auto_adjust=False)
        except Exception as e:
            logger.debug(f"NSE Validation failed for {yf_symbol}: {e}")
            
    return hist


def _cache_file_path(key: str) -> str:
    safe = key.replace('/', '_').replace(':', '__')
    return os.path.join(CACHE_DIR, f"{safe}.parquet")


def _acquire_lock(lock_path: str) -> bool:
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def _read_cache_file(path: str) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except Exception:
        return pd.DataFrame()


def _write_cache_file(df: pd.DataFrame, path: str) -> None:
    try:
        df.to_parquet(path, index=False)
    except Exception:
        logger.exception(f"Failed to write cache file {path}")


def _symbol_to_yf(symbol: str) -> str:
    symbol = symbol.replace("_", "-")
    return symbol + ".NS" if not symbol.endswith(".NS") else symbol


def fetch_historical_data(symbol: str, period: str = "1y", resolution: str = "1d", dataset_key: str = None, use_cache: bool = True, stale_serve: bool = True) -> pd.DataFrame:
    """Cadence-aware fetcher with persistent disk cache and stale-while-revalidate behaviour.

    dataset_key (optional) ties the fetch to a cadence defined in `app.data_registry.DATASETS`.
    If cached data exists and is fresh (age < cadence) it's returned. If it's stale and
    `stale_serve` is True we return stale immediately and start a background refresh.
    """
    yf_symbol = _symbol_to_yf(symbol)
    if dataset_key is None:
        dataset_key = f"price_{resolution}"

    ds = DATASETS.get(dataset_key, {})
    cadence = ds.get("cadence", 24 * 3600)

    cache_key = f"{dataset_key}::{yf_symbol}"
    cache_path = _cache_file_path(cache_key)
    lock_path = cache_path + ".lock"

    # In-memory run cache quick-hit
    if use_cache and cache_key in _price_cache:
        return _price_cache[cache_key].copy()

    # Check persisted metadata
    meta = None
    try:
        meta = get_cache_metadata(cache_key)
    except Exception:
        meta = None

    now = datetime.now(timezone.utc)
    if meta:
        try:
            last_fetched = datetime.fromisoformat(meta['last_fetched'])
            age = (now - last_fetched).total_seconds()
        except Exception:
            age = float('inf')

        # Fresh cache
        if age < cadence and os.path.exists(cache_path):
            df = _read_cache_file(cache_path)
            if not df.empty:
                if use_cache:
                    _price_cache[cache_key] = df.copy()
                return df

        # Stale: return stale if allowed and trigger background refresh
        if stale_serve and os.path.exists(cache_path):
            df = _read_cache_file(cache_path)
            # Trigger background refresh if possible
            def _bg_refresh():
                if not _acquire_lock(lock_path):
                    return
                try:
                    try:
                        fetched = _fetch_history_with_retry(yf_symbol, period)
                        if not fetched.empty:
                            _write_cache_file(fetched, cache_path)
                            upsert_cache_metadata(cache_key, datetime.now(timezone.utc).isoformat(), cadence, len(fetched), source='yfinance')
                            from data_fetch_status import mark_success
                            mark_success('yfinance')
                    except Exception as e:
                        # record failure in health table
                        logger.warning(f"Background refresh failed for {yf_symbol}: {e}")
                        from data_fetch_status import mark_failure
                        mark_failure('yfinance', f"{e} (Symbol: {yf_symbol})")
                finally:
                    _release_lock(lock_path)

            threading.Thread(target=_bg_refresh, daemon=True).start()
            if use_cache:
                _price_cache[cache_key] = df.copy()
            return df

    # No cache or forced refresh — attempt direct fetch (synchronous)
    got_lock = _acquire_lock(lock_path)
    try:
        if not got_lock:
            # Another process is refreshing. If stale exists return it, else wait briefly then try
            if os.path.exists(cache_path):
                df = _read_cache_file(cache_path)
                if use_cache:
                    _price_cache[cache_key] = df.copy()
                return df
            # wait short period for lock to clear
            waited = 0
            while waited < 10:
                time.sleep(1)
                waited += 1
                if not os.path.exists(lock_path):
                    break

        try:
            fetched = _fetch_history_with_retry(yf_symbol, period)
            if fetched is None or fetched.empty:
                raise ValueError('Empty fetch')
            # persist
            _write_cache_file(fetched, cache_path)
            upsert_cache_metadata(cache_key, datetime.now(timezone.utc).isoformat(), cadence, len(fetched), source='yfinance')
            from data_fetch_status import mark_success
            mark_success('yfinance')
            if use_cache:
                _price_cache[cache_key] = fetched.copy()
            return fetched
        except Exception as e:
            logger.warning(f"Failed to fetch historical data for {yf_symbol}: {e}")
            from data_fetch_status import mark_failure
            mark_failure('yfinance', f"{e} (Symbol: {yf_symbol})")
            # fallback to any stale cache if present
            if os.path.exists(cache_path):
                df = _read_cache_file(cache_path)
                if use_cache:
                    _price_cache[cache_key] = df.copy()
                return df
            return pd.DataFrame()
    finally:
        if got_lock:
            _release_lock(lock_path)


def clear_price_cache():
    """Clears the in-memory price cache. Should be called at the start of a new run."""
    _price_cache.clear()

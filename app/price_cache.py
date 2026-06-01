# =====================================================================================
# app/price_cache.py
# SHARED PRICE DATA CACHE
#
# PROBLEM THIS SOLVES:
#   intraday.py (15m) and live_scanner.py (1h) both fire at startup within ~1 second
#   of each other. Each had its own fetch_watchlist_data() that independently called
#   yf.download for all 369 symbols. Result: every batch log line appeared twice,
#   the Railway 500 logs/sec rate limit was hit, and yfinance rate-limit errors
#   (YFRateLimitError) were frequent.
#
# FIX:
#   This module is the single download layer for both scanners. It caches results
#   per (interval, period) key with a 90-second TTL. When intraday.py downloads
#   15m data and live_scanner.py fires 1 second later requesting 1h data, each
#   gets its own cache entry — no collision, no double download per timeframe.
#   If both scanners somehow request the SAME interval simultaneously (unlikely but
#   possible after a restart), the second call returns the cache hit instantly.
#
# CACHE DESIGN:
#   Key  = (interval, period)   e.g. ("15m", "10d"), ("1h", "60d")
#   TTL  = 90 seconds           covers the ~1s startup gap + any retry within a cycle
#   Lock = threading.Lock()     safe for concurrent threads in the same process
#
# USAGE (replace fetch_watchlist_data calls in intraday.py and live_scanner.py):
#
#   from price_cache import fetch_watchlist_data
#
#   all_ticker_data = fetch_watchlist_data(watchlist, period="10d", interval="15m")
#   all_ticker_data = fetch_watchlist_data(watchlist, period="60d", interval="1h")
#
# The function signature is identical to the old per-scanner versions — drop-in.
# =====================================================================================

import logging
import threading
import time as _time

import pandas as pd
import yfinance as yf

from datetime import datetime
from zoneinfo import ZoneInfo

from config import BATCH_DOWNLOAD_SIZE

logger = logging.getLogger(__name__)
IST    = ZoneInfo("Asia/Kolkata")

# ── Cache store ───────────────────────────────────────────────────────────────────────
# { (interval, period): {"data": dict[str, pd.DataFrame], "ts": float} }
_cache: dict[tuple, dict] = {}
_lock  = threading.Lock()

CACHE_TTL_SECONDS = 90   # two scans starting within 90s share one download


# =====================================================================================
# PUBLIC API
# =====================================================================================

def fetch_watchlist_data(
    watchlist: pd.DataFrame,
    period:    str = "10d",
    interval:  str = "15m",
) -> dict[str, pd.DataFrame]:
    """
    Download OHLCV data for all watchlist symbols in batches via yfinance.
    Results are cached per (interval, period) for CACHE_TTL_SECONDS seconds.

    Parameters
    ----------
    watchlist : pd.DataFrame  — must have a "Stock" column
    period    : str           — yfinance period string, e.g. "10d", "60d"
    interval  : str           — yfinance interval string, e.g. "15m", "1h"

    Returns
    -------
    dict[str, pd.DataFrame]  — {symbol: ohlcv_df}, only successfully downloaded symbols.
    Each DataFrame has columns reset to the index (Datetime or Date included).
    """
    cache_key = (interval, period)

    with _lock:
        entry = _cache.get(cache_key)
        if entry is not None:
            age = _time.monotonic() - entry["ts"]
            if age < CACHE_TTL_SECONDS:
                logger.info(
                    f"📦 Price cache hit | interval={interval} period={period} "
                    f"| age={age:.1f}s | {len(entry['data'])} symbols — skipping download"
                )
                return entry["data"]

    # Cache miss (or expired) — download fresh data
    result = _download_all(watchlist, period=period, interval=interval)

    with _lock:
        _cache[cache_key] = {"data": result, "ts": _time.monotonic()}

    return result


def invalidate(interval: str = None, period: str = None):
    """
    Manually invalidate cache entries. Pass both to target a specific key,
    or neither to flush the entire cache.
    """
    with _lock:
        if interval and period:
            _cache.pop((interval, period), None)
        else:
            _cache.clear()


# =====================================================================================
# INTERNAL DOWNLOAD (identical logic extracted from both scanners)
# =====================================================================================

def _download_all(
    watchlist: pd.DataFrame,
    period:    str,
    interval:  str,
) -> dict[str, pd.DataFrame]:
    """
    Batch-download OHLCV data for all symbols in watchlist["Stock"].
    This is the single download implementation shared by all scanners.
    """
    symbols    = watchlist["Stock"].tolist()
    all_data: dict[str, pd.DataFrame] = {}
    total      = len(symbols)
    batch_size = BATCH_DOWNLOAD_SIZE

    for i in range(0, total, batch_size):
        batch       = symbols[i : i + batch_size]
        tickers_str = " ".join(f"{sym}.NS" for sym in batch)
        batch_end   = min(i + batch_size, total)

        logger.info(
            f"📥 Batch downloading {len(batch)} symbols "
            f"({i}–{batch_end}/{total}) [{interval}]"
        )

        try:
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

            if not isinstance(raw.columns, pd.MultiIndex):
                # Flat DataFrame — yfinance returned a single-ticker result.
                if len(batch) == 1:
                    sym = batch[0]
                    df  = raw.reset_index().copy()
                    if not df.empty:
                        all_data[sym] = df
                else:
                    # Multi-ticker request but flat DF returned — symbol→data mismatch risk.
                    logger.warning(
                        f"⚠️ YF returned flat DF for multi-ticker batch "
                        f"(batch {i // batch_size + 1}, {len(batch)} requested). "
                        f"Skipping to prevent symbol→data mismatch."
                    )
                continue

            # MultiIndex: columns are (Ticker, OHLCV)
            for sym in batch:
                ns_sym = f"{sym}.NS"
                try:
                    level0 = raw.columns.get_level_values(0)
                    key    = ns_sym if ns_sym in level0 else (sym if sym in level0 else None)
                    if key is None:
                        logger.warning(f"⚠️ Symbol not in batch response: {sym}")
                        continue
                    # Assign and store in one expression — avoids UnboundLocalError
                    # if reset_index() or copy() raises (Python 3.12 scoping).
                    sym_df = raw[key].reset_index().copy()
                    if not sym_df.empty:
                        all_data[sym] = sym_df
                except Exception:
                    logger.warning(f"⚠️ Skipping {sym} — could not extract from batch")

        except Exception:
            logger.exception(f"❌ Batch download failed (batch {i // batch_size + 1})")

    logger.info(f"📥 Data downloaded for {len(all_data)}/{total} symbols [{interval}]")
    return all_data

import os
import time
import logging
import yfinance as yf
import pandas as pd
from requests_cache import CachedSession
from pyrate_limiter import Duration, RequestRate, Limiter
from requests_ratelimiter import LimiterSession

from config import DATA_DIR
from watchlist_cache import get_watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Cache session and rate limiter setup (as per user specifications)
class CachedLimiterSession(CachedSession, LimiterSession):
    pass

session = CachedLimiterSession(
    limiter=Limiter(RequestRate(2, Duration.SECOND * 5)),  # 2 requests per 5 seconds
    bucket_class=dict,
    backend='sqlite',
    cache_name='yfinance_backtest_cache',
    expire_after=-1  # Never expire
)

START_DATE = "2026-01-01"
END_DATE   = "2026-06-19"
BACKTEST_DATA_DIR = os.path.join(DATA_DIR, "backtest_data")
os.makedirs(BACKTEST_DATA_DIR, exist_ok=True)

# yfinance only provides 5m data for the last 60 days per request.
# We must fetch in chunks to go further back.
CHUNKS_5M = [
    ("2026-01-01", "2026-02-28"),
    ("2026-03-01", "2026-04-30"),
    ("2026-05-01", "2026-06-19"),
]

def _normalize_symbol(symbol: str) -> str:
    sym = symbol.replace("_", "-")
    if sym.startswith("^"):
        return sym
    return f"{sym}.NS" if not sym.endswith(".NS") else sym

def prefetch_all():
    logger.info("Starting historical data pre-fetch for backtesting...")
    watchlist_df = get_watchlist()
    if watchlist_df.empty:
        logger.error("Watchlist is empty. Run daily_builder.py first.")
        return
    
    symbols = watchlist_df["Stock"].tolist()
    logger.info(f"Loaded {len(symbols)} symbols from watchlist.")

    for symbol in symbols:
        ns_sym = _normalize_symbol(symbol)
        
        # --- 1. Fetch Daily Data (1d) ---
        try:
            path_1d = os.path.join(BACKTEST_DATA_DIR, f"{ns_sym}_1d.parquet")
            if not os.path.exists(path_1d):
                df_1d = yf.download(
                    ns_sym, start=START_DATE, end=END_DATE,
                    interval="1d", session=session,
                    auto_adjust=True, progress=False, threads=False
                )
                
                # Cleanup multiindex
                if isinstance(df_1d.columns, pd.MultiIndex):
                    df_1d.columns = df_1d.columns.get_level_values(0)
                    
                df_1d = df_1d.dropna(how='all')
                
                if not df_1d.empty:
                    df_1d.to_parquet(path_1d)
                    logger.info(f"Cached {ns_sym} 1d: {len(df_1d)} bars")
                else:
                    logger.warning(f"No 1d data for {ns_sym}")
            else:
                logger.info(f"Already cached {ns_sym} 1d")
        except Exception as e:
            logger.error(f"Failed to fetch 1d data for {ns_sym}: {e}")

        # --- 2. Fetch Intraday Data (5m / 15m) ---
        # Note: The system mostly relies on 15m in intraday.py, but user mentions 5m in prompts.
        # We fetch 15m directly to avoid resampling complexity, but also support 5m if needed.
        for interval in ["15m"]:
            try:
                path_intraday = os.path.join(BACKTEST_DATA_DIR, f"{ns_sym}_{interval}.parquet")
                if not os.path.exists(path_intraday):
                    all_chunks = []
                    for chunk_start, chunk_end in CHUNKS_5M:
                        df_chunk = yf.download(
                            ns_sym, start=chunk_start, end=chunk_end,
                            interval=interval, session=session,
                            auto_adjust=True, progress=False, threads=False
                        )
                        # Cleanup multiindex
                        if isinstance(df_chunk.columns, pd.MultiIndex):
                            df_chunk.columns = df_chunk.columns.get_level_values(0)
                        
                        df_chunk = df_chunk.dropna(how='all')
                        if not df_chunk.empty:
                            all_chunks.append(df_chunk)
                    
                    if all_chunks:
                        df_intraday = pd.concat(all_chunks)
                        # Localize index to IST before saving as requested
                        if df_intraday.index.tz is None:
                            df_intraday.index = df_intraday.index.tz_localize("Asia/Kolkata")
                        else:
                            df_intraday.index = df_intraday.index.tz_convert("Asia/Kolkata")
                            
                        # Sort and drop duplicates
                        df_intraday = df_intraday[~df_intraday.index.duplicated(keep='first')].sort_index()
                        
                        df_intraday.to_parquet(path_intraday)
                        logger.info(f"Cached {ns_sym} {interval}: {len(df_intraday)} bars")
                    else:
                        logger.warning(f"No {interval} data for {ns_sym}")
                else:
                    logger.info(f"Already cached {ns_sym} {interval}")
            except Exception as e:
                logger.error(f"Failed to fetch {interval} data for {ns_sym}: {e}")

if __name__ == "__main__":
    prefetch_all()

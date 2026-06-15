# =====================================================================================
# app/watchlist_cache.py
# =====================================================================================
import pandas as pd
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from config import WATCHLIST_PATH

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_watchlist_cache = None
_watchlist_date = None

def get_watchlist() -> pd.DataFrame:
    global _watchlist_cache, _watchlist_date
    current_date = datetime.now(IST).date()
    
    if _watchlist_cache is not None and _watchlist_date == current_date:
        return _watchlist_cache.copy()

    try:
        df = pd.read_parquet(WATCHLIST_PATH)
        _watchlist_cache = df
        _watchlist_date = current_date
        logger.info(f"📁 Watchlist loaded into memory cache ({len(df)} symbols)")
        return _watchlist_cache.copy()
    except Exception:
        # Try to restore from database first to avoid 2-minute rebuilding on server restarts
        try:
            from database import download_parquet_from_db
            import os
            
            # If downloaded successfully, we can just read it normally
            if download_parquet_from_db("daily_builder", WATCHLIST_PATH) and os.path.exists(WATCHLIST_PATH):
                df = pd.read_parquet(WATCHLIST_PATH)
                _watchlist_cache = df
                _watchlist_date = current_date
                logger.info(f"☁️ [WATCHLIST CACHE] Restored watchlist from Postgres cache ({len(df)} symbols)")
                return _watchlist_cache.copy()
        except Exception as e:
            logger.warning(f"Failed to restore watchlist from DB: {e}")

        # Fallback to trigger full build if missing
        try:
            from daily_builder import build_watchlist
            build_watchlist()
            df = pd.read_parquet(WATCHLIST_PATH)
            _watchlist_cache = df
            _watchlist_date = current_date
            logger.info(f"📁 Watchlist built and loaded into memory cache ({len(df)} symbols)")
            return _watchlist_cache.copy()
        except Exception as e:
            logger.error(f"❌ Failed to load/build watchlist: {e}")
            raise

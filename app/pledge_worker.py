import os
import time
import logging
import requests
import re
from bs4 import BeautifulSoup
from functools import lru_cache
import pandas as pd
from dotenv import load_dotenv

from database import get_connection, upsert_scanner_health, init_db
from data_fetch_status import mark_success, mark_failure
from config import WATCHLIST_PATH

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def discover_trendlyne_url(symbol: str, api_key: str) -> str:
    """Try to find the correct Trendlyne URL dynamically."""
    # Hardcoded fallback list
    fallbacks = {
        'HINDCOPPER': 'https://trendlyne.com/equity/551/HINDCOPPER/hindustan-copper-ltd/',
    }
    if symbol in fallbacks:
        return fallbacks[symbol]
        
    # Attempt basic shortcut (will likely 404)
    return f"https://trendlyne.com/stock/{symbol}/"

def worker_loop():
    logger.info("🚀 Starting Pledge Worker Daemon")
    init_db()
    
    api_key = os.getenv("SCRAPERAPI_KEY")
    if not api_key:
        logger.error("❌ SCRAPERAPI_KEY not found. Exiting.")
        return

    while True:
        try:
            # 1. Read the watchlist
            if not os.path.exists(WATCHLIST_PATH):
                logger.warning(f"Watchlist not found at {WATCHLIST_PATH}, sleeping 60s...")
                time.sleep(60)
                continue
                
            df = pd.read_parquet(WATCHLIST_PATH)
            if "Symbol" not in df.columns:
                logger.error("Symbol column missing from watchlist")
                time.sleep(60)
                continue
                
            symbols = df["Symbol"].unique().tolist()
            logger.info(f"Loaded {len(symbols)} symbols from watchlist.")
            
            # 2. Check DB for stale pledges
            stale_symbols = []
            with get_connection() as conn:
                with conn.cursor() as cur:
                    for sym in symbols:
                        cur.execute("""
                            SELECT updated_at 
                            FROM promoter_pledge_cache 
                            WHERE symbol = %s 
                              AND updated_at >= NOW() - INTERVAL '30 days'
                        """, (sym,))
                        if not cur.fetchone():
                            stale_symbols.append(sym)
                            
            if not stale_symbols:
                logger.info("✅ All pledges are up-to-date. Sleeping for 4 hours.")
                upsert_scanner_health("Pledge Worker", "IDLE", error_msg=None, today_alerts=0)
                time.sleep(4 * 3600)
                continue
                
            logger.info(f"Found {len(stale_symbols)} symbols needing pledge updates.")
            upsert_scanner_health("Pledge Worker", "RUNNING", today_alerts=len(stale_symbols))
            
            error_count = 0
            for i, sym in enumerate(stale_symbols):
                target_url = discover_trendlyne_url(sym, api_key)
                logger.info(f"[{i+1}/{len(stale_symbols)}] Scraping pledge for {sym} at {target_url}")
                
                payload = {'api_key': api_key, 'url': target_url, 'render': 'false'}
                try:
                    res = requests.get('https://api.scraperapi.com/', params=payload, timeout=45)
                    if res.status_code == 200:
                        pledge_val = None
                        match = re.search(r'pledge[^\d]{1,30}?(\d+\.?\d*)\s*%', res.text, re.IGNORECASE)
                        if match:
                            pledge_val = float(match.group(1))
                        else:
                            soup = BeautifulSoup(res.text, 'html.parser')
                            for div in soup.find_all(['div', 'span', 'td']):
                                if 'pledge' in div.text.lower() and '%' in div.text:
                                    m = re.search(r'(\d+\.?\d*)\s*%', div.text)
                                    if m:
                                        pledge_val = float(m.group(1))
                                        break
                        if pledge_val is not None:
                            with get_connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        INSERT INTO promoter_pledge_cache (symbol, pledge_pct, updated_at)
                                        VALUES (%s, %s, NOW())
                                        ON CONFLICT (symbol) DO UPDATE 
                                        SET pledge_pct = EXCLUDED.pledge_pct, updated_at = NOW()
                                    """, (sym, pledge_val))
                                    conn.commit()
                            logger.info(f"✅ Saved pledge for {sym}: {pledge_val}%")
                            mark_success('scraperapi')
                        else:
                            logger.warning(f"⚠️ Could not find pledge text on page for {sym}")
                            error_count += 1
                    elif res.status_code == 404:
                        logger.warning(f"❌ 404 Not Found for {sym} at {target_url}")
                        mark_failure('scraperapi', f"404 Not Found: {target_url}")
                        error_count += 1
                        
                        # Cache the failure temporarily (7 days) so we don't spam 404s
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO promoter_pledge_cache (symbol, pledge_pct, updated_at)
                                    VALUES (%s, %s, NOW() - INTERVAL '23 days')
                                    ON CONFLICT (symbol) DO UPDATE 
                                    SET updated_at = NOW() - INTERVAL '23 days'
                                """, (sym, 0.0))
                                conn.commit()
                    else:
                        logger.warning(f"❌ HTTP {res.status_code} for {sym}")
                        mark_failure('scraperapi', f"HTTP {res.status_code} URL={target_url}")
                        error_count += 1
                except Exception as e:
                    logger.exception(f"Exception scraping {sym}: {e}")
                    mark_failure('scraperapi', str(e))
                    error_count += 1
                    
                time.sleep(3) # Rate limit
                
            # Loop done
            status = "IDLE" if error_count == 0 else "WARNING"
            err_msg = f"Failed to fetch {error_count} pledges" if error_count > 0 else None
            upsert_scanner_health("Pledge Worker", status, error_msg=err_msg, today_alerts=error_count)
            logger.info("Sleeping 1 hour before next full check...")
            time.sleep(3600)
            
        except Exception as e:
            logger.exception("Pledge worker loop crashed")
            upsert_scanner_health("Pledge Worker", "DOWN", error_msg=str(e), today_alerts=1)
            time.sleep(300)

if __name__ == "__main__":
    load_dotenv()
    worker_loop()

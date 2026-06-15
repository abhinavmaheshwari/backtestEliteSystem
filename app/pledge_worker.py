import os
import time
import logging
import requests
import re
from bs4 import BeautifulSoup
from functools import lru_cache
import pandas as pd

from database import get_connection, upsert_scanner_health, init_db
from data_fetch_status import mark_success, mark_failure
from config import WATCHLIST_PATH

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def discover_trendlyne_url(symbol: str, api_key: str) -> str:
    """Try to find the correct Trendlyne URL dynamically."""
    clean_symbol = symbol.replace('.NS', '')
    
    # Hardcoded fallback list
    fallbacks = {
        'HINDCOPPER': 'https://trendlyne.com/equity/551/HINDCOPPER/hindustan-copper-ltd/',
    }
    if clean_symbol in fallbacks:
        return fallbacks[clean_symbol]
        
    fast_url = f"https://trendlyne.com/stock/{clean_symbol}/"
    
    # 1. Attempt fast HEAD request
    payload = {'api_key': api_key, 'url': fast_url, 'render': 'false'}
    try:
        # Note: ScraperAPI sometimes ignores HEAD, so we do a quick GET with a short timeout
        res = requests.get('https://api.scraperapi.com/', params=payload, timeout=10)
        if res.status_code == 200:
            return fast_url
    except Exception:
        pass

    # 2. If it 404s, use ScraperAPI to search Google for the proper trendlyne URL
    logger.info(f"🔍 Direct URL failed for {clean_symbol}. Searching Google...")
    search_url = f"https://www.google.com/search?q=site:trendlyne.com/equity/+{clean_symbol}"
    payload = {'api_key': api_key, 'url': search_url, 'render': 'false'}
    try:
        res = requests.get('https://api.scraperapi.com/', params=payload, timeout=30)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            for a in soup.find_all('a', href=True):
                href = a['href']
                # Check if it looks like a trendlyne equity link and contains the symbol
                if "trendlyne.com/equity/" in href and clean_symbol.upper() in href.upper():
                    # Extract from google redirect format
                    actual_url = href.split("q=")[-1].split("&")[0] if "/url?q=" in href else href
                    logger.info(f"✅ Discovered Google URL for {clean_symbol}: {actual_url}")
                    return actual_url
    except Exception as e:
        logger.warning(f"Google search fallback failed for {clean_symbol}: {e}")

    # 3. Fallback to the fast_url so it fails naturally downstream
    return fast_url

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
            if "Stock" not in df.columns:
                logger.error("Stock column missing from watchlist")
                time.sleep(60)
                continue
                
            symbols = df["Stock"].unique().tolist()
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
                upsert_scanner_health("Pledge Worker", "IDLE", error_msg="Last: None | Total: 0", today_alerts=0)
                time.sleep(4 * 3600)
                continue
                
            logger.info(f"Found {len(stale_symbols)} symbols needing pledge updates.")
            upsert_scanner_health("Pledge Worker", "RUNNING", today_alerts=0, error_msg=f"Last: Starting... | Total: {len(stale_symbols)}")
            
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
                            from datetime import datetime
                            from zoneinfo import ZoneInfo
                            now_str = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
                            upsert_scanner_health("Pledge Worker", "RUNNING", last_success=now_str, today_alerts=i+1, error_msg=f"Last: {sym} | Total: {len(stale_symbols)}")
                        else:
                            logger.warning(f"⚠️ Could not find pledge text on page for {sym}. Assuming 0.0%")
                            with get_connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        INSERT INTO promoter_pledge_cache (symbol, pledge_pct, updated_at)
                                        VALUES (%s, 0.0, NOW())
                                        ON CONFLICT (symbol) DO UPDATE 
                                        SET pledge_pct = EXCLUDED.pledge_pct, updated_at = NOW()
                                    """, (sym,))
                                    conn.commit()
                            mark_success('scraperapi')
                            from datetime import datetime
                            from zoneinfo import ZoneInfo
                            now_str = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
                            upsert_scanner_health("Pledge Worker", "RUNNING", last_success=now_str, today_alerts=i+1, error_msg=f"Last: {sym} | Total: {len(stale_symbols)}")
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
            status = "IDLE" if error_count == 0 else "DOWN"
            last_sym = stale_symbols[-1] if stale_symbols else "None"
            err_msg = f"Last: {last_sym} | Total: {len(stale_symbols)} | Failed: {error_count}" if error_count > 0 else f"Last: {last_sym} | Total: {len(stale_symbols)}"
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now_str = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
            upsert_scanner_health("Pledge Worker", status, last_success=now_str, error_msg=err_msg, today_alerts=len(stale_symbols))
            logger.info("Sleeping 1 hour before next full check...")
            time.sleep(3600)
            
        except Exception as e:
            logger.exception("Pledge worker loop crashed")
            upsert_scanner_health("Pledge Worker", "DOWN", error_msg=str(e), today_alerts=1)
            time.sleep(300)

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    worker_loop()

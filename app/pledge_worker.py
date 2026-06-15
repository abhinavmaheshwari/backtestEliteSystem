import os
import time
import logging
import requests
import re
from bs4 import BeautifulSoup
from functools import lru_cache
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

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
            symbols_set = set()
            watchlist_count = 0
            if os.path.exists(WATCHLIST_PATH):
                df = pd.read_parquet(WATCHLIST_PATH)
                if "Stock" in df.columns:
                    watch_symbols = df["Stock"].unique().tolist()
                    symbols_set.update(watch_symbols)
                    watchlist_count = len(watch_symbols)
            
            excluded_count = 0
            excluded_paths = [
                os.path.join(os.path.dirname(WATCHLIST_PATH), 'elite_fundamental_watchlist_excluded.csv'),
                os.path.join(os.path.dirname(WATCHLIST_PATH), 'elite_fundamental_watchlist-excluded.csv'),
                WATCHLIST_PATH.replace(".parquet", "_excluded.csv"),
            ]
            for excluded_path in excluded_paths:
                if os.path.exists(excluded_path):
                    try:
                        ex_df = pd.read_csv(excluded_path)
                        if "Stock" in ex_df.columns:
                            ex_symbols = ex_df["Stock"].dropna().unique().tolist()
                            symbols_set.update(ex_symbols)
                            excluded_count = len(ex_symbols)
                            logger.info(f"📋 Loaded {excluded_count} stocks from excluded list: {excluded_path}")
                            break  # Stop after first successful load
                    except Exception as e:
                        logger.warning(f"Could not read excluded csv {excluded_path}: {e}")
            
            if excluded_count == 0:
                logger.warning("⚠️ No excluded stocks loaded — will only process watchlist")

            if not symbols_set:
                logger.warning(f"No symbols found in watchlist or excluded list. Sleeping 60s...")
                time.sleep(60)
                continue
                
            symbols = sorted(list(symbols_set))
            total_watch = len(symbols)
            logger.info(f"📋 Loaded {watchlist_count} (watchlist) + {excluded_count} (excluded) = {total_watch} total symbols")
            
            # 2. Check DB for stale pledges (refresh every 60-90 days = 2-3 months)
            # This ensures data freshness while not overloading the API
            stale_symbols = []
            with get_connection() as conn:
                with conn.cursor() as cur:
                    for sym in symbols:
                        cur.execute("""
                            SELECT updated_at 
                            FROM promoter_pledge_cache 
                            WHERE symbol = %s 
                              AND updated_at >= NOW() - INTERVAL '75 days'
                        """, (sym,))
                        if not cur.fetchone():
                            stale_symbols.append(sym)
                            
            processed_base = total_watch - len(stale_symbols)

            if not stale_symbols:
                logger.info(f"✅ All pledges are fresh (updated within 75 days). Sleeping for 4 hours.")
                upsert_scanner_health("Pledge Worker", "IDLE", last_success=datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(), today_alerts=total_watch, error_msg=f"Last: None | Total: {total_watch}")
                time.sleep(4 * 3600)
                continue
                
            logger.info(f"Found {len(stale_symbols)} symbols needing pledge updates (out of {total_watch} total).")
            upsert_scanner_health("Pledge Worker", "RUNNING", today_alerts=processed_base, error_msg=f"Last: Starting... | Total: {total_watch}")
            
            def process_symbol(sym, i_total, is_retry=False):
                """Returns True if successful or definitive failure (like 404), False if should retry."""
                target_url = discover_trendlyne_url(sym, api_key)
                prefix = "[RETRY]" if is_retry else f"[{i_total}/{len(stale_symbols)}]"
                logger.info(f"{prefix} Scraping pledge for {sym} at {target_url}")
                
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
                        return True
                    elif res.status_code == 404:
                        logger.warning(f"❌ 404 Not Found for {sym} at {target_url}")
                        mark_failure('scraperapi', f"404 Not Found: {target_url}")
                        # Cache the 404 temporarily so we don't spam it
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO promoter_pledge_cache (symbol, pledge_pct, updated_at)
                                    VALUES (%s, %s, NOW() - INTERVAL '23 days')
                                    ON CONFLICT (symbol) DO UPDATE 
                                    SET updated_at = NOW() - INTERVAL '23 days'
                                """, (sym, 0.0))
                                conn.commit()
                        return True # Don't retry 404s
                    else:
                        logger.warning(f"❌ HTTP {res.status_code} for {sym}")
                        mark_failure('scraperapi', f"HTTP {res.status_code} URL={target_url}")
                        return False
                except Exception as e:
                    logger.exception(f"Exception scraping {sym}: {e}")
                    mark_failure('scraperapi', str(e))
                    return False

            failed_queue = []
            successful_in_first_pass = 0
            
            for i, sym in enumerate(stale_symbols):
                success = process_symbol(sym, i+1)
                if success:
                    successful_in_first_pass += 1
                else:
                    failed_queue.append(sym)
                    
                
                # Update health with processed count
                from datetime import datetime
                from zoneinfo import ZoneInfo
                now_str = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
                current_processed = processed_base + successful_in_first_pass
                upsert_scanner_health("Pledge Worker", "RUNNING", last_success=now_str, today_alerts=current_processed, error_msg=f"Last: {sym} | Total: {total_watch}")

            final_error_count = 0
            
            if failed_queue:
                logger.info(f"Retrying {len(failed_queue)} failed symbols...")
                time.sleep(10) # Brief pause before retries
                for sym in failed_queue:
                    success = process_symbol(sym, 0, is_retry=True)
                    if success:
                        successful_in_first_pass += 1
                    else:
                        final_error_count += 1
                    time.sleep(3)
                    
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    now_str = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
                    current_processed = processed_base + successful_in_first_pass
                    upsert_scanner_health("Pledge Worker", "RUNNING", last_success=now_str, today_alerts=current_processed, error_msg=f"Last: {sym} (Retry) | Total: {total_watch}")

            # Loop done
            status = "IDLE" if final_error_count == 0 else "DOWN"
            last_sym = stale_symbols[-1] if stale_symbols else "None"
            current_processed = processed_base + successful_in_first_pass
            
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now_str = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
            
            if final_error_count > 0:
                err_msg = f"Last: {last_sym} | Total: {total_watch} | Failed: {final_error_count}"
                logger.warning(f"⚠️ Pledge Worker completed with {final_error_count} failures")
            else:
                err_msg = f"Last: {last_sym} | Total: {total_watch}"
                logger.info(f"✅ Pledge Worker completed successfully for all {total_watch} symbols")
            
            upsert_scanner_health("Pledge Worker", status, last_success=now_str, today_alerts=current_processed, error_msg=err_msg)
            
            # Sleep until 1 AM or 5 PM IST
            from datetime import timedelta
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
            t1 = now_ist.replace(hour=1, minute=0, second=0, microsecond=0)
            t2 = now_ist.replace(hour=17, minute=0, second=0, microsecond=0)
            if now_ist < t1:
                next_run = t1
            elif now_ist < t2:
                next_run = t2
            else:
                next_run = t1 + timedelta(days=1)
            sleep_secs = (next_run - now_ist).total_seconds()
            logger.info(f"🕒 Processed: {current_processed}/{total_watch}. Sleeping {int(sleep_secs)}s until {next_run.strftime('%Y-%m-%d %H:%M:%S')} IST")
            time.sleep(sleep_secs)
            
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

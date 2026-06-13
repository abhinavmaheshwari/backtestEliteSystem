import os
import time
import logging
import threading
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)

def run_worker_loop():
    """Infinite loop that scans the watchlist CSV and fetches AI concall reports."""
    from config import WATCHLIST_PATH
    from database import get_recent_concall_analysis, upsert_scanner_health
    from dashboard_server import fetch_and_analyze_concall
    
    logger.info("🤖 AI Worker Thread Started. Monitoring watchlist for missing caches...")
    upsert_scanner_health("AI Worker", "IDLE", last_success=None, today_alerts=0)
    
    while True:
        try:
            if not os.path.exists(WATCHLIST_PATH):
                time.sleep(300) # Sleep 5 mins if watchlist doesn't exist yet
                continue
                
            # Read the latest watchlist (ensure we read the .csv file, not the .parquet)
            csv_path = WATCHLIST_PATH.replace(".parquet", ".csv")
            if not os.path.exists(csv_path):
                time.sleep(300)
                continue
                
            df = pd.read_csv(csv_path)
            
            # We want to analyze all stocks in the fundamental watchlist
            top_stocks = df["Stock"].tolist()
            total_stocks = len(top_stocks)
            
            processed_count = 0
            upsert_scanner_health("AI Worker", "OK", last_success=datetime.now().isoformat(), today_alerts=processed_count)

            for i, sym in enumerate(top_stocks):
                try:
                    # 1. Check if we already have a cache for this stock
                    cached = get_recent_concall_analysis(sym, max_age_days=60)
                    if cached:
                        # Already cached, skip
                        processed_count += 1
                        upsert_scanner_health("AI Worker", "OK", last_success=datetime.now().isoformat(), today_alerts=processed_count)
                        continue
                        
                    # 2. No cache. Fetch it.
                    logger.info(f"🤖 [AI WORKER] Missing cache for {sym} ({i+1}/{total_stocks}). Fetching live...")
                    result = fetch_and_analyze_concall(sym)
                    
                    if result and "error" not in result:
                        conf = result.get("management_confidence", "N/A")
                        key_used = result.get("key_used", "Key 1")
                        logger.info(f"✅ [AI WORKER] Successfully cached analysis for {sym} | Confidence: {conf} | {key_used}")
                        processed_count += 1
                        upsert_scanner_health("AI Worker", "OK", last_success=datetime.now().isoformat(), today_alerts=processed_count)
                    else:
                        logger.warning(f"⚠️ [AI WORKER] Failed to cache {sym}: {result.get('error', 'Unknown Error')}")
                        upsert_scanner_health("AI Worker", "OK", last_success=datetime.now().isoformat(), today_alerts=processed_count, error_msg=result.get('error', 'Unknown Error'))
                        
                    # Sleep 5 seconds between successful fetches to gently pace the API
                    time.sleep(5)
                    
                except Exception as e:
                    logger.error(f"❌ [AI WORKER] Error processing {sym}: {e}")
                    upsert_scanner_health("AI Worker", "DOWN", error_msg=str(e))
                    time.sleep(10) # Sleep a bit longer on error
                    
        except Exception as e:
            logger.error(f"❌ [AI WORKER] Main loop crashed: {e}")
            upsert_scanner_health("AI Worker", "DOWN", error_msg=str(e))
            
        # Once we've checked the whole list, sleep for 30 minutes before checking again
        logger.info(f"🤖 [AI WORKER] Finished scanning entire universe ({total_stocks} stocks). Sleeping for 30 minutes.")
        upsert_scanner_health("AI Worker", "IDLE", last_success=datetime.now().isoformat(), today_alerts=processed_count)
        time.sleep(1800)

def start_worker():
    """Starts the AI worker in a daemon thread."""
    thread = threading.Thread(target=run_worker_loop, daemon=True)
    thread.start()
    return thread

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker_loop()

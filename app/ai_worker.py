import os
import time
import logging
import threading
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)

def run_worker_loop():
    """Infinite loop that scans the watchlist CSV and fetches AI concall reports."""
    from app.config import WATCHLIST_PATH
    from app.database import get_recent_concall_analysis
    from app.dashboard_server import fetch_and_analyze_concall
    
    logger.info("🤖 AI Worker Thread Started. Monitoring watchlist for missing caches...")
    
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
            
            # We only want to analyze the top 100 stocks to save API credits
            top_stocks = df.head(100)["Stock"].tolist()
            
            for i, sym in enumerate(top_stocks):
                try:
                    # 1. Check if we already have a cache for this stock
                    cached = get_recent_concall_analysis(sym, max_age_days=60)
                    if cached:
                        # Already cached, skip
                        continue
                        
                    # 2. No cache. Fetch it.
                    logger.info(f"🤖 [AI WORKER] Missing cache for {sym} ({i+1}/100). Fetching live...")
                    result = fetch_and_analyze_concall(sym)
                    
                    if result and "error" not in result:
                        conf = result.get("management_confidence", "N/A")
                        key_used = result.get("key_used", "Key 1")
                        logger.info(f"✅ [AI WORKER] Successfully cached analysis for {sym} | Confidence: {conf} | {key_used}")
                    else:
                        logger.warning(f"⚠️ [AI WORKER] Failed to cache {sym}: {result.get('error', 'Unknown Error')}")
                        
                    # Sleep 5 seconds between successful fetches to gently pace the API
                    time.sleep(5)
                    
                except Exception as e:
                    logger.error(f"❌ [AI WORKER] Error processing {sym}: {e}")
                    time.sleep(10) # Sleep a bit longer on error
                    
        except Exception as e:
            logger.error(f"❌ [AI WORKER] Main loop crashed: {e}")
            
        # Once we've checked the whole list, sleep for 30 minutes before checking again
        logger.info("🤖 [AI WORKER] Finished scanning Top 100 universe. Sleeping for 30 minutes.")
        time.sleep(1800)

def start_worker():
    """Starts the AI worker in a daemon thread."""
    thread = threading.Thread(target=run_worker_loop, daemon=True)
    thread.start()
    return thread

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker_loop()

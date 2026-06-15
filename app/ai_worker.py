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
    from database import get_recent_concall_analysis, upsert_scanner_health, get_total_cached_concalls, upsert_fetch_error, save_concall_analysis
    from dashboard_server import fetch_and_analyze_concall
    
    logger.info("🤖 AI Worker Thread Started. Monitoring watchlist for missing caches...")
    
    # Initialize with actual processed count instead of 0
    db_processed_count = get_total_cached_concalls()
    upsert_scanner_health("AI Worker", "IDLE", last_success=None, today_alerts=db_processed_count, error_msg="Status: Booting up")
    
    while True:
        try:
            if not os.path.exists(WATCHLIST_PATH):
                upsert_scanner_health("AI Worker", "IDLE", today_alerts=get_total_cached_concalls(), error_msg="Status: Waiting for watchlist.parquet")
                time.sleep(300) # Sleep 5 mins if watchlist doesn't exist yet
                continue
                
            # Read the latest watchlist directly from parquet
            try:
                df = pd.read_parquet(WATCHLIST_PATH)
            except Exception as e:
                logger.error(f"Failed to read parquet watchlist: {e}")
                upsert_scanner_health("AI Worker", "IDLE", today_alerts=get_total_cached_concalls(), error_msg=f"Status: Error reading parquet: {e}")
                time.sleep(300)
                continue
                
            pending_stocks = df["Stock"].tolist()
            logger.info(f"📋 Loaded {len(pending_stocks)} stocks from watchlist parquet")
            
            # Read excluded stocks so they are pre-cached if they break out later
            excluded_csv_paths = [
                os.path.join(os.path.dirname(WATCHLIST_PATH), 'elite_fundamental_watchlist_excluded.csv'),
                os.path.join(os.path.dirname(WATCHLIST_PATH), 'elite_fundamental_watchlist-excluded.csv'),
                WATCHLIST_PATH.replace('.parquet', '_excluded.csv'),
            ]
            excluded_loaded = 0
            for excluded_csv_path in excluded_csv_paths:
                if os.path.exists(excluded_csv_path):
                    try:
                        df_ex = pd.read_csv(excluded_csv_path)
                        if 'Stock' in df_ex.columns:
                            ex_stocks = df_ex['Stock'].dropna().tolist()
                            pending_stocks.extend(ex_stocks)
                            excluded_loaded = len(ex_stocks)
                            logger.info(f"📋 Loaded {excluded_loaded} stocks from excluded list: {excluded_csv_path}")
                            break  # Stop after first successful load
                    except Exception as e:
                        logger.warning(f"Failed to load exclusion list {excluded_csv_path}: {e}")
            
            if excluded_loaded == 0:
                logger.warning("⚠️ No excluded stocks loaded — will only process watchlist")

            # Deduplicate and sort
            pending_stocks = sorted(list(set(pending_stocks)))
            total_stocks = len(pending_stocks)

            db_processed_count = get_total_cached_concalls()
            # Store initial boot info in scanner health so UI can show totals quickly
            try:
                upsert_scanner_health("AI Worker", "OK", last_success=datetime.now().isoformat(), today_alerts=db_processed_count, error_msg=None)
            except Exception:
                logger.exception("Failed to upsert initial AI Worker health")


            max_retries = 3
            global_penalty_idx = 0
            final_failed_count = 0
            
            for attempt in range(max_retries):
                failed_stocks = []
                for i, sym in enumerate(pending_stocks):
                    try:
                        # 1. Check if we already have a cache for this stock
                        cached = get_recent_concall_analysis(sym, max_age_days=60)
                        if cached:
                            # Already cached, skip
                            db_processed_count = get_total_cached_concalls()
                            upsert_scanner_health("AI Worker", "RUNNING", last_success=datetime.now().isoformat(), today_alerts=db_processed_count, error_msg=f"Last: {sym} | Total: {total_stocks}")
                            continue
                            
                        # 2. No cache. Fetch it.
                        logger.info(f"🤖 [AI WORKER] Missing cache for {sym} ({i+1}/{len(pending_stocks)} in batch). Fetching live...")
                        result = fetch_and_analyze_concall(sym)
                        
                        if result and "error" not in result:
                            global_penalty_idx = 0
                            conf = result.get("management_confidence", "N/A")
                            key_used = result.get("key_used", "Key 1")
                            logger.info(f"✅ [AI WORKER] Successfully cached analysis for {sym} | Confidence: {conf} | {key_used}")
                            db_processed_count = get_total_cached_concalls()
                            upsert_scanner_health("AI Worker", "RUNNING", last_success=datetime.now().isoformat(), today_alerts=db_processed_count, error_msg=f"Last: {sym} | Total: {total_stocks}")
                        else:
                            error_msg = result.get('error', 'Unknown Error')
                            logger.warning(f"⚠️ [AI WORKER] Failed to cache {sym}: {error_msg}")
                            db_processed_count = get_total_cached_concalls()
                            # Record the failure so admin UI can show it per-symbol
                            try:
                                upsert_fetch_error('ai', 'AI Worker', sym, None, 'ai_concall', error_msg)
                            except Exception:
                                logger.exception("Failed to upsert fetch_error for AI Worker")

                            upsert_scanner_health("AI Worker", "RUNNING", last_success=datetime.now().isoformat(), today_alerts=db_processed_count, error_msg=f"Last: {sym} | Total: {total_stocks}")

                            # Only retry if it's an API/parsing error, not if it just lacks a transcript on NSE
                            if "No recent concall transcripts" not in error_msg:
                                failed_stocks.append(sym)
                                if "All AI models" in error_msg or "429" in error_msg:
                                    penalty = [300, 900, 1800][min(global_penalty_idx, 2)]
                                    logger.warning(f"⚠️ [AI WORKER] Global API failure. Backing off for {penalty//60} minutes...")
                                    time.sleep(penalty)
                                    global_penalty_idx += 1
                            else:
                                # Save negative cache so it doesn't infinitely retry on the next 30-min global loop
                                save_concall_analysis(sym, f"NONE_{sym}", {"error": error_msg})
                            
                        # Sleep 5 seconds between successful fetches to gently pace the API
                        time.sleep(5)
                        
                    except Exception as e:
                        logger.error(f"❌ [AI WORKER] Error processing {sym}: {e}")
                        # Log to fetch_errors for per-stock error tracking (NOT scanner_health - individual stock failure is non-critical)
                        try:
                            upsert_fetch_error('ai', 'AI Worker', sym, None, 'ai_concall_failure', str(e))
                        except Exception:
                            logger.exception(f"Failed to upsert fetch_error for {sym}")
                        failed_stocks.append(sym)
                        time.sleep(10) # Sleep a bit longer on error
                
                if not failed_stocks:
                    break
                
                pending_stocks = failed_stocks
                if attempt < max_retries - 1:
                    logger.info(f"🤖 [AI WORKER] {len(failed_stocks)} stocks failed. Retrying in 60s (Attempt {attempt+2}/{max_retries})...")
                    time.sleep(60)
                else:
                    logger.error(f"❌ [AI WORKER] Giving up on {len(failed_stocks)} stocks after {max_retries} attempts.")
                    final_failed_count = len(failed_stocks)
                    # Record final failures into fetch_errors and update scanner health so admin can triage
                    for fsym in failed_stocks:
                        try:
                            upsert_fetch_error('ai', 'AI Worker', fsym, None, 'ai_concall', 'Giving up after retries')
                        except Exception:
                            logger.exception(f"Failed to upsert final fetch_error for {fsym}")
                    
        except Exception as e:
            logger.error(f"❌ [AI WORKER] Main loop crashed: {e}")
            upsert_scanner_health("AI Worker", "DOWN", error_msg=str(e))
            
        # Once we've checked the whole list, sleep for 30 minutes before checking again
        db_processed_count = get_total_cached_concalls()
        logger.info(f"🤖 [AI WORKER] Finished scanning entire universe ({total_stocks} stocks). Sleeping for 30 minutes.")
        
        status = "IDLE" if final_failed_count == 0 else "DOWN"
        error_msg = f"Last: Finished | Total: {total_stocks} | Failed: {final_failed_count}" if final_failed_count > 0 else f"Last: Finished | Total: {total_stocks}"
        
        upsert_scanner_health("AI Worker", status, last_success=datetime.now().isoformat(), today_alerts=db_processed_count, error_msg=error_msg)
        
        # Sleep until 1 AM or 5 PM IST
        from datetime import timedelta
        from zoneinfo import ZoneInfo
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
        logger.info(f"🤖 [AI WORKER] Sleeping {int(sleep_secs)}s until {next_run.strftime('%Y-%m-%d %H:%M:%S')} IST")
        time.sleep(sleep_secs)

def start_worker():
    """Starts the AI worker in a daemon thread."""
    thread = threading.Thread(target=run_worker_loop, daemon=True)
    thread.start()
    return thread

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker_loop()

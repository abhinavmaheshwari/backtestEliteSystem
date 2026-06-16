import os
import time
import logging
import threading
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST_ZONE = ZoneInfo("Asia/Kolkata")

def is_in_window() -> bool:
    """Check if current time is between 7 PM IST and 7 AM IST."""
    now = datetime.now(IST_ZONE)
    return now.hour >= 19 or now.hour < 7

def wait_until_next_window() -> float:
    """Calculate seconds until the next 7 PM IST."""
    now = datetime.now(IST_ZONE)
    target = now.replace(hour=19, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()

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
        # Check active scheduling window (7 PM - 7 AM IST)
        if not is_in_window():
            sleep_secs = wait_until_next_window()
            logger.info(f"🤖 [AI WORKER] Outside active window (7 PM - 7 AM IST). Sleeping {sleep_secs:.1f}s until 7 PM IST...")
            upsert_scanner_health("AI Worker", "IDLE", today_alerts=get_total_cached_concalls(), error_msg="Outside active window (7 PM - 7 AM IST)")
            time.sleep(sleep_secs)
            continue

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

            # Pre-filter to only those that actually need processing today
            actual_pending = []
            for sym in pending_stocks:
                # Check 60-day valid cache
                cached = get_recent_concall_analysis(sym, max_age_days=60)
                if cached and not (isinstance(cached, dict) and "error" in cached):
                    continue
                # Check today's negative cache
                cached_today = get_recent_concall_analysis(sym, max_age_days=1)
                if cached_today and isinstance(cached_today, dict) and "error" in cached_today:
                    continue
                actual_pending.append(sym)

            db_processed_count = get_total_cached_concalls()
            
            if not actual_pending:
                sleep_secs = wait_until_next_window()
                logger.info(f"🤖 [AI WORKER] All {total_stocks} stocks are already processed today. Sleeping {sleep_secs:.1f}s until tomorrow 7 PM IST...")
                upsert_scanner_health("AI Worker", "IDLE", last_success=datetime.now().isoformat(), today_alerts=db_processed_count, error_msg=f"All processed | Total: {total_stocks}")
                time.sleep(sleep_secs)
                continue

            logger.info(f"🤖 [AI WORKER] Found {len(actual_pending)}/{total_stocks} stocks requiring analysis.")

            max_retries = 3
            global_penalty_idx = 0
            final_failed_count = 0
            
            for attempt in range(max_retries):
                failed_stocks = []
                for i, sym in enumerate(actual_pending):
                    try:
                        # Fetch it directly (pre-filtering already skipped cached ones)
                        logger.info(f"🤖 [AI WORKER] Missing cache for {sym} ({i+1}/{len(actual_pending)} in batch). Fetching live...")
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

                            # For rate limit / temporary API failures, retry
                            if "429" in error_msg or "All AI models" in error_msg:
                                failed_stocks.append(sym)
                                penalty = [300, 900, 1800][min(global_penalty_idx, 2)]
                                logger.warning(f"⚠️ [AI WORKER] Global API rate limit/failure. Backing off for {penalty//60} minutes...")
                                time.sleep(penalty)
                                global_penalty_idx += 1
                            else:
                                # For data-not-found, extraction failed, or other specific errors:
                                # Save negative cache immediately and do not retry today
                                logger.warning(f"⚠️ [AI WORKER] Saving negative cache for {sym} - {error_msg}, will not retry today")
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
                
                actual_pending = failed_stocks
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
                            save_concall_analysis(fsym, f"NONE_{fsym}", {"error": "Giving up after retries"})
                        except Exception:
                            logger.exception(f"Failed to upsert final fetch_error for {fsym}")
                    
        except Exception as e:
            logger.error(f"❌ [AI WORKER] Main loop crashed: {e}")
            upsert_scanner_health("AI Worker", "DOWN", error_msg=str(e))
            
        # Once we've checked the whole list, do a quick recheck in 5 minutes
        db_processed_count = get_total_cached_concalls()
        logger.info(f"🤖 [AI WORKER] Finished scanning universe ({total_stocks} stocks). Rechecking in 5 minutes for updates.")
        
        status = "IDLE" if final_failed_count == 0 else "DOWN"
        error_msg = f"Last: Finished | Total: {total_stocks} | Failed: {final_failed_count}" if final_failed_count > 0 else f"Last: Finished | Total: {total_stocks}"
        
        upsert_scanner_health("AI Worker", status, last_success=datetime.now().isoformat(), today_alerts=db_processed_count, error_msg=error_msg)
        
        # Sleep for 5 minutes before rechecking (allows watchlist updates)
        time.sleep(300)

def start_worker():
    """Starts the AI worker in a daemon thread."""
    thread = threading.Thread(target=run_worker_loop, daemon=True)
    thread.start()
    return thread

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker_loop()

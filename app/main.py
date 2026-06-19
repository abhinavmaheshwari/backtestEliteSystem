# =====================================================================================
# app/main.py  — SELF-HEALING ORCHESTRATOR
#
# RAILWAY FIX: Flask (dashboard) runs in the MAIN thread so Railway's health check
# gets a response immediately. The watchdog loop and all scanners run as daemon
# threads in the background. This is the correct pattern for Railway deployments.
#
# EOD / REVERSAL run ONCE at 18:30 IST. They are NOT auto-restarted on crash.
# Instead, any crash or zero-alert result sends a Telegram notification.
# =====================================================================================

import sys
import os
import threading
import logging
import time
import traceback
import signal
import random
from zoneinfo import ZoneInfo
from datetime import datetime, time as dt_time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Map watchdog thread names to dashboard database keys
THREAD_TO_SCANNER = {
    "IntradayScanner":    "INTRADAY",
    "LiveScanner":        "1H",
    "EODScanner":         "EOD",
    "ReversalScanner":    "REVERSAL",
}

# Lazy import — dashboard_server may not be ready yet at module load
def _notify_down(name: str, err: str):
    if name == "PerformanceTracker":
        return
    try:
        scanner_name = THREAD_TO_SCANNER.get(name, name)
        from dashboard_server import notify_scanner_down
        notify_scanner_down(scanner_name, err)
        # Telegram notification removed (2026-06-17)
    except Exception:
        pass

def _clear_down(name: str):
    if name == "PerformanceTracker":
        return
    try:
        scanner_name = THREAD_TO_SCANNER.get(name, name)
        from dashboard_server import clear_scanner_down
        clear_scanner_down(scanner_name)
        # Telegram notification removed (2026-06-17)
    except Exception:
        pass

# ── Scan windows (start_time, end_time) ─────────────────────────────────────────────
WINDOWS = {
    "intraday": (dt_time(9, 32),  dt_time(15, 30)),
    "live":     (dt_time(10, 17), dt_time(15, 30)),
    "eod":      (dt_time(18, 30), dt_time(23, 59, 59)),
    "reversal": (dt_time(18, 30), dt_time(23, 59, 59)),
}


# =====================================================================================
# HELPERS
# =====================================================================================

def _cleanup_old_scanner_names():
    from database import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM scanner_health WHERE scanner_name ILIKE '%worker%' OR scanner_name ILIKE '%wealthengine%';")
                # Reset stale DOWN status from previous crashes for main scanners.
                # On boot, every scanner starts fresh — it will set its own status
                # once it completes its first cycle. This prevents old DOWN entries
                # from a previous deploy from showing RED on the dashboard.
                cur.execute("""
                    UPDATE scanner_health 
                    SET status='OK', error_msg=NULL, is_acknowledged=TRUE
                    WHERE scanner_name IN ('INTRADAY', '1H', 'EOD', 'REVERSAL', 'Wealth Engine', 'DAILY_BUILDER')
                      AND status = 'DOWN';
                """)
            conn.commit()
    except Exception as e:
        logger.warning(f"Failed to cleanup old scanner names: {e}")

def wait_for_window(name: str):
    """Block until the scan window opens (weekday only)."""
    start_time, end_time = WINDOWS[name]
    while True:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            logger.info(f"[{name}] 📅 Weekend — sleeping 1 hour...")
            time.sleep(3600)
            continue
        if now.time() > end_time:
            logger.info(f"[{name}] 🕒 Past window end ({end_time}) — waiting for tomorrow...")
            time.sleep(1800)  # Sleep 30 minutes before checking again
            continue
        if now.time() >= start_time:
            logger.info(f"[{name}] ✅ Window open | {now.strftime('%H:%M:%S')} | Launching scanner")
            return
        time.sleep(60)


# =====================================================================================
# WATCHLIST PRE-FLIGHT
# =====================================================================================
from config import WATCHLIST_PATH
import threading as _threading

_watchlist_ready = _threading.Event()

def _build_watchlist_background():
    if os.path.exists(WATCHLIST_PATH):
        logger.info(f"✅ Watchlist found | {WATCHLIST_PATH}")
        _watchlist_ready.set()
        return
    logger.info("📋 Watchlist missing | Attempting to restore or build in background thread...")
    try:
        from watchlist_cache import get_watchlist
        get_watchlist()
    except Exception:
        logger.exception("❌ Daily builder failed — scanners will rebuild at first scan cycle")
    finally:
        _watchlist_ready.set()

_threading.Thread(target=_build_watchlist_background, name="WatchlistBuilder", daemon=True).start()


# =====================================================================================
# THREAD RUNNERS — intraday / live  (self-healing via watchdog)
# =====================================================================================

active_threads = {}

def _run(name, fn):
    try:
        _clear_down(name)
        fn()
        threading.current_thread().completed_cleanly = True
    except Exception as exc:
        logger.exception(f"❌ Unhandled exception in {name}")
        threading.current_thread().completed_cleanly = False
        _notify_down(name, str(exc)[:200])

def run_intraday_scanner():
    wait_for_window("intraday")
    import intraday
    intraday.start()

def run_live_scanner():
    wait_for_window("live")
    import live_scanner
    live_scanner.start()

def run_performance_tracker():
    """Refreshes dashboard data every 5 minutes all day on weekdays."""
    from performance_tracker import build_performance_data
    
    # Always run once on boot to ensure fresh dashboard data, even on weekends
    try:
        build_performance_data()
    except Exception:
        logger.exception("❌ PERFORMANCE TRACKER | Initial boot refresh failed")
        
    while True:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            logger.info("📊 PERFORMANCE TRACKER | Weekend | Sleeping 1 hour...")
            time.sleep(3600)
            continue
        try:
            build_performance_data()
        except Exception:
            logger.exception("❌ PERFORMANCE TRACKER | Refresh failed")
        time.sleep(300)


# =====================================================================================
# SINGLE-SHOT RUNNERS — EOD & Reversal
#
# Rules:
#   • Runs between 18:30 IST and midnight.
#   • If the scan raises an exception  → send Telegram crash alert, and RETRY in 5 minutes.
#   • Once it finishes successfully    → do NOT run again until the next day's window.
# =====================================================================================

def run_eod_scanner():
    """
    EOD Scanner:
    - Wait for 6:30 PM window
    - Run scan
    - On SUCCESS: Mark completed and EXIT cleanly
    - On ERROR: Retry every minute until midnight, then force stop
    """
    retry_count = 0
    while True:
        wait_for_window("eod")
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")
        
        # Check database if we already succeeded today
        try:
            from database import get_all_scanner_health
            health_records = get_all_scanner_health()
            already_ran = False
            for rec in health_records:
                if rec.get("scanner_name") == "EOD" and rec.get("status") == "OK" and rec.get("last_success"):
                    last_success_str = str(rec["last_success"])
                    if last_success_str.startswith(today_str):
                        already_ran = True
                        break
            
            if already_ran:
                logger.info("📊 EOD SCAN | Already successfully executed today. Sleeping until tomorrow...")
                time.sleep(3600)  # Sleep 1 hour
                continue
        except Exception as e:
            logger.warning(f"Could not verify EOD previous run status: {e}")
        
        try:
            logger.info(f"📊 EOD SCAN | Starting scan for {today_str}...")
            import eod_scanner
            total = eod_scanner.start()   # returns int
            if total == 0:
                msg = (
                    f"📊 EOD SCAN — {today_str}\n"
                    f"ℹ️ No breakout setups found today.\n"
                    f"All stocks screened — none passed the filters."
                )
                logger.info("📊 EOD | Zero alerts — no Telegram notification (removed 2026-06-17)")
            else:
                logger.info(f"📊 EOD | Completed — {total} alert(s) sent")
            
            # Successfully finished EOD scan for today — MARK COMPLETED AND EXIT
            from database import upsert_scanner_health
            upsert_scanner_health(
                "EOD",
                status="OK",
                last_success=datetime.now(IST).isoformat(),
                today_alerts=total,
                scheduled_for="06:30 IST"
            )
            logger.info("✅ EOD SCANNER | Completed successfully — exiting")
            retry_count = 0  # reset on successful completion
            # Mark thread as completed cleanly so watchdog doesn't restart
            import threading
            threading.current_thread().completed_cleanly = True
            return
            
        except Exception as exc:
            retry_count += 1
            now = datetime.now(IST)
            
            # Force stop at midnight
            if now.hour == 0 or now.hour >= 1:
                logger.critical(f"⏰ MIDNIGHT PASSED — EOD scanner force-stopping after {retry_count} retries")
                from database import upsert_scanner_health
                upsert_scanner_health(
                    "EOD",
                    status="DOWN",
                    error_msg=f"Stopped at midnight after {retry_count} failed attempts",
                    scheduled_for="06:30 IST"
                )
                # Telegram notification removed (2026-06-17)
                import threading
                threading.current_thread().completed_cleanly = True
                return
            
            # Retry logic
            tb = traceback.format_exc()
            msg = (
                f"🚨 EOD SCAN FAILED — {now.strftime('%Y-%m-%d')} (Retry #{retry_count})\n"
                f"Error: {exc}\n\n"
                f"{tb[-500:]}"
            )
            logger.critical(f"💀 EOD scanner crashed (attempt {retry_count}): {exc}. Retrying in 1 minute...")
            
            from database import upsert_scanner_health
            upsert_scanner_health(
                "EOD",
                status="DOWN",
                error_msg=str(exc)[:500],
                retry_count=retry_count,
                scheduled_for="06:30 IST"
            )
            
            wait_time = min(300, (2 ** retry_count) * random.uniform(0.5, 1.5))
            logger.info(f"⏳ Sleeping for {wait_time:.1f}s before next EOD retry...")
            time.sleep(wait_time)


def run_reversal_scanner():
    """
    REVERSAL Scanner:
    - Wait for 6:30 PM window
    - Run scan
    - On SUCCESS: Mark completed and EXIT cleanly
    - On ERROR: Retry every minute until midnight, then force stop
    """
    retry_count = 0
    while True:
        wait_for_window("reversal")
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")
        
        # Check database if we already succeeded today
        try:
            from database import get_all_scanner_health
            health_records = get_all_scanner_health()
            already_ran = False
            for rec in health_records:
                if rec.get("scanner_name") == "REVERSAL" and rec.get("status") == "OK" and rec.get("last_success"):
                    last_success_str = str(rec["last_success"])
                    if last_success_str.startswith(today_str):
                        already_ran = True
                        break
            
            if already_ran:
                logger.info("🔄 REVERSAL SCAN | Already successfully executed today. Sleeping until tomorrow...")
                time.sleep(3600)  # Sleep 1 hour
                continue
        except Exception as e:
            logger.warning(f"Could not verify REVERSAL previous run status: {e}")
        
        try:
            logger.info(f"🔄 REVERSAL SCAN | Starting scan for {today_str}...")
            import reversal_scanner
            total = reversal_scanner.start()   # returns int
            if total == 0:
                msg = (
                    f"🔄 REVERSAL SCAN — {today_str}\n"
                    f"ℹ️ No mean-reversion setups found today.\n"
                    f"All stocks screened — none passed the filters."
                )
                logger.info("🔄 REVERSAL | Zero alerts — no Telegram notification (removed 2026-06-17)")
            else:
                logger.info(f"🔄 REVERSAL | Completed — {total} alert(s) sent")
            
            # Successfully finished Reversal scan for today — MARK COMPLETED AND EXIT
            from database import upsert_scanner_health
            upsert_scanner_health(
                "REVERSAL",
                status="OK",
                last_success=datetime.now(IST).isoformat(),
                today_alerts=total,
                scheduled_for="06:30 IST"
            )
            logger.info("✅ REVERSAL SCANNER | Completed successfully — exiting")
            retry_count = 0  # reset on successful completion
            # Mark thread as completed cleanly so watchdog doesn't restart
            import threading
            threading.current_thread().completed_cleanly = True
            return
            
        except Exception as exc:
            retry_count += 1
            now = datetime.now(IST)
            
            # Force stop at midnight
            if now.hour == 0 or now.hour >= 1:
                logger.critical(f"⏰ MIDNIGHT PASSED — REVERSAL scanner force-stopping after {retry_count} retries")
                from database import upsert_scanner_health
                upsert_scanner_health(
                    "REVERSAL",
                    status="DOWN",
                    error_msg=f"Stopped at midnight after {retry_count} failed attempts",
                    scheduled_for="06:30 IST"
                )
                # Telegram notification removed (2026-06-17)
                import threading
                threading.current_thread().completed_cleanly = True
                return
            
            # Retry logic
            tb = traceback.format_exc()
            msg = (
                f"🚨 REVERSAL SCAN FAILED — {now.strftime('%Y-%m-%d')} (Retry #{retry_count})\n"
                f"Error: {exc}\n\n"
                f"{tb[-500:]}"
            )
            logger.critical(f"💀 REVERSAL scanner crashed (attempt {retry_count}): {exc}. Retrying in 1 minute...")
            
            from database import upsert_scanner_health
            upsert_scanner_health(
                "REVERSAL",
                status="DOWN",
                error_msg=str(exc)[:500],
                retry_count=retry_count,
                scheduled_for="06:30 IST"
            )
            
            wait_time = min(300, (2 ** retry_count) * random.uniform(0.5, 1.5))
            logger.info(f"⏳ Sleeping for {wait_time:.1f}s before next REVERSAL retry...")
            time.sleep(wait_time)


def run_bayesian_loop():
    """Runs the Bayesian Updater loop. Triggers immediately on boot, then waits 24h."""
    from bayesian_updater import run_bayesian_updater
    while True:
        try:
            logger.info("🧠 BAYESIAN UPDATER | Waking up to process trades...")
            run_bayesian_updater()
        except Exception as e:
            logger.exception("❌ BAYESIAN UPDATER | Crashed")
            # Telegram notification removed (2026-06-17)
        
        # Run daily (86400 seconds)
        logger.info("🧠 BAYESIAN UPDATER | Sleeping for 24h")
        time.sleep(86400)


# =====================================================================================
# TIME-BASED SCHEDULER
# =====================================================================================
def run_system_scheduler():
    """
    Custom time-based scheduler (replaces schedule library for reliability).
    
    Timing:
    - 1:00 AM: Daily Builder (fresh watchlist)
    - 1:05 AM: Wealth Engine (initial setup with fresh watchlist)
    - 8:30 AM: Verify file readiness
    - Market hours (9:15 AM - 3:30 PM): Wealth Engine hourly at :05 to generate new buy signals
    """
    from daily_builder import build_watchlist
    from wealth_engine import run_wealth_scan
    from config import WATCHLIST_PATH, DATA_DIR
    from database import upsert_scanner_health
    
    WEALTH_PATH = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
    
    # Track which tasks have run today
    daily_builder_ran = False
    wealth_initial_ran = False
    verify_scans_ran = False
    last_wealth_market_run = None  # Track last market-hours wealth run

    def safe_run_daily_builder():
        """Run Daily Builder with success tracking."""
        try:
            logger.info("🕒 SCHEDULER | [1:00 AM] Triggering Daily Builder")
            from daily_builder import build_watchlist
            build_watchlist()
            
            # Update memory cache
            from watchlist_cache import get_watchlist
            get_watchlist()
            
            # Mark success
            now_str = datetime.now(IST).isoformat()
            try:
                upsert_scanner_health(
                    "DAILY_BUILDER",
                    status="OK",
                    last_success=now_str,
                    scheduled_for="01:00 IST"
                )
            except Exception:
                logger.warning("⚠️ Could not update Daily Builder health status")
            logger.info("✅ Daily Builder completed successfully")
            return True
        except Exception as e:
            logger.exception("❌ SCHEDULER | Daily Builder crashed")
            # Telegram notifications disabled (2026-06-17)
            try:
                upsert_scanner_health(
                    "DAILY_BUILDER",
                    status="DOWN",
                    error_msg=str(e)[:500],
                    scheduled_for="01:00 IST"
                )
            except Exception:
                pass
            return False

    def safe_run_wealth_scan_initial():
        """Run Wealth Engine at 1:05 AM with fresh watchlist."""
        try:
            logger.info("🕒 SCHEDULER | [1:05 AM] Triggering Wealth Engine (initial setup)")
            run_wealth_scan()
            
            # Mark success
            now_str = datetime.now(IST).isoformat()
            upsert_scanner_health(
                "Wealth Engine",
                status="OK",
                last_success=now_str,
                scheduled_for="01:05 IST"
            )
            logger.info("✅ Wealth Engine (initial) completed successfully")
            return True
        except Exception as e:
            logger.exception("❌ SCHEDULER | Wealth Engine (initial) crashed")
            upsert_scanner_health(
                "Wealth Engine",
                status="DOWN",
                error_msg=str(e)[:500],
                scheduled_for="01:05 IST"
            )
            return False

    def safe_run_wealth_market_hours():
        """Run Wealth Engine during market hours (5-min loop from 9:15 AM to 3:30 PM)."""
        nonlocal last_wealth_market_run
        try:
            now = datetime.now(IST)
            # Only run once per 5 minutes (300 seconds)
            if last_wealth_market_run and (now - last_wealth_market_run).total_seconds() < 300:
                return False
            
            logger.info(f"🕒 SCHEDULER | [{now.strftime('%H:%M')}] Triggering Wealth Engine (market hours - 5min loop)")
            run_wealth_scan()
            
            last_wealth_market_run = now
            # Mark success
            now_str = now.isoformat()
            upsert_scanner_health(
                "Wealth Engine",
                status="OK",
                last_success=now_str,
                scheduled_for="Every 5min (9:15 AM - 3:30 PM)"
            )
            logger.info("✅ Wealth Engine (market hours) completed successfully")
            return True
        except Exception as e:
            logger.exception("❌ SCHEDULER | Wealth Engine (market hours) crashed")
            upsert_scanner_health(
                "Wealth Engine",
                status="DOWN",
                error_msg=str(e)[:500],
                scheduled_for="Every 5min (9:15 AM - 3:30 PM)"
            )
            return False

    def verify_scans():
        """Verify file readiness at 8:30 AM."""
        logger.info("🕒 SCHEDULER | [8:30 AM] Verifying file readiness")
        now = datetime.now(IST)

        # 1. Verify Watchlist — use robust embedded date check to prevent clock-skew issues
        try:
            if not os.path.exists(WATCHLIST_PATH):
                logger.warning("⚠️ Watchlist missing! Forcing rebuild.")
                safe_run_daily_builder()
            else:
                import pandas as pd
                try:
                    df = pd.read_parquet(WATCHLIST_PATH)
                    if "Scan Time" in df.columns and not df.empty:
                        scan_date_str = str(df["Scan Time"].iloc[0])[:10]
                        scan_date = datetime.strptime(scan_date_str, "%Y-%m-%d").date()
                        if scan_date < now.date():
                            logger.warning(f"⚠️ Watchlist stale! Embedded date is {scan_date}, expected {now.date()}. Forcing rebuild.")
                            safe_run_daily_builder()
                        else:
                            logger.info("✅ Watchlist embedded date is fresh.")
                    else:
                        logger.warning("⚠️ Watchlist missing 'Scan Time' column! Forcing rebuild.")
                        safe_run_daily_builder()
                except Exception as e:
                    logger.warning(f"⚠️ Failed to read Watchlist parquet ({e}). Forcing rebuild.")
                    safe_run_daily_builder()
        except Exception:
            logger.exception("Failed to verify watchlist; forcing rebuild.")
            safe_run_daily_builder()

        # 2. Verify Wealth Engine
        try:
            if not os.path.exists(WEALTH_PATH):
                try:
                    from database import download_parquet_from_db
                    restored = download_parquet_from_db("wealth_engine", WEALTH_PATH)
                    if restored and os.path.exists(WEALTH_PATH):
                        logger.info("✅ Wealth system restored from DB.")
                    else:
                        logger.warning("⚠️ Wealth system missing! Forcing run.")
                        safe_run_wealth_scan_initial()
                except Exception:
                    logger.exception("Failed to restore wealth from DB; forcing run.")
                    safe_run_wealth_scan_initial()
            else:
                mtime_ts = os.path.getmtime(WEALTH_PATH)
                mtime = datetime.fromtimestamp(mtime_ts, IST)
                if mtime.date() < now.date():
                    try:
                        from database import download_parquet_from_db
                        restored = download_parquet_from_db("wealth_engine", WEALTH_PATH)
                        if restored and os.path.exists(WEALTH_PATH):
                            logger.info("✅ Wealth system restored from DB.")
                        else:
                            logger.warning("⚠️ Wealth system stale! Forcing run.")
                            safe_run_wealth_scan_initial()
                    except Exception:
                        logger.exception("Failed to restore wealth; forcing run.")
                        safe_run_wealth_scan_initial()
        except Exception:
            logger.exception("Failed to verify wealth system.")

    logger.info("🕒 SCHEDULER | Started (custom time-based scheduler)")
    
    # Run boot verification
    verify_scans()

    # Main scheduler loop
    while True:
        now = datetime.now(IST)
        
        # Weekdays only
        if now.weekday() < 5:  # Mon-Fri
            # 1:00 AM - Daily Builder
            if now.hour == 1 and now.minute >= 0 and not daily_builder_ran:
                daily_builder_ran = True
                safe_run_daily_builder()
            elif now.hour != 1:
                daily_builder_ran = False
            
            # Refresh now in case daily builder blocked for a long time
            now = datetime.now(IST)
            
            # 1:30 AM - Wealth Engine (initial)
            if now.hour == 1 and now.minute >= 30 and not wealth_initial_ran:
                wealth_initial_ran = True
                safe_run_wealth_scan_initial()
            elif now.hour != 1:
                wealth_initial_ran = False
            
            now = datetime.now(IST)
            
            # 9:15 AM - Verify Scans
            if now.hour == 9 and now.minute >= 15 and not verify_scans_ran:
                verify_scans_ran = True
                verify_scans()
            elif now.hour != 9:
                verify_scans_ran = False
            
            # Market hours: Wealth Engine every 5 minutes from 9:15 AM - 3:30 PM
            if (now.hour == 9 and now.minute >= 15) or (10 <= now.hour <= 14) or (now.hour == 15 and now.minute <= 30):
                safe_run_wealth_market_hours()
        
        time.sleep(30)  # Check every 30 seconds


# =====================================================================================
# SELF-HEALING WATCHDOG  (runs in background thread)
#
# EOD and REVERSAL are intentionally excluded from auto-restart — they run once and
# exit.  The watchdog will see completed_cleanly=True and simply drop them.
# =====================================================================================

# Only intraday/live/performance get auto-restarted on crash
from ai_worker import run_worker_loop
from pledge_worker import worker_loop as run_pledge_loop

RESTARTABLE_THREADS = {
    "IntradayScanner":    run_intraday_scanner,
    "LiveScanner":        run_live_scanner,
    "PerformanceTracker": run_performance_tracker,
    "AI Worker":          run_worker_loop,
    "Pledge Worker":      run_pledge_loop,
    "BayesianUpdater":    run_bayesian_loop,
    "SystemScheduler":    run_system_scheduler,
    "EODScanner":         run_eod_scanner,
    "ReversalScanner":    run_reversal_scanner,
}

# EOD and Reversal are now restartable since they run continuously
ONE_SHOT_THREADS = {}

ALL_THREADS = {**RESTARTABLE_THREADS, **ONE_SHOT_THREADS}


def start_thread(name, target):
    t = threading.Thread(target=lambda: _run(name, target), name=name, daemon=True)
    t.completed_cleanly = False
    t.start()
    active_threads[name] = t
    return t


def run_watchdog():
    """Watchdog loop — background daemon thread; Flask owns the main thread."""
    _missing_env = [v for v in ("BOT_TOKEN", "CHAT_ID") if not os.getenv(v)]
    if _missing_env:
        logger.error(f"❌ FATAL: Missing env vars: {_missing_env}")

    for name, target in ALL_THREADS.items():
        start_thread(name, target)

    logger.info("=" * 70)
    logger.info("🛡️  SELF-HEALING WATCHDOG ACTIVE | All Scanners Initialized")
    logger.info("🌐  Dashboard: https://elitebreakoutsystem-production.up.railway.app/")
    logger.info("=" * 70)

    _logged_ready = False
    while True:
        if not _logged_ready and _watchlist_ready.is_set():
            logger.info("✅ Watchlist build complete — all scanners can proceed")
            _logged_ready = True
            
            # --- POST-DEPLOYMENT INSTANT VERIFICATION ---
            # Removed to prevent unnecessary API hits on every restart
            # --------------------------------------------

        for name, thread in list(active_threads.items()):
            if not thread.is_alive():
                if getattr(thread, "completed_cleanly", False):
                    logger.info(f"✅ THREAD COMPLETED CLEANLY: {name} — removing from watchdog.")
                    del active_threads[name]

                elif name in ONE_SHOT_THREADS:
                    # EOD/Reversal crashed without completing cleanly — already sent
                    # Telegram alert inside the runner.  Just drop from tracking.
                    logger.warning(f"⚠️ ONE-SHOT THREAD EXITED UNCLEANLY: {name} — NOT restarting (Telegram already notified).")
                    del active_threads[name]

                else:
                    # Restartable scanner crashed — revive it
                    logger.critical(f"💀 THREAD CRASH: {name} — restarting in 10s...")
                    _notify_down(name, "Thread crashed — restarting")
                    time.sleep(10)
                    start_thread(name, RESTARTABLE_THREADS[name])
                    logger.info(f"🔄 THREAD REVIVED: {name}")

        time.sleep(30)


# =====================================================================================
# ENTRY POINT
# =====================================================================================

if __name__ == "__main__":
    _cleanup_old_scanner_names()
    def handle_sigterm(*args):
        logger.info("🛑 SIGTERM received — container shutting down. Closing gracefuly...")
        # Telegram notification removed (2026-06-17)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)


    watchdog_thread = threading.Thread(target=run_watchdog, name="Watchdog", daemon=True)
    watchdog_thread.start()

    if "--worker" in sys.argv:
        logger.info("🛠️ Running in WORKER mode — decoupling Flask dashboard.")
        while True:
            time.sleep(86400)
    else:
        try:
            from dashboard_server import start_dashboard_server
            port = int(os.getenv("PORT", 8080))
            logger.info(f"🌐 Dashboard server binding to port {port} (main thread)")
            start_dashboard_server()
        except ImportError:
            logger.error("❌ dashboard_server.py not found — Railway will show 'failed to respond'")
            watchdog_thread.join()
        except Exception:
            logger.exception("❌ Dashboard server crashed")
            watchdog_thread.join()

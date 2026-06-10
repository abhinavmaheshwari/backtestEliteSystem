# =====================================================================================
# app/main.py  — SELF-HEALING ORCHESTRATOR
#
# RAILWAY FIX: Flask (dashboard) runs in the MAIN thread so Railway's health check
# gets a response immediately. The watchdog loop and all scanners run as daemon
# threads in the background. This is the correct pattern for Railway deployments.
# =====================================================================================

import sys
import os
import threading
import logging
import time
from zoneinfo import ZoneInfo
from datetime import datetime, time as dt_time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

WINDOWS = {
    "intraday":    (dt_time(9, 32),  dt_time(15, 30)),
    "live":        (dt_time(10, 17), dt_time(15, 30)),
    "eod":         (dt_time(18, 30), dt_time(20, 0)),
    "reversal":    (dt_time(18, 45), dt_time(20, 0)),
}

def wait_for_window(name: str):
    start_time, _ = WINDOWS[name]
    while True:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            logger.info(f"[{name}] 📅 Weekend detected | Sleeping 1 hour...")
            time.sleep(3600)
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
    logger.info("📋 Watchlist missing | Running daily builder in background thread...")
    try:
        from daily_builder import build_watchlist
        build_watchlist()
    except Exception:
        logger.exception("❌ Daily builder failed — scanners will rebuild at first scan cycle")
    finally:
        _watchlist_ready.set()

_threading.Thread(target=_build_watchlist_background, name="WatchlistBuilder", daemon=True).start()

# =====================================================================================
# THREAD RUNNERS
# =====================================================================================

active_threads = {}

def _run(name, fn):
    try:
        fn()
        threading.current_thread().completed_cleanly = True
    except Exception:
        logger.exception(f"❌ Unhandled exception in {name}")
        threading.current_thread().completed_cleanly = False

def run_intraday_scanner():
    wait_for_window("intraday")
    import intraday
    intraday.start()

def run_live_scanner():
    wait_for_window("live")
    import live_scanner
    live_scanner.start()

def run_eod_scanner():
    wait_for_window("eod")
    import eod_scanner
    eod_scanner.start()

def run_reversal_scanner():
    wait_for_window("reversal")
    import reversal_scanner
    reversal_scanner.start()

def run_performance_tracker():
    """
    Runs every 5 minutes ALL day on weekdays — not just a 30-min morning window.
    This ensures the dashboard always reflects the latest prices and new alerts
    regardless of when they fire (intraday, EOD, etc.).
    """
    from performance_tracker import build_performance_data

    while True:
        now = datetime.now(IST)

        # Skip weekends
        if now.weekday() >= 5:
            logger.info("📊 PERFORMANCE TRACKER | Weekend | Sleeping 1 hour...")
            time.sleep(3600)
            continue

        try:
            build_performance_data()
        except Exception:
            logger.exception("❌ PERFORMANCE TRACKER | Refresh failed")

        time.sleep(300)  # refresh every 5 minutes all day

# =====================================================================================
# SELF-HEALING WATCHDOG  (runs in background thread)
# =====================================================================================

THREAD_REGISTRY = {
    "IntradayScanner":    run_intraday_scanner,
    "LiveScanner":        run_live_scanner,
    "EODScanner":         run_eod_scanner,
    "ReversalScanner":    run_reversal_scanner,
    "PerformanceTracker": run_performance_tracker,
}

def start_thread(name, target):
    t = threading.Thread(target=lambda: _run(name, target), name=name, daemon=True)
    t.completed_cleanly = False
    t.start()
    active_threads[name] = t
    return t

def run_watchdog():
    """Watchdog loop — runs as a background daemon thread so Flask owns the main thread."""
    _missing_env = [v for v in ("BOT_TOKEN", "CHAT_ID") if not os.getenv(v)]
    if _missing_env:
        logger.error(f"❌ FATAL: Missing env vars: {_missing_env}")

    # Start all scanner + tracker threads
    for name, target in THREAD_REGISTRY.items():
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

        for name, thread in list(active_threads.items()):
            if not thread.is_alive():
                if getattr(thread, "completed_cleanly", False):
                    logger.info(f"✅ THREAD COMPLETED CLEANLY: {name} — removing from watchdog tracking.")
                    del active_threads[name]
                else:
                    logger.critical(f"💀 THREAD CRASH DETECTED: {name} has died. Auto-restarting in 10 seconds...")
                    time.sleep(10)
                    start_thread(name, THREAD_REGISTRY[name])
                    logger.info(f"🔄 THREAD REVIVED: {name} is back online.")

        time.sleep(30)

# =====================================================================================
# ENTRY POINT
# =====================================================================================

if __name__ == "__main__":
    # ── Launch watchdog + all scanners in background ─────────────────────────────────
    watchdog_thread = threading.Thread(target=run_watchdog, name="Watchdog", daemon=True)
    watchdog_thread.start()

    # ── Flask runs in the MAIN thread — Railway health checks pass immediately ────────
    try:
        from dashboard_server import start_dashboard_server
        port = int(os.getenv("PORT", 8080))
        logger.info(f"🌐 Dashboard server binding to port {port} (main thread)")
        start_dashboard_server()   # blocks here — this is intentional
    except ImportError:
        logger.error("❌ dashboard_server.py not found — Railway will show 'failed to respond'")
        logger.error("   Make sure dashboard_server.py is in the app/ folder")
        watchdog_thread.join()
    except Exception:
        logger.exception("❌ Dashboard server crashed")
        watchdog_thread.join()

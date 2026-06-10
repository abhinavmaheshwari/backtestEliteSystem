# =====================================================================================
# app/main.py  — SELF-HEALING ORCHESTRATOR
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
    "performance": (dt_time(9, 30),  dt_time(10, 0)),   # refreshes every 5 min during morning window
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
    """
    Wrapper: marks thread completed_cleanly=True on normal exit, False on exception.
    Normal exit means the scanner finished its job intentionally (e.g. EOD ran once).
    Exception exit means the thread crashed and should be restarted by the watchdog.
    """
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
    Waits until 20:15 IST (after EOD + reversal have both settled),
    then regenerates performance_data.json once per trading day.
    Skips weekends automatically via wait_for_window.
    Sleeps 24 hours before looping so it only runs once per day.
    """
    while True:
        wait_for_window("performance")
        try:
            from performance_tracker import generate_performance_data
            logger.info("📊 PERFORMANCE TRACKER | Starting daily P&L refresh...")
            generate_performance_data()
            logger.info("✅ PERFORMANCE TRACKER | Done — dashboard data refreshed")
        except Exception:
            logger.exception("❌ PERFORMANCE TRACKER | Failed")
        # Sleep 24 hours before next run (avoids re-triggering in the same window)
        logger.info("⏳ PERFORMANCE TRACKER | Next run in 24 hours")
        time.sleep(86_400)

# =====================================================================================
# SELF-HEALING WATCHDOG
# =====================================================================================

THREAD_REGISTRY = {
    "IntradayScanner":    run_intraday_scanner,
    "LiveScanner":        run_live_scanner,
    "EODScanner":         run_eod_scanner,
    "ReversalScanner":    run_reversal_scanner,
    "PerformanceTracker": run_performance_tracker,
    # DashboardServer is NOT in the registry — it's a permanent blocking thread
    # managed separately and never restarted by the watchdog.
}

def start_thread(name, target):
    t = threading.Thread(target=lambda: _run(name, target), name=name, daemon=True)
    t.completed_cleanly = False
    t.start()
    active_threads[name] = t
    return t

if __name__ == "__main__":
    _missing_env = [v for v in ("BOT_TOKEN", "CHAT_ID") if not os.getenv(v)]
    if _missing_env:
        logger.error(f"❌ FATAL: Missing env vars: {_missing_env}")

    # ── Start Dashboard Server (permanent — runs for the lifetime of the process) ──
    try:
        from dashboard_server import start_dashboard_server
        _dash_thread = threading.Thread(
            target=start_dashboard_server,
            name="DashboardServer",
            daemon=True,
        )
        _dash_thread.start()
        logger.info(f"🌐 Dashboard server started on PORT={os.getenv('PORT', 8080)}")
    except ImportError:
        logger.warning("⚠️ dashboard_server.py not found — web dashboard disabled")
    except Exception:
        logger.exception("❌ Dashboard server failed to start")

    # ── Start All Scanner + Tracker Threads ─────────────────────────────────────────
    for name, target in THREAD_REGISTRY.items():
        start_thread(name, target)

    logger.info("=" * 70)
    logger.info("🛡️  SELF-HEALING WATCHDOG ACTIVE | All Scanners Initialized")
    logger.info("🌐  Dashboard: https://your-app.railway.app/")
    logger.info("=" * 70)

    # ── Watchdog Loop ────────────────────────────────────────────────────────────────
    _logged_ready = False
    while True:
        if not _logged_ready and _watchlist_ready.is_set():
            logger.info("✅ Watchlist build complete — all scanners can proceed")
            _logged_ready = True

        for name, thread in list(active_threads.items()):
            if not thread.is_alive():
                if getattr(thread, "completed_cleanly", False):
                    # ✅ Thread exited normally (e.g. EOD ran once and returned).
                    # Remove from tracking — do NOT restart.
                    logger.info(f"✅ THREAD COMPLETED CLEANLY: {name} — removing from watchdog tracking.")
                    del active_threads[name]
                else:
                    # ❌ Thread crashed — restart it.
                    logger.critical(f"💀 THREAD CRASH DETECTED: {name} has died. Auto-restarting in 10 seconds...")
                    time.sleep(10)
                    start_thread(name, THREAD_REGISTRY[name])
                    logger.info(f"🔄 THREAD REVIVED: {name} is back online.")

        time.sleep(30)

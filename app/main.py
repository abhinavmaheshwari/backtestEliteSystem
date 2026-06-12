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
        _telegram_notify(f"🔴 Scanner {scanner_name} is DOWN!\nError: {err}")
    except Exception:
        pass

def _clear_down(name: str):
    if name == "PerformanceTracker":
        return
    try:
        scanner_name = THREAD_TO_SCANNER.get(name, name)
        from dashboard_server import clear_scanner_down
        clear_scanner_down(scanner_name)
        _telegram_notify(f"🟢 Scanner {scanner_name} is active / recovered.")
    except Exception:
        pass

# ── Scan windows (start_time, end_time) ─────────────────────────────────────────────
WINDOWS = {
    "intraday": (dt_time(9, 32),  dt_time(15, 30)),
    "live":     (dt_time(10, 17), dt_time(15, 30)),
    "eod":      (dt_time(18, 30), dt_time(20, 0)),
    "reversal": (dt_time(18, 30), dt_time(20, 0)),   # same window as EOD
}


# =====================================================================================
# HELPERS
# =====================================================================================

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


def _telegram_notify(text: str):
    """Send a plain Telegram message — best-effort, never raises."""
    try:
        from telegram_engine import send_telegram_message
        send_telegram_message(text, scan_type="SYSTEM")
    except Exception:
        logger.exception("❌ Could not send Telegram system notification")


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
#   • Run ONCE at 18:30 IST on weekdays.
#   • If the scan raises an exception  → send Telegram crash alert, do NOT restart.
#   • If the scan returns 0 alerts     → send Telegram "no alerts" notification.
#   • Thread exits cleanly either way  → watchdog sees completed_cleanly=True and
#     removes it from tracking (no restart).
# =====================================================================================

def run_eod_scanner():
    wait_for_window("eod")
    scan_date = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        import eod_scanner
        total = eod_scanner.start()   # returns int
        if total == 0:
            msg = (
                f"📊 EOD SCAN — {scan_date}\n"
                f"ℹ️ No breakout setups found today.\n"
                f"All stocks screened — none passed the filters."
            )
            logger.info("📊 EOD | Zero alerts — notifying Telegram")
            _telegram_notify(msg)
        else:
            logger.info(f"📊 EOD | Completed — {total} alert(s) sent")
    except Exception as exc:
        tb = traceback.format_exc()
        msg = (
            f"🚨 EOD SCAN FAILED — {scan_date}\n"
            f"Error: {exc}\n\n"
            f"{tb[-800:]}"   # last 800 chars of traceback to stay within Telegram limits
        )
        logger.critical(f"💀 EOD scanner crashed: {exc}")
        _telegram_notify(msg)
        raise exc
    # Thread exits — watchdog will NOT restart (completed_cleanly handled in _run wrapper)


def run_reversal_scanner():
    wait_for_window("reversal")
    scan_date = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        import reversal_scanner
        total = reversal_scanner.start()   # returns int
        if total == 0:
            msg = (
                f"🔄 REVERSAL SCAN — {scan_date}\n"
                f"ℹ️ No mean-reversion setups found today.\n"
                f"All stocks screened — none passed the filters."
            )
            logger.info("🔄 REVERSAL | Zero alerts — notifying Telegram")
            _telegram_notify(msg)
        else:
            logger.info(f"🔄 REVERSAL | Completed — {total} alert(s) sent")
    except Exception as exc:
        tb = traceback.format_exc()
        msg = (
            f"🚨 REVERSAL SCAN FAILED — {scan_date}\n"
            f"Error: {exc}\n\n"
            f"{tb[-800:]}"
        )
        logger.critical(f"💀 REVERSAL scanner crashed: {exc}")
        _telegram_notify(msg)
        raise exc


# =====================================================================================
# SELF-HEALING WATCHDOG  (runs in background thread)
#
# EOD and REVERSAL are intentionally excluded from auto-restart — they run once and
# exit.  The watchdog will see completed_cleanly=True and simply drop them.
# =====================================================================================

# Only intraday/live/performance get auto-restarted on crash
RESTARTABLE_THREADS = {
    "IntradayScanner":    run_intraday_scanner,
    "LiveScanner":        run_live_scanner,
    "PerformanceTracker": run_performance_tracker,
}

# EOD and Reversal are launched once and never restarted
ONE_SHOT_THREADS = {
    "EODScanner":      run_eod_scanner,
    "ReversalScanner": run_reversal_scanner,
}

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
    watchdog_thread = threading.Thread(target=run_watchdog, name="Watchdog", daemon=True)
    watchdog_thread.start()

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

# =====================================================================================
# app/dashboard_server.py
# LIGHTWEIGHT WEB DASHBOARD — serves performance_dashboard.html + JSON via Flask
#
# Railway exposes this on the PORT env var (default 8080).
# Access via: https://your-app.railway.app/
# =====================================================================================
import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, send_file, Response

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

try:
    from config import DATA_DIR, BASE_DIR
except ImportError:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(BASE_DIR, "data")

APP_DIR        = os.path.dirname(os.path.abspath(__file__))
PERF_JSON_PATH = os.path.join(DATA_DIR, "performance_data.json")

# ── Locate the dashboard HTML ────────────────────────────────────────────────────────
_DASHBOARD_CANDIDATES = [
    os.path.join(APP_DIR,  "performance_dashboard.html"),
    os.path.join(BASE_DIR, "performance_dashboard.html"),
]
DASHBOARD_HTML_PATH = next((p for p in _DASHBOARD_CANDIDATES if os.path.exists(p)), None)

app = Flask(__name__)

# ── Disable Flask startup banner in production ───────────────────────────────────────
import logging as _logging
_logging.getLogger("werkzeug").setLevel(_logging.WARNING)

# ── CORS + cache headers on every response ──────────────────────────────────────────
@app.after_request
def add_headers(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Cache-Control"]                = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"]                       = "no-cache"
    return response


@app.route("/")
def index():
    """Serve the performance dashboard HTML."""
    if DASHBOARD_HTML_PATH and os.path.exists(DASHBOARD_HTML_PATH):
        return send_file(DASHBOARD_HTML_PATH)
    return Response(
        "<h2 style='font-family:monospace;color:#00e5a0;background:#0b0e14;margin:0;padding:40px'>"
        "⚠️ performance_dashboard.html not found.<br><br>"
        "Place it in <code>app/</code> or the project root, then redeploy.</h2>",
        mimetype="text/html",
    )


@app.route("/data/performance_data.json")
def performance_json():
    """Serve the latest performance JSON for the dashboard to fetch, loaded from DB."""
    try:
        from database import get_system_state
        val = get_system_state("performance_data")
        if val:
            return Response(val, mimetype="application/json")
    except Exception:
        logger.exception("❌ Failed to load performance data from DB")

    # Return empty-but-valid structure so dashboard doesn't fall back to demo data
    empty = {
        "generated_at": datetime.now(IST).isoformat(),
        "trades": [],
        "summary": {
            "total_alerts":    0,
            "win_rate":        0,
            "winners":         0,
            "losers":          0,
            "avg_return_pct":  0,
            "avg_win_pct":     0,
            "avg_loss_pct":    0,
            "expectancy":      0,
            "best_trade_pct":  0,
            "worst_trade_pct": 0,
            "open_positions":  0,
        },
        "equity_curve": [],
        "monthly":      [],
        "by_scanner":   {},
        "by_category":  {},
    }
    return jsonify(empty), 200


@app.route("/health")
def health():
    """Railway health-check endpoint."""
    perf_exists = False
    perf_age    = None
    try:
        from database import get_system_state
        val = get_system_state("performance_data")
        perf_exists = val is not None
        if perf_exists:
            data = json.loads(val)
            gen_at = data.get("generated_at")
            if gen_at:
                gen_dt = datetime.fromisoformat(gen_at)
                now_dt = datetime.now(IST)
                perf_age = round((now_dt - gen_dt).total_seconds() / 3600, 1)
    except Exception:
        logger.exception("❌ Health check failed to load/parse performance data")

    return jsonify({
        "status":            "ok",
        "time_ist":          datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "performance_ready": perf_exists,
        "performance_age_h": perf_age,
    })


@app.route("/api/summary")
def api_summary():
    """Quick JSON summary — useful for curl checks, loaded from DB."""
    try:
        from database import get_system_state
        val = get_system_state("performance_data")
        if val:
            data = json.loads(val)
            return jsonify(data.get("summary", {}))
    except Exception:
        logger.exception("❌ /api/summary failed")
    return jsonify({"error": "No data yet"}), 404


@app.route("/api/scanner_status")
def api_scanner_status():
    """
    Return per-scanner health stats and today's trades — all sourced from Postgres.
    scanner_health table holds status/last_success/error.
    alerts table is queried live for today's trades per scanner.
    """
    try:
        from database import get_all_scanner_health, get_scanner_today_trades
        from datetime import date as _date
        today_str = _date.today().isoformat()

        health_rows = get_all_scanner_health()
        result = {}
        for row in health_rows:
            sc = row["scanner_name"]
            today_trades = get_scanner_today_trades(sc, today_str)
            result[sc] = {
                "status":        row["status"],
                "last_success":  row["last_success"],
                "today_alerts":  row["today_alerts"],
                "error":         row["error_msg"],
                "updated_at":    row["updated_at"],
                "today_trades":  [
                    {
                        "symbol":       t["symbol"],
                        "category":     t["category"] or "",
                        "signals":      t["signals"] or "",
                        "entry_price":  float(t["entry_price"]) if t["entry_price"] else None,
                        "entry_time":   t["alert_time"] or "",
                        "stop_loss":    float(t["stop_loss"]) if t["stop_loss"] else None,
                        "target_price": float(t["target_price"]) if t["target_price"] else None,
                        "exit_price":   float(t["exit_price"]) if t["exit_price"] else None,
                        "closed_at":    t["closed_at"],
                        "pnl_pct":      float(t["pnl_pct"]) if t["pnl_pct"] is not None else None,
                        "status":       t["status"] or "OPEN",
                        "score":        t["score"],
                    }
                    for t in today_trades
                ],
            }
        return jsonify(result)
    except Exception as exc:
        logger.exception("❌ /api/scanner_status failed")
        return jsonify({"_error": str(exc)}), 500


# ── Scanner DOWN helpers — write to Postgres, not just memory ─────────────────────────

def notify_scanner_down(scanner_name: str, error: str) -> None:
    """Mark a scanner as DOWN in the DB. Called from watchdog on crash."""
    logger.warning(f"🔴 Scanner DOWN: {scanner_name} | {error}")
    try:
        from database import upsert_scanner_health
        upsert_scanner_health(scanner_name, status="DOWN", error_msg=error[:500])
    except Exception:
        logger.exception(f"❌ Could not persist DOWN status for {scanner_name}")


def clear_scanner_down(scanner_name: str) -> None:
    """Clear DOWN flag in DB when a scanner recovers / restarts."""
    logger.info(f"🟢 Scanner recovering: {scanner_name}")
    try:
        from database import upsert_scanner_health
        upsert_scanner_health(scanner_name, status="OK", error_msg=None)
    except Exception:
        logger.exception(f"❌ Could not clear DOWN status for {scanner_name}")


def start_dashboard_server():
    """Called from main.py in a daemon thread."""
    # Railway injects PORT automatically — never hardcode it.
    # If PORT is missing the default 8080 is used, but Railway will always set it.
    port = int(os.getenv("PORT", 8080))
    logger.info(f"🌐 Dashboard server starting on port {port}")
    logger.info(f"🌐 Serving dashboard HTML from: {DASHBOARD_HTML_PATH or 'NOT FOUND'}")
    logger.info(f"🌐 Performance JSON path: {PERF_JSON_PATH}")
    # use_reloader=False is critical — Flask reloader forks the process and
    # breaks Railway's single-process model and our threading setup.
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)



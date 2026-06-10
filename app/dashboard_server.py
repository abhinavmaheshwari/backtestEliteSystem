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
    """Serve the latest performance JSON for the dashboard to fetch."""
    if os.path.exists(PERF_JSON_PATH):
        return send_file(PERF_JSON_PATH, mimetype="application/json")
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
    perf_exists = os.path.exists(PERF_JSON_PATH)
    perf_age    = None
    if perf_exists:
        mtime    = os.path.getmtime(PERF_JSON_PATH)
        perf_age = round((datetime.now().timestamp() - mtime) / 3600, 1)
    return jsonify({
        "status":            "ok",
        "time_ist":          datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "performance_ready": perf_exists,
        "performance_age_h": perf_age,
    })


@app.route("/api/summary")
def api_summary():
    """Quick JSON summary — useful for curl checks."""
    if not os.path.exists(PERF_JSON_PATH):
        return jsonify({"error": "No data yet"}), 404
    with open(PERF_JSON_PATH) as f:
        data = json.load(f)
    return jsonify(data.get("summary", {}))


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

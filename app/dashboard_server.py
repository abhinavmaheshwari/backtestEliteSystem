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
import time
import threading
from flask import Flask, jsonify, send_file, Response, request
import yfinance as yf

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
def get_html_path(filename):
    candidates = [
        os.path.join(APP_DIR, filename),
        os.path.join(BASE_DIR, filename),
    ]
    return next((p for p in candidates if os.path.exists(p)), None)

USER_DASHBOARD_PATH = get_html_path("user_dashboard.html")
ADMIN_DASHBOARD_PATH = get_html_path("admin_dashboard.html")

app = Flask(__name__)

# ── Disable Flask startup banner in production ───────────────────────────────────────
import logging as _logging
_logging.getLogger("werkzeug").setLevel(_logging.WARNING)

_active_viewers = {}

@app.route("/api/viewers", methods=["POST", "GET"])
def api_viewers():
    """Tracks active viewers by IP and Name. Cleans up inactive ones (>120s)."""
    now = time.time()
    if request.method == "POST":
        data = request.json or {}
        name = data.get("name", "Unknown")
        # Get real IP behind proxy (e.g. Railway)
        ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
        _active_viewers[ip] = {"name": name, "last_seen": now}

    # Clean up stale viewers
    stale_ips = [ip for ip, info in _active_viewers.items() if now - info["last_seen"] > 120]
    for sip in stale_ips:
        del _active_viewers[sip]

    return jsonify({
        "active_count": len(_active_viewers),
        "viewers": [info["name"] for info in _active_viewers.values()]
    })

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
    """Serve the user dashboard HTML."""
    if USER_DASHBOARD_PATH and os.path.exists(USER_DASHBOARD_PATH):
        return send_file(USER_DASHBOARD_PATH)
    return Response(
        "<h2 style='font-family:monospace;color:#00e5a0;background:#0b0e14;margin:0;padding:40px'>"
        "⚠️ user_dashboard.html not found.</h2>",
        mimetype="text/html",
    )

@app.route("/admin")
def admin_index():
    """Serve the admin dashboard HTML."""
    if ADMIN_DASHBOARD_PATH and os.path.exists(ADMIN_DASHBOARD_PATH):
        return send_file(ADMIN_DASHBOARD_PATH)
    return Response(
        "<h2 style='font-family:monospace;color:#00e5a0;background:#0b0e14;margin:0;padding:40px'>"
        "⚠️ admin_dashboard.html not found.</h2>",
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

# ── Endpoints for Market Ticker & Catalyst News ────────────────────────────────────

_indices_cache = {"data": None, "timestamp": 0}
_indices_lock = threading.Lock()

@app.route("/api/indices")
def api_indices():
    """Fetch live NIFTY 50, BANKNIFTY, and SENSEX with 1-min caching."""
    with _indices_lock:
        if _indices_cache["data"] and (time.time() - _indices_cache["timestamp"] < 60):
            return jsonify(_indices_cache["data"])
        
    try:
        symbols = {"NIFTY 50": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN"}
        data = {}
        for name, sym in symbols.items():
            ticker = yf.Ticker(sym)
            info = ticker.info
            price = info.get("regularMarketPrice") or info.get("previousClose")
            prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
            pct_change = 0.0
            if price and prev_close:
                pct_change = round(((price - prev_close) / prev_close) * 100, 2)
            data[name] = {"price": price, "pct_change": pct_change}
            
        with _indices_lock:
            _indices_cache["data"] = data
            _indices_cache["timestamp"] = time.time()
        return jsonify(data)
    except Exception as e:
        logger.error(f"Failed to fetch indices: {e}")
        return jsonify(_indices_cache["data"] or {})

_news_cache = {}
_news_lock = threading.Lock()

@app.route("/api/news/<symbol>")
def api_news(symbol):
    """Fetch recent 3 news headlines for a symbol with 15-min caching."""
    # Append .NS for Yahoo Finance compatibility if not present and if it doesn't have an extension
    yf_symbol = symbol if "." in symbol else f"{symbol}.NS"
    
    with _news_lock:
        cached = _news_cache.get(yf_symbol)
        if cached and (time.time() - cached["timestamp"] < 900): # 15 min cache
            return jsonify(cached["data"])
            
    try:
        ticker = yf.Ticker(yf_symbol)
        raw_news = ticker.news[:3]
        news = []
        for item in raw_news:
            n = item.get("content", item)
            news.append({
                "title": n.get("title", ""),
                "summary": n.get("summary", ""),
                "link": n.get("link") or n.get("clickThroughUrl", {}).get("url", "") or n.get("canonicalUrl", {}).get("url", ""),
                "provider": n.get("provider", {}).get("displayName", ""),
                "date": n.get("pubDate", "") or n.get("providerPublishTime", "")
            })
            
        with _news_lock:
            _news_cache[yf_symbol] = {"data": news, "timestamp": time.time()}
        return jsonify(news)
    except Exception as e:
        logger.error(f"Failed to fetch news for {yf_symbol}: {e}")
        return jsonify([])

import subprocess
import json

@app.route("/api/notices/<symbol>")
def api_notices(symbol):
    """Fetch recent corporate announcements from NSE via requests.Session to bypass WAF."""
    yf_symbol = symbol.replace('.NS', '')
    url = f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={yf_symbol}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*'
    }
    
    try:
        import requests
        s = requests.Session()
        # Ping homepage to establish cookies
        s.get('https://www.nseindia.com', headers=headers, timeout=5)
        # Fetch the actual data
        r = s.get(url, headers=headers, timeout=5)
        
        if r.status_code != 200:
            logger.error(f"NSE API returned {r.status_code} for {symbol}")
            return jsonify([])
            
        data = r.json()
        notices = []
        for n in data[:4]:
            desc = str(n.get("desc", ""))
            # Truncate overly long descriptions
            if len(desc) > 40:
                desc = desc[:37] + "..."
                
            notices.append({
                "date": n.get("an_dt", "").split(" ")[0],
                "desc": desc,
                "link": n.get("attchmntFile", "")
            })
        return jsonify(notices)
    except Exception as e:
        logger.error(f"Failed to fetch notices for {symbol}: {e}")
        return jsonify([])

@app.route('/api/all_tickers', methods=['GET'])
def api_all_tickers():
    """Returns a list of all active NSE symbols for frontend autocomplete."""
    try:
        from app.watchlist_cache import get_watchlist
        df = get_watchlist()
        if 'SYMBOL' in df.columns:
            return jsonify(df['SYMBOL'].dropna().unique().tolist())
        return jsonify([])
    except Exception as e:
        logger.error(f"Failed to fetch tickers: {e}")
        return jsonify([])

@app.route('/api/concall_ai/<symbol>', methods=['GET'])
def api_concall_ai(symbol):
    """Fetches the latest Concall transcript from NSE, parses the PDF, and uses AI to summarize it."""
    yf_symbol = symbol.replace('.NS', '')
    url = f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={yf_symbol}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*'
    }
    
    try:
        import requests
        s = requests.Session()
        s.get('https://www.nseindia.com', headers=headers, timeout=5)
        r = s.get(url, headers=headers, timeout=5)
        
        if r.status_code != 200:
            return jsonify({"error": "Failed to fetch NSE announcements."}), 500
            
        data = r.json()
        target_pdf = None
        
        # Priority 1: Transcripts
        for n in data:
            desc = str(n.get("desc", "")).lower()
            if "transcript" in desc:
                target_pdf = n.get("attchmntFile")
                break
                
        # Priority 2: Earnings / Investor Presentations
        if not target_pdf:
            for n in data:
                desc = str(n.get("desc", "")).lower()
                if "presentation" in desc or "earnings" in desc:
                    target_pdf = n.get("attchmntFile")
                    break
                    
        # Priority 3: General Concall Updates (Might just be a schedule)
        if not target_pdf:
            for n in data:
                desc = str(n.get("desc", "")).lower()
                if "con. call" in desc or "investor meet" in desc:
                    target_pdf = n.get("attchmntFile")
                    break
                
        if not target_pdf:
            return jsonify({"error": "No recent concall transcripts or investor presentations found on NSE."}), 404
            
        # Check Cache
        try:
            from app.database import get_cached_concall_analysis, save_concall_analysis
        except ImportError:
            from database import get_cached_concall_analysis, save_concall_analysis
            
        cached_data = get_cached_concall_analysis(symbol, target_pdf)
        if cached_data:
            logger.info(f"Returning CACHED AI analysis for {symbol}")
            return jsonify(cached_data)
            
        # Parse the PDF
        import sys
        if os.path.dirname(__file__) not in sys.path:
            sys.path.insert(0, os.path.dirname(__file__))
            
        try:
            from app.pdf_parser import extract_text_from_nse_pdf
        except ImportError:
            from pdf_parser import extract_text_from_nse_pdf
            
        text = extract_text_from_nse_pdf(target_pdf)
        
        if not text:
            return jsonify({"error": "Could not extract text from the PDF document."}), 500
            
        # Analyze with AI
        try:
            from app.ai_analyzer import analyze_concall_text
        except ImportError:
            from ai_analyzer import analyze_concall_text
            
        ai_data = analyze_concall_text(text)
        
        if "error" in ai_data:
            return jsonify(ai_data), 500
            
        # Save to Cache
        save_concall_analysis(symbol, target_pdf, ai_data)
        
        return jsonify(ai_data)
        
    except Exception as e:
        logger.error(f"AI Concall failed for {symbol}: {e}")
        return jsonify({"error": str(e)}), 500

# ── Scanner DOWN helpers

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
    logger.info(f"🌐 Serving User HTML from: {USER_DASHBOARD_PATH or 'NOT FOUND'}")
    logger.info(f"🌐 Serving Admin HTML from: {ADMIN_DASHBOARD_PATH or 'NOT FOUND'}")
    logger.info(f"🌐 Performance JSON path: {PERF_JSON_PATH}")
    # use_reloader=False is critical — Flask reloader forks the process and
    # breaks Railway's single-process model and our threading setup.
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)



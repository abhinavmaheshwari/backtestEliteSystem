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
from flask import Flask, jsonify, send_file, Response, request, make_response
# Ensure tzcache writable location before importing yfinance (robust import to support different cwd)
try:
    import app.yf_bootstrap
except Exception:
    try:
        import yf_bootstrap
    except Exception:
        pass
import yfinance as yf
from data_fetch_status import mark_success, mark_failure

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

@app.route('/favicon.ico')
def favicon():
    # Return a transparent 1x1 GIF to perfectly satisfy all browsers and CDNs
    from flask import send_file
    import io
    gif_data = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x01D\x00;'
    return send_file(io.BytesIO(gif_data), mimetype='image/gif')


# ── Disable Flask startup banner in production ───────────────────────────────────────
import logging as _logging
_logging.getLogger("werkzeug").setLevel(_logging.WARNING)

from database import (
    upsert_user, ping_user_session, cleanup_stale_sessions, get_online_users_and_history,
    send_user_message, get_user_messages, mark_user_messages_read, get_unread_message_counts
)

@app.route("/api/viewers", methods=["POST", "GET"])
def api_viewers():
    """Tracks active viewers by IP and Name using DB. Cleans up inactive ones (>120s)."""
    # 1. First, mark any inactive sessions as offline
    cleanup_stale_sessions()

    # 2. If it's a heartbeat/ping, update or start their session
    if request.method == "POST":
        data = request.json or {}
        name = data.get("name", "Unknown").strip()
        
        if name:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
            # Ensure user exists in users table
            user_id = upsert_user(name)
            if user_id:
                # Ping their session table
                ping_user_session(user_id, ip)

    # 3. Always return current state (online + history)
    stats = get_online_users_and_history()
    unread = get_unread_message_counts()
    
    return jsonify({
        "active_count": len(stats["online"]),
        "viewers": [u["name"] for u in stats["online"]],
        "history": stats["history"],
        "detailed_online": stats["online"],
        "unread_messages": unread
    })

@app.route("/api/messages", methods=["GET", "POST"])
def api_messages():
    """Get or send messages for a specific user."""
    if request.method == "GET":
        user_name = request.args.get("user")
        if not user_name:
            return jsonify({"error": "Missing user parameter"}), 400
        
        user_id = upsert_user(user_name)
        if not user_id:
            return jsonify({"error": "User not found"}), 404
            
        messages = get_user_messages(user_id)
        return jsonify(messages)
        
    elif request.method == "POST":
        data = request.json or {}
        user_name = data.get("user")
        message = data.get("message")
        is_from_admin = data.get("is_from_admin", False)
        
        if not user_name or not message:
            return jsonify({"error": "Missing user or message"}), 400
            
        user_id = upsert_user(user_name)
        if not user_id:
            return jsonify({"error": "User not found"}), 404
            
        success = send_user_message(user_id, message, is_from_admin)
        if success:
            return jsonify({"status": "success"})
        else:
            return jsonify({"error": "Failed to send message"}), 500

@app.route("/api/messages/read", methods=["POST"])
def api_messages_read():
    """Mark messages as read for a specific user."""
    data = request.json or {}
    user_name = data.get("user")
    as_admin = data.get("as_admin", False)
    
    if not user_name:
        return jsonify({"error": "Missing user"}), 400
        
    user_id = upsert_user(user_name)
    if not user_id:
        return jsonify({"error": "User not found"}), 404
        
    success = mark_user_messages_read(user_id, as_admin)
    return jsonify({"status": "success" if success else "error"})

# ── CORS + cache headers on every response ──────────────────────────────────────────
@app.after_request
def add_headers(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Cache-Control"]                = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"]                       = "no-cache"
    return response


@app.route("/")
def index():
    """Serve the user dashboard HTML."""
    if USER_DASHBOARD_PATH and os.path.exists(USER_DASHBOARD_PATH):
        r = make_response(send_file(USER_DASHBOARD_PATH))
        r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        r.headers['Pragma'] = 'no-cache'
        r.headers['Expires'] = '0'
        return r
    return Response(
        "<h2 style='font-family:monospace;color:#00e5a0;background:#0b0e14;margin:0;padding:40px'>"
        "⚠️ user_dashboard.html not found.</h2>",
        mimetype="text/html",
    )

@app.route("/admin")
def admin_index():
    """Serve the admin dashboard HTML."""
    if ADMIN_DASHBOARD_PATH and os.path.exists(ADMIN_DASHBOARD_PATH):
        r = make_response(send_file(ADMIN_DASHBOARD_PATH))
        r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        r.headers['Pragma'] = 'no-cache'
        r.headers['Expires'] = '0'
        return r
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

@app.route("/admin/export/<table>")
def export_csv_data(table):
    """Exports the requested database table as a CSV file."""
    # Prevent SQL injection by strictly whitelisting allowed tables
    valid_tables = ["alerts", "scanner_health", "system_state", "ai_concall_cache_v3"]
    if table not in valid_tables:
        return jsonify({"error": "Invalid table requested."}), 400
        
    try:
        from database import get_connection
        import io
        import csv
        from flask import Response
        
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table}")
                rows = cur.fetchall()
                col_names = [desc[0] for desc in cur.description]
                
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(col_names)
                for row in rows:
                    writer.writerow(row)
                    
                csv_data = output.getvalue()
                
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename={table}_export.csv"}
        )
    except Exception as e:
        logger.error(f"Error exporting CSV for table {table}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/admin/export/watchlist/<list_type>")
def export_watchlist(list_type):
    """Exports the daily generated watchlist CSVs."""
    from config import DATA_DIR
    import os
    from flask import send_file
    
    if list_type == "fundamental":
        file_path = os.path.join(DATA_DIR, "elite_fundamental_watchlist.csv")
        filename = "elite_fundamental_watchlist.csv"
    elif list_type == "excluded":
        file_path = os.path.join(DATA_DIR, "elite_fundamental_watchlist_excluded.csv")
        filename = "elite_fundamental_watchlist_excluded.csv"
    else:
        return jsonify({"error": "Invalid list type requested."}), 400
        
    if not os.path.exists(file_path):
        return jsonify({"error": "Watchlist file not found. Ensure daily builder has run."}), 404
        
    return send_file(file_path, as_attachment=True, download_name=filename)


@app.route("/api/summary")
def api_summary():
    """Quick JSON summary — useful for curl checks, loaded from DB."""
    try:
        from database import get_system_state
        val = get_system_state("performance_data")
        if val:
            data = json.loads(val)
            summary = data.get("summary", {})
            from database import get_ai_cache_count
            summary["ai_cache_count"] = get_ai_cache_count()
            return jsonify(summary)
    except Exception:
        logger.exception("❌ /api/summary failed")
    return jsonify({"error": "No data yet"}), 404


@app.route("/api/shortlist")
def api_shortlist():
    """Returns the elite fundamental watchlist data as JSON."""
    from config import WATCHLIST_PATH
    import pandas as pd
    try:
        if not os.path.exists(WATCHLIST_PATH):
            return jsonify([])
        df = pd.read_parquet(WATCHLIST_PATH)
        import json
        records = json.loads(df.to_json(orient="records"))
        return jsonify(records)
    except Exception as e:
        logger.error(f"Failed to load shortlist JSON: {e}")
        return jsonify([])

@app.route("/api/wealth")
def api_wealth():
    """Returns the elite wealth system data as JSON."""
    from config import DATA_DIR
    import pandas as pd
    try:
        WEALTH_PATH = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
        if not os.path.exists(WEALTH_PATH):
            return jsonify([])
        df = pd.read_parquet(WEALTH_PATH)
        import json
        records = json.loads(df.to_json(orient="records"))
        return jsonify(records)
    except Exception as e:
        logger.error(f"Failed to load wealth JSON: {e}")
        return jsonify([])

@app.route("/api/macro_state")
def api_macro_state():
    """Returns the current Macro Regime state (Nifty correction)."""
    try:
        from wealth_engine import fetch_nifty_macro_state
        ret_6m, dist_52w = fetch_nifty_macro_state()
        r_6m = round(float(ret_6m), 2) if ret_6m is not None else None
        d_52w = round(float(dist_52w), 2) if dist_52w is not None else None
        return jsonify({
            "nifty_6m_return": r_6m,
            "nifty_dist_52w": d_52w,
            "bear_market_gate": bool(d_52w > 15.0) if d_52w is not None else False
        })
    except Exception as e:
        logger.error(f"Failed to fetch macro state: {e}")
        return jsonify({"nifty_6m_return": 0, "nifty_dist_52w": 0, "bear_market_gate": False})


# ── Fetch errors API (admin) ─────────────────────────────────────────────────────
@app.route("/api/fetch_errors")
def api_fetch_errors():
    """Return recent aggregated fetch errors for admin triage."""
    try:
        from database import get_all_fetch_errors
        rows = get_all_fetch_errors(200)
        return jsonify(rows)
    except Exception:
        logger.exception("❌ /api/fetch_errors failed")
        return jsonify([]), 200


@app.route("/api/fetch_errors/by_scanner", methods=["GET"])
def api_fetch_errors_by_scanner():
    """Return unacknowledged fetch_errors for a specific scanner."""
    try:
        from database import get_fetch_errors_for_scanner
        scanner_name = request.args.get('name')
        if not scanner_name:
            return jsonify({"error": "Missing 'name' parameter"}), 400
        rows = get_fetch_errors_for_scanner(scanner_name)
        return jsonify(rows)
    except Exception:
        logger.exception("❌ /api/fetch_errors/by_scanner failed")
        return jsonify([]), 200


@app.route("/api/fetch_errors/ack/<int:error_id>", methods=["POST"])
def api_ack_fetch_error(error_id):
    """Acknowledge a specific fetch error so it stops alerting in UI."""
    try:
        from database import acknowledge_fetch_error
        ok = acknowledge_fetch_error(error_id)
        return jsonify({"ok": ok})
    except Exception:
        logger.exception("❌ /api/fetch_errors/ack failed")
        return jsonify({"ok": False}), 500


# ── MANUAL PORTFOLIO TRACKER ──────────────────────────────────────────────────
@app.route("/api/portfolio", methods=["GET"])
def api_get_portfolio():
    """Returns manual portfolio with live recommendations based on Wealth Engine data."""
    try:
        from database import get_manual_portfolio
        from config import DATA_DIR
        import pandas as pd
        import os
        
        portfolio = get_manual_portfolio()
        if not portfolio:
            return jsonify([])

        # Load live wealth data to enrich the portfolio
        wealth_data = {}
        WEALTH_PATH = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
        if os.path.exists(WEALTH_PATH):
            df = pd.read_parquet(WEALTH_PATH)
            # Create a lookup dictionary by Stock symbol
            for _, row in df.iterrows():
                wealth_data[row["Stock"]] = row.to_dict()

        def safe_num(v):
            if v is None: return 0.0
            try:
                f = float(v)
                import math
                return 0.0 if math.isnan(f) else f
            except (ValueError, TypeError):
                return 0.0

        enriched = []
        for p in portfolio:
            sym = p["symbol"]
            entry_price = safe_num(p["entry_price"])
            
            # Defaults
            live_data = wealth_data.get(sym, {})
            cmp = safe_num(live_data.get("cmp"))
            fm_score = safe_num(live_data.get("FM_Score"))
            signal = live_data.get("Signal") or ""
            ai_conf = safe_num(live_data.get("AI_Confidence"))
            category = live_data.get("Category") or ""

            pnl_pct = 0.0
            if cmp > 0 and entry_price > 0:
                pnl_pct = ((cmp - entry_price) / entry_price) * 100

            # Recommendation Engine Logic
            rec = "HOLD"
            if cmp == 0:
                rec = "NO DATA"
            elif fm_score > 0 and fm_score < 65:
                rec = "EXIT"
            elif signal and "SELL" in str(signal).upper():
                rec = "EXIT"
            elif fm_score >= 80 and cmp > 0 and pnl_pct <= -8:
                rec = "AVERAGE"
            
            p.update({
                "cmp": cmp,
                "pnl_pct": pnl_pct,
                "FM_Score": fm_score,
                "Signal": signal,
                "AI_Confidence": ai_conf,
                "Category": category,
                "Recommendation": rec,
                "Bucket": live_data.get("Portfolio_Bucket", "")
            })
            enriched.append(p)
            
        return jsonify(enriched)
    except Exception as e:
        logger.error(f"Failed to get manual portfolio: {e}")
        return jsonify([])

@app.route("/api/portfolio/add", methods=["POST"])
def api_add_portfolio():
    try:
        data = request.json
        symbol = data.get("symbol")
        entry_date = data.get("entry_date")
        entry_price = float(data.get("entry_price"))
        quantity = int(data.get("quantity"))
        
        if not symbol or not entry_date or not entry_price:
            return jsonify({"error": "Missing required fields"}), 400
            
        from database import add_portfolio_entry
        add_portfolio_entry(symbol, entry_date, entry_price, quantity)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Failed to add portfolio entry: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/portfolio/remove", methods=["POST"])
def api_remove_portfolio():
    try:
        data = request.json
        entry_id = int(data.get("id"))
        from database import remove_portfolio_entry
        remove_portfolio_entry(entry_id)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Failed to remove portfolio entry: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/data_fetch_health")
def api_data_fetch_health():
    """Return the health status of external data providers (cache/fetch failures)."""
    try:
        from database import get_all_data_fetch_health
        rows = get_all_data_fetch_health()
        return jsonify(rows)
    except Exception:
        logger.exception("❌ /api/data_fetch_health failed")
        return jsonify([]), 500


@app.route('/api/todays_alerts')
def api_todays_alerts():
    """Return alerts fired today (includes seen flags)."""
    try:
        from database import get_todays_alerts
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d')
        rows = get_todays_alerts(today)
        return jsonify(rows)
    except Exception:
        logger.exception('❌ /api/todays_alerts failed')
        return jsonify([]), 200


@app.route('/api/alert/mark_seen', methods=['POST'])
def api_mark_alert_seen():
    """Mark an alert as seen by user/admin via POST {id: int, role: 'user'|'admin'}."""
    try:
        data = request.json or {}
        alert_id = int(data.get('id'))
        role = data.get('role', 'user')
        from database import mark_alert_seen
        ok = mark_alert_seen(alert_id, role)
        return jsonify({'success': bool(ok)})
    except Exception as e:
        logger.exception('❌ /api/alert/mark_seen failed')
        return jsonify({'error': str(e)}), 500

@app.route("/api/data_fetch_health/acknowledge/<source_name>", methods=["POST"])
def api_acknowledge_health(source_name):
    """Admin endpoint to dismiss persistent API warnings."""
    try:
        from database import acknowledge_data_fetch_health
        acknowledge_data_fetch_health(source_name)
        return jsonify({"status": "success", "source": source_name})
    except Exception as e:
        logger.exception(f"❌ /api/data_fetch_health/acknowledge failed for {source_name}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/scanner_health/acknowledge/<scanner_name>", methods=["POST"])
def api_acknowledge_scanner_health(scanner_name):
    """Admin endpoint to dismiss persistent scanner warnings."""
    try:
        from database import acknowledge_scanner_health
        acknowledge_scanner_health(scanner_name)
        return jsonify({"status": "success", "scanner": scanner_name})
    except Exception as e:
        logger.exception(f"❌ /api/scanner_health/acknowledge failed for {scanner_name}")
        return jsonify({"error": str(e)}), 500

@app.route("/wealth")
def route_wealth():
    from config import BASE_DIR
    return send_file(os.path.join(BASE_DIR, "app", "wealth_dashboard.html"))

@app.route("/api/download_shortlist")
def api_download_shortlist():
    """Serves the elite fundamental watchlist as a CSV file."""
    from config import WATCHLIST_PATH
    import pandas as pd
    try:
        if not os.path.exists(WATCHLIST_PATH):
            return "No watchlist generated yet", 404
            
        csv_path = WATCHLIST_PATH.replace(".parquet", ".csv")
        df = pd.read_parquet(WATCHLIST_PATH)
        df.to_csv(csv_path, index=False)
        
        return send_file(
            csv_path,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"Elite_Watchlist_{datetime.now().strftime('%Y%m%d')}.csv"
        )
    except Exception as e:
        logger.error(f"Failed to generate shortlist CSV: {e}")
        return "Server Error", 500

@app.route("/api/scanner_status")
def api_scanner_status():
    """
    Return per-scanner health stats and today's trades — all sourced from Postgres.
    scanner_health table holds status/last_success/error.
    alerts table is queried live for today's trades per scanner.
    """
    try:
        import os
        from database import get_all_scanner_health, get_scanner_today_trades
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today_str = datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d')

        health_rows = get_all_scanner_health()
        result = {}
        for row in health_rows:
            sc = row["scanner_name"]
            today_trades = get_scanner_today_trades(sc, today_str)
            
            # Special case for Wealth Engine: It doesn't write to the alerts table.
            # We must parse its parquet file to get today's trades for the tooltip to work!
            if sc == "Wealth Engine":
                try:
                    import os, pandas as pd
                    from config import DATA_DIR
                    wealth_path = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
                    if os.path.exists(wealth_path):
                        wdf = pd.read_parquet(wealth_path)
                        # Filter for BUY signals
                        buy_df = wdf[wdf["Signal"].str.contains("BUY", na=False)]
                        today_trades = []
                        for _, wrow in buy_df.iterrows():
                            today_trades.append({
                                "symbol": wrow.get("Stock", ""),
                                "category": wrow.get("Portfolio_Bucket", ""),
                                "signals": wrow.get("Signal", ""),
                                "entry_price": wrow.get("cmp", 0),
                                "alert_time": today_str,
                                "stop_loss": None,
                                "target_price": None,
                                "exit_price": None,
                                "closed_at": None,
                                "pnl_pct": None,
                                "status": "OPEN",
                                "score": wrow.get("FM_Score", 0)
                            })
                except Exception as e:
                    pass

            # Enrich AI/Pledge workers with progress metrics
            # Enrich AI/Pledge workers with progress metrics
            extra = {}
            try:
                if sc in ("AI Worker", "Pledge Worker"):
                    # Compute total watchlist size (included + excluded)
                    import pandas as pd
                    total_needed = 0
                    from config import DATA_DIR
                    for f in [
                        os.path.join(DATA_DIR, 'elite_fundamental_watchlist.csv'),
                        os.path.join(DATA_DIR, 'elite_fundamental_watchlist_excluded.csv'),
                    ]:
                        try:
                            if os.path.exists(f):
                                dfw = pd.read_csv(f)
                                if 'Stock' in dfw.columns:
                                    total_needed += dfw['Stock'].dropna().shape[0]
                        except Exception:
                            pass
                    from database import get_ai_concall_stats, get_promoter_pledge_stats
                    if sc == 'AI Worker':
                        stats = get_ai_concall_stats()
                    else:
                        stats = get_promoter_pledge_stats()
                    extra = {
                        'progress': stats.get('total_cached', 0),
                        'total_needed': total_needed,
                        'last_processed_symbol': stats.get('last_symbol'),
                        'last_processed_at': stats.get('last_updated')
                    }
            except Exception:
                logger.exception('Failed to compute worker progress metrics')
    
            result[sc] = {
                    "status":        row["status"],
                    "last_success":  row["last_success"],
                    "today_alerts":  row["today_alerts"],
                    "error":         row["error_msg"],
                    "updated_at":    row["updated_at"],
                    "is_acknowledged": row["is_acknowledged"],
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
            # Merge extras if present
            if extra:
                result[sc].update(extra)
        return jsonify(result)
    except Exception as exc:
        logger.exception("❌ /api/scanner_status failed")
        return jsonify({}), 200

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
        try:
            mark_success('yfinance')
        except Exception:
            logger.exception('Failed to report yfinance success from dashboard indices')
        return jsonify(data)
    except Exception as e:
        logger.warning(f"Failed to fetch indices from yfinance: {e}")
        try:
            mark_failure('yfinance', f"{e} (Dashboard Indices)")
        except Exception:
            logger.exception('Failed to report yfinance failure from dashboard indices')
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
        try:
            mark_success('yfinance')
        except Exception:
            logger.exception('Failed to report yfinance success from dashboard news')
        return jsonify(news)
    except Exception as e:
        logger.error(f"Failed to fetch news for {yf_symbol}: {e}")
        try:
            mark_failure('yfinance', f"{e} (Dashboard News: {yf_symbol})")
        except Exception:
            logger.exception('Failed to report yfinance failure from dashboard news')
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
            try:
                mark_failure('nse_announcements', f'status_code={r.status_code}')
            except Exception:
                logger.exception('Failed to report nse_announcements failure')
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
        try:
            mark_success('nse_announcements')
        except Exception:
            logger.exception('Failed to report nse_announcements success')
        return jsonify(notices)
    except Exception as e:
        logger.error(f"Failed to fetch notices for {symbol}: {e}")
        try:
            mark_failure('nse_announcements', e)
        except Exception:
            logger.exception('Failed to report nse_announcements exception')
        return jsonify([])

@app.route('/api/all_tickers', methods=['GET'])
def api_all_tickers():
    """Returns a list of all active NSE symbols for frontend autocomplete."""
    try:
        import pandas as pd
        import os
        tickers = set()
        for f in ['data/elite_fundamental_watchlist.csv', 'data/elite_fundamental_watchlist_excluded.csv']:
            if os.path.exists(f):
                try:
                    df = pd.read_csv(f)
                    if 'Stock' in df.columns:
                        tickers.update(df['Stock'].dropna().unique().tolist())
                except Exception: pass
        if tickers:
            return jsonify(sorted(list(tickers)))
        return jsonify([])
    except Exception as e:
        logger.error(f"Failed to fetch tickers: {e}")
        return jsonify([])

def fetch_and_analyze_concall(symbol):
    """Internal function to fetch and analyze concall, returning a dict instead of a Response."""
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
            return {"error": "Failed to fetch NSE announcements."}
            
        data = r.json()
        target_pdfs = []
        
        # Priority 1: Transcripts
        for n in data:
            desc = str(n.get("desc", "")).lower()
            if "transcript" in desc:
                url = str(n.get("attchmntFile", ""))
                if url.lower().endswith(".pdf") and url not in target_pdfs:
                    target_pdfs.append(url)
            if len(target_pdfs) == 2: break
                
        # Priority 2: Earnings / Investor Presentations
        if not target_pdfs:
            for n in data:
                desc = str(n.get("desc", "")).lower()
                if "presentation" in desc or "earnings" in desc:
                    url = str(n.get("attchmntFile", ""))
                    if url.lower().endswith(".pdf") and url not in target_pdfs:
                        target_pdfs.append(url)
                if len(target_pdfs) == 2: break
                    
        # Priority 3: General Concall Updates (Might just be a schedule)
        if not target_pdfs:
            for n in data:
                desc = str(n.get("desc", "")).lower()
                if "con. call" in desc or "investor meet" in desc:
                    url = str(n.get("attchmntFile", ""))
                    if url.lower().endswith(".pdf") and url not in target_pdfs:
                        target_pdfs.append(url)
                if len(target_pdfs) == 2: break
                
        if not target_pdfs:
            return {"error": "No recent concall transcripts or investor presentations found on NSE."}
            
        target_pdf = target_pdfs[0]
        target_pdf_2 = target_pdfs[1] if len(target_pdfs) > 1 else None
            
        # Check Cache
        try:
            from database import get_cached_concall_analysis, save_concall_analysis
        except ImportError:
            from database import get_cached_concall_analysis, save_concall_analysis
            
        cached_data = get_cached_concall_analysis(symbol, target_pdf)
        if cached_data:
            logger.info(f"Returning CACHED AI analysis for {symbol}")
            return cached_data
            
        # Parse the PDF
        import sys
        if os.path.dirname(__file__) not in sys.path:
            sys.path.insert(0, os.path.dirname(__file__))
            
        try:
            from pdf_parser import extract_text_from_nse_pdf
        except ImportError:
            from pdf_parser import extract_text_from_nse_pdf
            
        text_1 = extract_text_from_nse_pdf(target_pdf)
        if not text_1:
            return {"error": "Could not extract text from the PDF document."}
            
        text = "--- LATEST QUARTER ---\n" + text_1
        
        if target_pdf_2:
            text_2 = extract_text_from_nse_pdf(target_pdf_2)
            if text_2:
                text += "\n\n--- PREVIOUS QUARTER ---\n" + text_2
            
        # Analyze with AI
        try:
            from ai_analyzer import analyze_concall_text
        except ImportError:
            from ai_analyzer import analyze_concall_text
            
        ai_data = analyze_concall_text(text)
        
        if "error" in ai_data:
            return ai_data
            
        # Save to Cache
        save_concall_analysis(symbol, target_pdf, ai_data)
        
        return ai_data
    except Exception as e:
        logger.error(f"Error in concall AI analysis for {symbol}: {e}")
        return {"error": str(e)}

@app.route("/api/concall_ai/<symbol>")
def api_concall_ai(symbol):
    from database import get_recent_concall_analysis
    cached = get_recent_concall_analysis(symbol, max_age_days=60)
    if cached:
        return jsonify(cached)
        
    res = fetch_and_analyze_concall(symbol)
    if "error" in res:
        return jsonify(res), 500 if "extract text" in res.get("error", "") else 404
    return jsonify(res)

# ── Wealth Buy Alerts API ──────────────────────────────────────────────────────────────

@app.route("/api/wealth/alerts", methods=["GET"])
def get_wealth_alerts():
    """Retrieve wealth buy alerts (all or filtered by symbol)."""
    from database import get_wealth_buy_alerts, get_today_wealth_alerts
    try:
        symbol = request.args.get("symbol")
        today_only = request.args.get("today", "").lower() == "true"
        
        if today_only:
            alerts = get_today_wealth_alerts()
        elif symbol:
            alerts = get_wealth_buy_alerts(symbol=symbol)
        else:
            alerts = get_wealth_buy_alerts()
        
        return jsonify(alerts)
    except Exception as e:
        logger.error(f"❌ Error fetching wealth alerts: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/wealth/save-alert", methods=["POST"])
def save_wealth_alert():
    """Save a new wealth buy alert."""
    from database import save_wealth_buy_alert
    try:
        data = request.get_json() or {}
        symbol = data.get("symbol", "").upper()
        alert_price = data.get("alert_price")
        breakout_type = data.get("breakout_type")
        fm_score = data.get("fm_score")
        notes = data.get("notes")
        
        if not symbol or alert_price is None:
            return jsonify({"error": "Symbol and alert_price are required"}), 400
        
        success = save_wealth_buy_alert(symbol, alert_price, breakout_type, fm_score, notes)
        if success:
            return jsonify({"success": True, "message": f"Alert saved for {symbol} @ ₹{alert_price}"})
        else:
            return jsonify({"error": "Failed to save alert"}), 500
    except Exception as e:
        logger.error(f"❌ Error saving wealth alert: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/wealth/update-alert/<int:alert_id>", methods=["POST"])
def update_wealth_alert(alert_id):
    """Update status of a wealth buy alert."""
    from database import update_wealth_alert_status
    try:
        data = request.get_json() or {}
        status = data.get("status", "").upper()
        current_price = data.get("current_price")
        
        if status not in ["ACTIVE", "BUY", "SELL", "HOLD", "CLOSED"]:
            return jsonify({"error": "Invalid status"}), 400
         
        success = update_wealth_alert_status(alert_id, status, current_price)
        if success:
            return jsonify({"success": True, "message": f"Alert {alert_id} updated to {status}"})
        else:
            return jsonify({"error": "Failed to update alert"}), 500
    except Exception as e:
        logger.error(f"❌ Error updating wealth alert: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/wealth/open-positions", methods=["GET"])
def get_open_positions_api():
    """Get all open positions."""
    from database import get_open_positions
    try:
        positions = get_open_positions()
        return jsonify(positions)
    except Exception as e:
        logger.error(f"❌ Error fetching open positions: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/wealth/closed-positions", methods=["GET"])
def get_closed_positions_api():
    """Get closed positions (filterable by days)."""
    from database import get_closed_positions
    try:
        days = request.args.get("days", "30")
        days = int(days) if days.isdigit() else 30
        positions = get_closed_positions(days_back=days)
        return jsonify(positions)
    except Exception as e:
        logger.error(f"❌ Error fetching closed positions: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/wealth/close-position", methods=["POST"])
def close_position_api():
    """Manually close a position (or auto-close on SELL signal)."""
    from database import close_position
    try:
        data = request.get_json() or {}
        symbol = data.get("symbol", "").upper()
        exit_price = data.get("exit_price")
        exit_signal = data.get("exit_signal")
        
        if not symbol or exit_price is None:
            return jsonify({"error": "Symbol and exit_price are required"}), 400
        
        success = close_position(symbol, exit_price, exit_signal)
        if success:
            return jsonify({"success": True, "message": f"Position closed for {symbol}"})
        else:
            return jsonify({"error": "No open position found"}), 404
    except Exception as e:
        logger.error(f"❌ Error closing position: {e}")
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



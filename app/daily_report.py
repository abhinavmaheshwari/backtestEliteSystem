# =====================================================================================
# app/daily_report.py
# CONSOLIDATED EOD REPORT WITH TELEGRAM FALLBACK
# =====================================================================================

from database import get_connection
import pandas as pd
from datetime import datetime
import logging
import requests
import os

from config import DB_PATH, WATCHLIST_PATH, DATA_DIR

logger = logging.getLogger(__name__)

def generate_and_send_daily_summary():
    today_str = datetime.now().strftime("%Y-%m-%d")
    csv_filename = os.path.join(DATA_DIR, f"Daily_Alerts_{today_str}.csv")
    
    # ── 1. FETCH TODAY'S ALERTS & SAVE TO CSV ────────────────────────────────────────
    try:
        with get_connection() as conn:
            query = f"SELECT symbol, breakout_type, alert_time FROM alerts WHERE alert_date = '{today_str}' ORDER BY alert_time DESC"
            alerts_df = pd.read_sql_query(query, conn)
        
        if not alerts_df.empty:
            alerts_df[['Category', 'Signals', 'Date', 'Scanner']] = alerts_df['breakout_type'].str.split('|', expand=True)
            alerts_df = alerts_df[['alert_time', 'Scanner', 'symbol', 'Category', 'Signals']]
            alerts_df.columns = ['Time (IST)', 'Scanner', 'Stock', 'Category', 'Breakout Signals']
            alerts_df.to_csv(csv_filename, index=False)
        else:
            # Create an empty placeholder CSV if no alerts fired
            pd.DataFrame(columns=['Time (IST)', 'Scanner', 'Stock', 'Category', 'Breakout Signals']).to_csv(csv_filename, index=False)
            
    except Exception as e:
        logger.error(f"Error fetching alerts: {e}")
        alerts_df = pd.DataFrame()

    # ── 2. FETCH CURRENT WATCHLIST ───────────────────────────────────────────────────
    try:
        watchlist_df = pd.read_parquet(WATCHLIST_PATH)
        watchlist_df = watchlist_df[['Stock', 'Category', 'Sector', 'CMP', 'Fundamental Score']]
    except Exception:
        watchlist_df = pd.DataFrame()

    # ── 3. BUILD HTML TEMPLATE ───────────────────────────────────────────────────────
    alerts_html = alerts_df.to_html(index=False, border=0, classes="styled-table", justify="left") if not alerts_df.empty else "<p>No alerts were generated today.</p>"
    watchlist_html = watchlist_df.to_html(index=False, border=0, classes="styled-table", justify="left") if not watchlist_df.empty else "<p>Watchlist unavailable.</p>"

    html_content = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f7f6; color: #333; padding: 20px; }}
            .container {{ max-width: 900px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; }}
            h1 {{ color: #2c3e50; text-align: center; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
            .styled-table {{ border-collapse: collapse; margin: 15px 0; font-size: 0.9em; width: 100%; }}
            .styled-table thead tr {{ background-color: #009879; color: #ffffff; text-align: left; }}
            .styled-table th, .styled-table td {{ padding: 12px 15px; border-bottom: 1px solid #dddddd; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>End of Day Market Summary</h1>
            <h2>🚨 Today's Momentum Alerts</h2>
            {alerts_html}
            <h2>📋 Active Elite Fundamental Universe</h2>
            {watchlist_html}
        </div>
    </body>
    </html>
    """

    # ── 4. EMAIL & TELEGRAM NOTIFICATIONS REMOVED ────────────────────────────────────
    # Email and Telegram notifications removed (2026-06-17)
    logger.info(f"📊 Daily Summary prepared | {today_str} | {len(alerts_df)} alert(s)")

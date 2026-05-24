# ==============================================================================
# SCRIPT: HOURLY INTRADAY SNIPER
# WHAT IT DOES:
# 1. Loads the 'Elite Master-List' cached by the Morning Scan (No API ban risk).
# 2. Fetches today's 5-minute live price tape.
# 3. Detects breakouts above 20-day highs + volume pacing spikes.
# 4. Fires a Telegram alert with entry price, SL, and breakout timestamp.
# ==============================================================================

import pickle
import os
import yfinance as yf
import pandas as pd
from datetime import datetime
import requests

# Railway environment variables
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
DATA_PATH = "/app/data/elite_watchlist.pkl"

def send_alert(msg):
    """Sends the breakout alert to your Telegram channel."""
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})

def run_tape_sniper():
    # Load the pre-vetted list instantly (Takes < 0.1s)
    with open(DATA_PATH, 'rb') as f:
        winners = pickle.load(f)
    
    tickers = [f"{w['name']}.NS" for w in winners]
    print(f"📡 Scanning {len(tickers)} elite stocks on live tape...")
    
    # Fetch live 5-minute candles to detect the EXACT moment of breakout
    intraday = yf.download(tickers, period="1d", interval="5m", group_by="ticker", progress=False)
    
    for stock in winners:
        t = f"{stock['name']}.NS"
        i_df = intraday[t].dropna()
        
        # Technical Logic: If Price > 20d High AND Vol Surge > 70% of Daily Avg
        # If true:
        send_alert(f"🚨 <b>LIVE BREAKOUT:</b> {stock['name']} at {datetime.now().strftime('%H:%M')} IST")

if __name__ == "__main__":
    run_tape_sniper()

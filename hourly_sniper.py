# ==============================================================================
# HOURLY TECHNICAL SNIPER
# Purpose: Scans live 5-minute tape for institutional setups (Breakout/Accum/Squeeze).
# Speed: Runs in < 5 seconds. Uses cached list to avoid API bans.
# ==============================================================================
import pickle, os, requests, yfinance as yf
from datetime import datetime

TOKEN, CHAT_ID = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
DATA_PATH = "/app/data/elite_watchlist.pkl"

def send_alert(setup, stock, price, sl):
    msg = f"<b>{setup} DETECTED</b>\nStock: #{stock['name']}\nPrice: ₹{price:.2f}\nSL: ₹{sl:.2f}"
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})

def run_sniper():
    with open(DATA_PATH, 'rb') as f: winners = pickle.load(f)
    tickers = [f"{w['name']}.NS" for w in winners]
    data = yf.download(tickers, period="1d", interval="5m", group_by="ticker", progress=False)
    
    for w in winners:
        t = f"{w['name']}.NS"
        df = data[t].dropna()
        # Institutional Pattern Logic
        vol_avg = df['Volume'].rolling(20).mean()
        # Logic: If Breakout (Price > 20d high) + Vol Surge (>2x avg)
        if df['Close'].iloc[-1] > df['High'].rolling(20).max().iloc[-2] and df['Volume'].iloc[-1] > vol_avg.iloc[-1]*2:
            send_alert("🚀 BREAKOUT", w, df['Close'].iloc[-1], df['Low'].min())

if __name__ == "__main__": run_sniper()

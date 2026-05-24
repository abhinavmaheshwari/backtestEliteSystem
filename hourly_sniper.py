import pickle, os, requests, yfinance as yf

DATA_PATH = "/app/data/elite_watchlist.pkl"
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def send_alert(msg):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})

def run_sniper():
    if not os.path.exists(DATA_PATH): return
    with open(DATA_PATH, 'rb') as f: winners = pickle.load(f)
    
    tickers = [f"{w['name']}.NS" for w in winners[:50]] # Limit to 50 for speed
    data = yf.download(tickers, period="1d", interval="5m", group_by="ticker", progress=False)
    
    for w in winners[:50]:
        t = f"{w['name']}.NS"
        df = data[t].dropna()
        if df['Close'].iloc[-1] > df['High'].rolling(20).max().iloc[-2] and df['Volume'].iloc[-1] > df['Volume'].rolling(20).mean().iloc[-1]*2:
            send_alert(f"🚀 <b>BREAKOUT:</b> {w['name']} @ {df['Close'].iloc[-1]:.2f}")

if __name__ == "__main__": run_sniper()

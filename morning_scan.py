import pickle, os
from tradingview_screener import Query
import yfinance as yf
from tqdm import tqdm
import concurrent.futures

DATA_PATH = "/app/data/elite_watchlist.pkl" 

def check_fundamentals(row):
    try:
        tk = yf.Ticker(f"{row['name']}.NS")
        q = tk.quarterly_financials
        if q is None or q.shape[1] < 5: return None
        # Logic: YoY Rev growth > 5%
        if (float(q.iloc[0, 0]) - float(q.iloc[0, 4])) / float(q.iloc[0, 4]) > 0.05:
            return row.to_dict()
    except: pass
    return None

def main():
    print("🚀 Morning Scan Started...")
    tv_df = Query().search_text("NSE").get_scanner_data()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        winners = [r for r in list(tqdm(executor.map(check_fundamentals, [row for _, row in tv_df.iterrows()]))) if r]
    
    os.makedirs("/app/data", exist_ok=True)
    with open(DATA_PATH, 'wb') as f: pickle.dump(winners, f)
    print(f"✅ Saved {len(winners)} stocks.")

if __name__ == "__main__": main()

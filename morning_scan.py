# ==============================================================================
# MORNING FUNDAMENTAL PRODUCER
# Purpose: Screens the NSE liquid universe once daily.
# Saves a persistent 'elite_watchlist.pkl' for the sniper to use all day.
# ==============================================================================
import pickle, yfinance as yf, pandas as pd
from tradingview_screener import Query, col
import concurrent.futures
from tqdm import tqdm

DATA_PATH = "/app/data/elite_watchlist.pkl" 

def check_fundamentals(row):
    try:
        ticker = f"{row['name']}.NS"
        tk = yf.Ticker(ticker)
        q_fin = tk.quarterly_financials
        if q_fin is None or q_fin.shape[1] < 5: return None
        # Logic: YoY Revenue/Profit Growth > 5%
        c_rev, ly_rev = float(q_fin.iloc[0, 0]), float(q_fin.iloc[0, 4])
        if (c_rev - ly_rev)/ly_rev > 0.05: return row.to_dict()
    except: pass
    return None

def main():
    # Fetch NSE liquid universe (add your specific screener filter here)
    tv_df = Query().search_text("NSE").get_scanner_data() 
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        winners = [r for r in list(tqdm(executor.map(check_fundamentals, [row for _, row in tv_df.iterrows()]))) if r]
    with open(DATA_PATH, 'wb') as f: pickle.dump(winners, f)
    print(f"✅ Cached {len(winners)} Elite Stocks.")

if __name__ == "__main__": main()

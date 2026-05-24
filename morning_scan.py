# ==============================================================================
# SCRIPT: MORNING FUNDAMENTAL SCANNER
# WHAT IT DOES: 
# 1. Scans the NSE liquid market (>500Cr Cap) for growth.
# 2. Applies strict YoY Sales/Profit growth filters & QoQ momentum checks.
# 3. Saves the 'Elite Master-List' to a persistent volume for the Hourly Sniper.
# ==============================================================================

import pickle
import os
from tradingview_screener import Query, col
import yfinance as yf
import pandas as pd
import concurrent.futures
from tqdm import tqdm

# Mount this path to a Railway Persistent Volume
DATA_PATH = "/app/data/elite_watchlist.pkl" 

def check_fundamentals(row):
    """
    Worker function: Fetches financials and applies strict growth gates.
    Only keeps companies that are currently accelerating.
    """
    ticker = f"{row['name']}.NS"
    tk = yf.Ticker(ticker)
    try:
        q_fin = tk.quarterly_financials
        if q_fin is None or q_fin.shape[1] < 5: return None
        
        # Pulling recent vs YoY data to filter for momentum
        c_rev, prev_q_rev, ly_rev = float(q_fin.iloc[0, 0]), float(q_fin.iloc[0, 1]), float(q_fin.iloc[0, 4])
        # [Add Profit Logic here]...
        
        # Growth Gate: Must be growing YoY and QoQ
        if ((c_rev - ly_rev)/ly_rev > 0.05) and (c_rev > prev_q_rev):
            return row.to_dict()
    except: pass
    return None

def main():
    print("🚀 Morning Scan: Starting Fundamental Screening...")
    # Add fetch_broad_liquid_market logic here
    
    # Multithreaded scanning to handle 500+ stocks efficiently
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(tqdm(executor.map(check_fundamentals, [row for _, row in tv_df.iterrows()])))
    
    winners = [r for r in results if r is not None]
    
    # Save the winners to disk so the hourly script can access them instantly
    with open(DATA_PATH, 'wb') as f:
        pickle.dump(winners, f)
    print(f"✅ Saved {len(winners)} elite stocks to {DATA_PATH}")

if __name__ == "__main__":
    main()

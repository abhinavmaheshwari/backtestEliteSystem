import os
import json
import logging
import pandas as pd
# Ensure tzcache writable location before importing yfinance (robust import to support different cwd)
try:
    import app.yf_bootstrap
except Exception:
    try:
        import yf_bootstrap
    except Exception:
        pass
import yfinance as yf
from datetime import datetime, date
import concurrent.futures

logger = logging.getLogger(__name__)

CACHE_FILE = "data/fundamentals_cache.json"

FUNDAMENTAL_REFRESH_SCHEDULE = {
    "NIFTY_500":     7,    # days
    "NIFTY_MIDCAP":  14,   # days
    "SMALLCAP_TAIL": 30,   # days
}

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cache(cache_data: dict):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache_data, f, indent=2)

def compute_piotroski(ticker_info: dict, financials: pd.DataFrame) -> int:
    try:
        score = 0
        if len(financials.columns) < 2:
            return -1 # Need at least 2 years

        # Profitability (4 pts)
        net_income = financials.loc["Net Income"] if "Net Income" in financials.index else pd.Series([0, 0])
        total_assets = financials.loc["Total Assets"] if "Total Assets" in financials.index else pd.Series([1, 1])
        
        score += 1 if net_income.iloc[0] > 0 else 0
        score += 1 if net_income.iloc[0] > net_income.iloc[1] else 0
        score += 1 if (net_income.iloc[0] / total_assets.iloc[0]) > 0 else 0
        score += 1 if ticker_info.get("operatingCashflow", 0) > 0 else 0
        
        # Leverage / Liquidity (3 pts)
        lt_debt = financials.loc["Long Term Debt"] if "Long Term Debt" in financials.index else pd.Series([0, 0])
        shares = financials.loc["Ordinary Shares Number"] if "Ordinary Shares Number" in financials.index else pd.Series([1, 1])
        
        score += 1 if lt_debt.iloc[0] < lt_debt.iloc[1] else 0
        score += 1 if ticker_info.get("currentRatio", 0) > ticker_info.get("previousCurrentRatio", 0) else 0
        score += 1 if shares.iloc[0] <= shares.iloc[1] else 0
        
        # Efficiency (2 pts)
        revenue = financials.loc["Total Revenue"] if "Total Revenue" in financials.index else pd.Series([0, 0])
        
        score += 1 if ticker_info.get("grossMargins", 0) > ticker_info.get("prevGrossMargins", 0) else 0
        score += 1 if (revenue.iloc[0] / total_assets.iloc[0]) > (revenue.iloc[1] / total_assets.iloc[1]) else 0
        
        return score
    except Exception as e:
        return -1


def fetch_single_piotroski(symbol: str) -> dict:
    try:
        t = yf.Ticker(f"{symbol.replace('_', '-')}.NS")
        info = t.info
        
        # Combine financials and balance sheet to have all required rows
        fin = t.financials
        bs = t.balance_sheet
        if fin.empty and bs.empty:
            return {"score": -1, "date": str(date.today())}
            
        combined = pd.concat([fin, bs])
        score = compute_piotroski(info, combined)
        return {"score": score, "date": str(date.today())}
    except Exception as e:
        return {"score": -1, "date": str(date.today())}


def get_tier(market_cap_cr: float) -> str:
    if market_cap_cr >= 20000:
        return "NIFTY_500"
    elif market_cap_cr >= 5000:
        return "NIFTY_MIDCAP"
    else:
        return "SMALLCAP_TAIL"

def is_stale(cache_entry: dict, tier: str) -> bool:
    if not cache_entry:
        return True
    try:
        entry_date = datetime.strptime(cache_entry["date"], "%Y-%m-%d").date()
        days_old = (date.today() - entry_date).days
        return days_old > FUNDAMENTAL_REFRESH_SCHEDULE.get(tier, 30)
    except Exception:
        return True

def refresh_fundamentals_tiered(universe_df: pd.DataFrame):
    logger.info("🔄 Refreshing Piotroski Fundamentals (Tiered)...")
    cache = load_cache()
    
    to_fetch = []
    for _, row in universe_df.iterrows():
        sym = row["name"]
        mc = row.get("market_cap_basic", 0) / 10000000
        tier = get_tier(mc)
        if is_stale(cache.get(sym), tier):
            to_fetch.append(sym)
            
    logger.info(f"📥 Need to fetch {len(to_fetch)} symbols out of {len(universe_df)} for Piotroski.")
    
    if not to_fetch:
        return
        
    def process(sym):
        return sym, fetch_single_piotroski(sym)
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process, sym) for sym in to_fetch]
        for idx, future in enumerate(concurrent.futures.as_completed(futures)):
            sym, result = future.result()
            cache[sym] = result
            if idx % 50 == 0:
                logger.info(f"   Fetched {idx}/{len(to_fetch)} fundamentals")
                save_cache(cache)
                
    save_cache(cache)
    logger.info("✅ Fundamental fetch complete.")

def get_piotroski_score(symbol: str) -> int:
    cache = load_cache()
    entry = cache.get(symbol, {})
    return entry.get("score", -1)

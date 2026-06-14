import requests
import json
import logging
from rapidfuzz import fuzz
from datetime import datetime
import pandas as pd

logger = logging.getLogger(__name__)

KNOWN_FII_PATTERNS = [
    "MORGAN STANLEY", "GOLDMAN SACHS", "NOMURA", "SOCIETE GENERALE", 
    "VANGUARD", "BLACKROCK", "GOVERNMENT PENSION FUND", "FIDELITY", "JP MORGAN",
    "COPTHALL MAURITIUS", "CITIGROUP", "MERRILL LYNCH", "BNP PARIBAS", "BOFA SECURITIES",
    "NORGES BANK", "ABU DHABI INVESTMENT AUTHORITY"
]

def get_nse_bulk_block_deals():
    """Fetches block/bulk deals from NSE"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/111.0',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    }
    
    session = requests.Session()
    session.headers.update(headers)
    
    # 1. Hit main page to get cookies
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except:
        pass
        
    urls = [
        "https://www.nseindia.com/api/historical/block-deals",
        "https://www.nseindia.com/api/snapshot-capital-market-sme-bulk-deals"
    ]
    
    all_deals = []
    
    for url in urls:
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data:
                    all_deals.extend(data["data"])
        except Exception as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            
    return all_deals

def detect_fii_deals() -> dict:
    """Returns a dict of symbol -> list of FII buyers"""
    deals = get_nse_bulk_block_deals()
    if not deals:
        return {}
        
    fii_stocks = {}
    
    for deal in deals:
        client = str(deal.get("clientName", "")).upper()
        symbol = str(deal.get("symbol", "")).upper()
        buy_sell = str(deal.get("buyOrSell", deal.get("remarks", ""))).upper()
        
        if "BUY" not in buy_sell:
            continue
            
        # Check against patterns using fuzzy match
        is_fii = False
        matched_fii = ""
        for pattern in KNOWN_FII_PATTERNS:
            # simple substring first
            if pattern in client:
                is_fii = True
                matched_fii = pattern
                break
            # fuzzy match if not substring
            score = fuzz.partial_ratio(pattern, client)
            if score >= 85:  # high confidence match
                is_fii = True
                matched_fii = pattern
                break
                
        if is_fii:
            if symbol not in fii_stocks:
                fii_stocks[symbol] = []
            fii_stocks[symbol].append(matched_fii)
            
    return fii_stocks

CACHE_FILE = "data/fii_block_deals.json"

def get_cached_fii_deals() -> dict:
    import os
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == str(datetime.now().date()):
                    return data.get("deals", {})
        except:
            pass
    return {}

def run_fii_detector() -> dict:
    logger.info("🔍 Running FII Block/Bulk Deal Detector...")
    
    # Check cache first to avoid hammering NSE
    cached = get_cached_fii_deals()
    if cached:
        logger.info(f"✅ Loaded {len(cached)} FII deals from cache.")
        return cached

    results = detect_fii_deals()
    
    # Save cache
    import os
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "date": str(datetime.now().date()),
            "deals": results
        }, f, indent=2)
        
    logger.info(f"✅ FII deals detected in {len(results)} stocks today and cached.")
    return results

def get_fii_buyers(symbol: str) -> list:
    deals = get_cached_fii_deals()
    if not deals:
        # Don't run the detector synchronously during scoring, just return empty
        return []
    return deals.get(symbol, [])

if __name__ == "__main__":
    print(run_fii_detector())

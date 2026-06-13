import logging
import requests
import pandas as pd
import io
import time
import re
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BULK_URL = "https://archives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://archives.nseindia.com/content/equities/block.csv"

# Keywords that indicate institutional/fund buying
INSTITUTIONAL_KEYWORDS = [
    "FUND", "CAPITAL", "MANAGEMENT", "ASSET", "INVESTMENT", "LLP", 
    "HOLDING", "TRUST", "VENTURES", "GLOBAL", "INDIA", "PARTNERS", 
    "EQUITY", "SECURITIES", "WEALTH", "ADVISORS", "LTD", "LIMITED"
]

# Keywords that usually indicate retail or individual names to exclude
RETAIL_KEYWORDS = ["HUF", "INDIVIDUAL"]

def _get_robust_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=5, read=5, connect=5, backoff_factor=1.5, status_forcelist=(500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
        "Referer": "https://www.nseindia.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    })
    return session

def _is_institutional(client_name: str) -> bool:
    if not isinstance(client_name, str):
        return False
    name = client_name.upper()
    for retail in RETAIL_KEYWORDS:
        if retail in name:
            return False
    
    # Needs to have at least one institutional keyword
    for inst in INSTITUTIONAL_KEYWORDS:
        if inst in name:
            return True
            
    # Or if the name is very long, it's often a company
    if len(name.split()) >= 3:
        return True
        
    return False

def get_institutional_buys() -> dict[str, list[str]]:
    """
    Fetches today's (or latest) bulk and block deals from NSE.
    Returns a dict mapping Symbol -> list of Institutional Buyer Names.
    """
    session = _get_robust_session()
    buys = {}
    
    def process_url(url, deal_type):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                text = r.text.strip()
                if not text or "NO RECORDS" in text or len(text.splitlines()) < 2:
                    return
                
                df = pd.read_csv(io.StringIO(text))
                df.columns = [c.strip().upper() for c in df.columns]
                
                if "SYMBOL" not in df.columns or "BUY/SELL" not in df.columns or "CLIENT NAME" not in df.columns:
                    return
                
                # Filter for buys
                df_buy = df[df["BUY/SELL"].astype(str).str.upper().isin(["BUY", "B"])]
                
                for _, row in df_buy.iterrows():
                    symbol = str(row["SYMBOL"]).strip()
                    client = str(row["CLIENT NAME"]).strip()
                    
                    if _is_institutional(client):
                        if symbol not in buys:
                            buys[symbol] = []
                        buys[symbol].append(f"[{deal_type}] {client}")
                        
        except Exception as e:
            logger.warning(f"Failed to fetch {deal_type} deals: {e}")

    process_url(BULK_URL, "BULK")
    process_url(BLOCK_URL, "BLOCK")
    
    logger.info(f"🏦 Found institutional buys in {len(buys)} stocks from bulk/block deals.")
    return buys

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    buys = get_institutional_buys()
    for sym, clients in list(buys.items())[:10]:
        print(f"{sym}: {clients}")

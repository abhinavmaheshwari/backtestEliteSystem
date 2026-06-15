import os
import requests
import logging
from bs4 import BeautifulSoup
import re
from functools import lru_cache
from database import get_connection, init_db
from data_fetch_status import mark_success, mark_failure

logger = logging.getLogger(__name__)

@lru_cache(maxsize=5000)
def fetch_promoter_pledge(symbol: str):
    """
    Fetches the promoter pledge percentage for a given NSE symbol.
    Primarily relies on the PostgreSQL cache populated by the pledge_worker.
    Makes ONE quick fallback attempt if cache is missing.
    """
    init_db()

    # 1. Check DB Cache
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT pledge_pct 
                    FROM promoter_pledge_cache 
                    WHERE symbol = %s 
                      AND updated_at >= NOW() - INTERVAL '30 days'
                """, (symbol,))
                row = cur.fetchone()
                if row:
                    return float(row[0])
    except Exception as e:
        logger.warning(f"Database error checking pledge cache for {symbol}: {e}")

    # 2. Fast Fallback Attempt (One-Time)
    # The pledge_worker will properly resolve broken URLs asynchronously.
    api_key = os.getenv("SCRAPERAPI_KEY")
    if not api_key:
        return 0.0

    fallback_urls = {
        'HINDCOPPER': 'https://trendlyne.com/equity/551/HINDCOPPER/hindustan-copper-ltd/'
    }
    
    target_url = fallback_urls.get(symbol, f"https://trendlyne.com/stock/{symbol}/")
    
    payload = {
        'api_key': api_key,
        'url': target_url,
        'render': 'false'
    }
    
    pledge_val = None
    try:
        # Extremely short timeout to prevent blocking wealth engine
        res = requests.get('https://api.scraperapi.com/', params=payload, timeout=10)
        if res.status_code == 200:
            match = re.search(r'pledge[^\d]{1,30}?(\d+\.?\d*)\s*%', res.text, re.IGNORECASE)
            if match:
                pledge_val = float(match.group(1))
            else:
                soup = BeautifulSoup(res.text, 'html.parser')
                for div in soup.find_all(['div', 'span', 'td']):
                    if 'pledge' in div.text.lower() and '%' in div.text:
                        m = re.search(r'(\d+\.?\d*)\s*%', div.text)
                        if m:
                            pledge_val = float(m.group(1))
                            break
            try:
                mark_success('scraperapi')
            except Exception:
                pass
        else:
            try:
                mark_failure('scraperapi', f'Fast fetch 404/Failed for {symbol} URL={target_url}')
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Fast pledge fetch failed for {symbol}: {e}")
        try:
            mark_failure('scraperapi', f"Fast fetch Exception: {e} URL={target_url}")
        except Exception:
            pass

    # We DO NOT save to the database here. 
    # That is the sole responsibility of pledge_worker.py to prevent race conditions.
    return pledge_val

import os
import requests
import logging
from bs4 import BeautifulSoup
import time
import random
import re
from functools import lru_cache
from database import get_connection, init_db
from data_fetch_status import mark_success, mark_failure

logger = logging.getLogger(__name__)

@lru_cache(maxsize=5000)
def fetch_promoter_pledge(symbol: str):
    """
    Fetches the promoter pledge percentage for a given NSE symbol.
    Uses ScraperAPI to bypass Cloudflare and scrapes Trendlyne.com.
    Results are cached in the database for 30 days.
    """
    api_key = os.getenv("SCRAPERAPI_KEY")
    if not api_key:
        return 0.0

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
                    logger.debug(f"📊 Cached Pledge for {symbol}: {row[0]}%")
                    return float(row[0])
    except Exception as e:
        logger.warning(f"Database error checking pledge cache for {symbol}: {e}")

    # 2. Not in cache (or expired), fetch via ScraperAPI
    target_url = f"https://trendlyne.com/stock/{symbol}/"
    payload = {
        'api_key': api_key,
        'url': target_url,
        'render': 'false'
    }
    
    # Rate limiting sleep
    time.sleep(random.uniform(1.5, 3.0))
    
    pledge_val = None
    try:
        res = requests.get('https://api.scraperapi.com/', params=payload, timeout=45)
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
                logger.exception('Failed to report success for scraperapi')
        else:
            try:
                mark_failure('scraperapi', f'status_code={res.status_code} URL={target_url}')
            except Exception:
                logger.exception('Failed to report failure for scraperapi (non-200)')
    except Exception as e:
        logger.warning(f"Failed to scrape pledge for {symbol}: {e}")
        try:
            mark_failure('scraperapi', f"Exception: {e} URL={target_url}")
        except Exception:
            logger.exception('Failed to report scraperapi exception')
        return None

    # 3. Save to Cache
    if pledge_val is not None:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO promoter_pledge_cache (symbol, pledge_pct, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (symbol) DO UPDATE 
                        SET pledge_pct = EXCLUDED.pledge_pct, updated_at = NOW()
                    """, (symbol, pledge_val))
                    conn.commit()
        except Exception as e:
            logger.warning(f"Failed to save pledge cache for {symbol}: {e}")

    return pledge_val

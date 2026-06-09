# =====================================================================================
# app/delivery_data.py (ANTI-BAN & ROBUST FETCH EDITION)
#
# WHAT THIS FILE DOES:
#   Fetches NSE end-of-day delivery volume data from the NSE bhavcopy archive.
#   Called by eod_scanner.py once per trading day (6:30 PM scan, with 5 retries),
#   and by intraday.py / live_scanner.py once per scan cycle (previous-day data).
#
# DATA SOURCE:
#   NSE Security-wise Delivery Position file (published daily after market close).
#   URL pattern:
#     https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
#
#   Columns we use from this file:
#     SYMBOL      — NSE ticker symbol (e.g. "RELIANCE", "INFY")
#     DELIV_QTY   — Total delivery quantity (shares that changed hands, not intraday)
#     DELIV_PER   — Delivery % of total traded quantity (pre-computed by NSE)
#
#   DELIV_PER is the key metric:
#     < 25%  → mostly intraday churn, institutional conviction is low
#     25–40% → moderate delivery, mixed participation
#     40–60% → solid delivery, genuine positional interest
#     ≥ 60%  → high delivery, strong institutional / positional conviction
#
# PUBLICATION TIMING:
#   NSE publishes this file between 5:00 PM and 6:00 PM IST.
# =====================================================================================

import pandas as pd
import requests
import logging
import io
import random
import time
from datetime import date, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 25
MAX_RETRIES = 5

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

def _get_robust_session() -> requests.Session:
    """Creates a session that automatically handles temporary 5xx errors from NSE."""
    session = requests.Session()
    retry = Retry(
        total=5,
        read=5,
        connect=5,
        backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def fetch_delivery_data(trading_date: date) -> dict[str, float]:
    """
    Fetches the NSE sec_bhavdata_full.csv for a specific date.
    Returns a dictionary of {SYMBOL: DELIVERY_PERCENTAGE}.
    """
    date_str = trading_date.strftime("%d%m%Y")
    url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

    session = _get_robust_session()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"🌐 Fetching Bhavcopy for {trading_date} | Attempt {attempt}/{MAX_RETRIES}...")
            response = session.get(url, headers=headers, timeout=FETCH_TIMEOUT)

            if response.status_code == 200:
                content = response.content.decode("utf-8")
                
                # If NSE returns a tiny file, it's an error page or holidays
                if len(content) < 1000:
                    logger.warning(f"⚠️ NSE returned empty or invalid data for {trading_date}. (Holiday?)")
                    return {}

                df = pd.read_csv(io.StringIO(content))

                required = {"SYMBOL", "DELIV_QTY", "DELIV_PER"}
                missing = required - set(df.columns)

                if missing:
                    logger.warning(f"⚠️ Bhavcopy missing columns {missing} | Available: {list(df.columns)[:10]}")
                    return {}

                # Strip whitespace from symbol column (NSE sometimes pads with spaces)
                df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()
                
                # DELIV_PER can contain "-" for stocks with no delivery data.
                df["DELIV_PER"] = pd.to_numeric(df["DELIV_PER"], errors="coerce")
                df = df.dropna(subset=["DELIV_PER"])

                delivery_map = dict(zip(df["SYMBOL"], df["DELIV_PER"].astype(float)))
                logger.info(f"✅ Bhavcopy parsed successfully | {len(delivery_map)} symbols loaded.")
                return delivery_map

            elif response.status_code == 404:
                logger.warning(f"⚠️ NSE Bhavcopy 404 Not Found for {trading_date}. (Market likely closed)")
                return {}
            else:
                logger.warning(f"⚠️ NSE returned HTTP {response.status_code} on attempt {attempt}.")

        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ Connection error on attempt {attempt}: {e}")

        # Sleep before retrying, increasing the delay each time
        time.sleep(2 ** attempt)

    logger.error(f"❌ Failed to fetch Bhavcopy for {trading_date} after {MAX_RETRIES} attempts.")
    return {}

def fetch_previous_day_delivery() -> dict[str, float]:
    """
    Attempts to fetch yesterday's delivery data. If yesterday was a weekend/holiday,
    it walks backward up to 5 days to find the last valid trading session.
    """
    for days_back in range(1, 6):
        target_date = date.today() - timedelta(days=days_back)
        if target_date.weekday() >= 5: # Skip weekends entirely
            continue
            
        data = fetch_delivery_data(target_date)
        if data:
            return data
            
    logger.warning("⚠️ Could not find valid delivery data in the last 5 days.")
    return {}

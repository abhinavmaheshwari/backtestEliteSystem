# =====================================================================================
# app/delivery_data.py
#
# WHAT THIS FILE DOES:
#   Fetches NSE end-of-day delivery volume data from the NSE bhavcopy archive.
# =====================================================================================

import logging
import requests
import pandas as pd
import time
import random
import io
from datetime import date, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BHAVCOPY_URL = (
    "https://archives.nseindia.com/products/content/"
    "sec_bhavdata_full_{date_str}.csv"
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
]

FETCH_TIMEOUT = 30
MAX_RETRIES = 5

def _get_robust_session() -> requests.Session:
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

def _last_trading_date(reference: date) -> date:
    d = reference - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def fetch_previous_day_delivery() -> dict[str, float]:
    from datetime import datetime as _dt
    today = _dt.now().date()
    for days_back in range(1, 5):
        candidate = today - timedelta(days=days_back)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        result = fetch_delivery_data(candidate)
        if result:
            logger.info(f"📦 Previous-day delivery loaded | Date={candidate}")
            return result
    return {}

def fetch_delivery_data(trading_date: date) -> dict[str, float]:
    date_str = trading_date.strftime("%d%m%Y")
    url      = BHAVCOPY_URL.format(date_str=date_str)
    session  = _get_robust_session()

    for attempt in range(1, MAX_RETRIES + 1):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://www.nseindia.com/",
        }
        try:
            response = session.get(url, headers=headers, timeout=FETCH_TIMEOUT)
            if response.status_code == 404:
                return {}
            if response.status_code == 200:
                raw_csv = response.text
                if len(raw_csv) < 1000:
                    continue
                df = pd.read_csv(io.StringIO(raw_csv))
                df.columns = [c.strip().upper() for c in df.columns]
                if not {"SYMBOL", "DELIV_PER"}.issubset(df.columns):
                    return {}
                df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()
                df["DELIV_PER"] = pd.to_numeric(df["DELIV_PER"], errors="coerce")
                return dict(zip(df["SYMBOL"], df["DELIV_PER"].astype(float)))
        except Exception as e:
            logger.warning(f"⚠️ Bhavcopy attempt {attempt} failed: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
    return {}

# =====================================================================================
# app/delivery_data.py
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
#   eod_scanner.py starts at 6:30 PM and retries up to 5 times (10-min gaps)
#   to handle delayed publication. intraday.py and live_scanner.py fetch the
#   previous trading day's data once at scan-start — no retry needed there.
#   If the fetch fails for any reason, callers receive an empty dict and proceed
#   without delivery scoring (treated as an optional bonus, never a hard filter).
#
# WHY NOT USE THE NSE API ENDPOINT?
#   NSE's equity API (quote-equity?section=trade_info) requires session cookies
#   that expire frequently. Maintaining cookie sessions in a headless script is
#   fragile and breaks silently. The bhavcopy archive URL is cookie-free, stable,
#   and has been published in the same format since 2010.
#
# USAGE:
#   from delivery_data import fetch_delivery_data
#   delivery_map = fetch_delivery_data(date)   # returns {symbol: delivery_pct}
#   pct = delivery_map.get("RELIANCE", None)   # None if data unavailable
# =====================================================================================

import logging
import requests
import pandas as pd
import time
import random

from datetime import date
from io import StringIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# NSE bhavcopy URL template — date formatted as DDMMYYYY
BHAVCOPY_URL = (
    "https://archives.nseindia.com/products/content/"
    "sec_bhavdata_full_{date_str}.csv"
)

# Randomized User-Agents to prevent NSE cloud-flare blocking
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]

# Timeout for the bhavcopy download (seconds).
FETCH_TIMEOUT = 30
MAX_RETRIES = 5

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


def _last_trading_date(reference: date) -> date:
    """
    Returns the most recent trading weekday before `reference`.
    Skips Saturday (5) and Sunday (6) by stepping back with timedelta.
    Does not account for NSE holidays — bhavcopy fetch will return 404 on those,
    which is handled gracefully by fetch_delivery_data() already.
    """
    from datetime import timedelta
    d = reference - timedelta(days=1)
    while d.weekday() >= 5:   # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def fetch_previous_day_delivery() -> dict[str, float]:
    """
    Fetches delivery data for the most recent completed trading day.
    Tries up to 3 prior weekdays to handle NSE holidays gracefully.
    Used by intraday.py and live_scanner.py at scan-start.
    Returns an empty dict if unavailable — callers handle None gracefully.
    """
    from datetime import datetime as _dt, timedelta
    today = _dt.now().date()

    for days_back in range(1, 5):
        candidate = today - timedelta(days=days_back)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        result = fetch_delivery_data(candidate)
        if result:
            logger.info(f"📦 Previous-day delivery loaded | Date={candidate} | {len(result)} symbols")
            return result
        # 404 (holiday/weekend) → try the next day back silently

    logger.info("📦 Previous-day delivery unavailable after 4-day lookback")
    return {}


def fetch_delivery_data(trading_date: date) -> dict[str, float]:
    """
    Download and parse the NSE security-wise delivery position file for a given date.

    Parameters
    ----------
    trading_date : date
        The trading date to fetch delivery data for.
        Should always be called with today's date from eod_scanner.py.

    Returns
    -------
    dict[str, float]
        Mapping of NSE symbol → delivery percentage (0.0 to 100.0).
        Returns an empty dict on any error — callers must handle None gracefully.

    Examples
    --------
    >>> delivery_map = fetch_delivery_data(date(2025, 5, 15))
    >>> delivery_map.get("RELIANCE")
    54.32
    >>> delivery_map.get("NONEXISTENT")   # returns None, not KeyError
    None
    """

    date_str = trading_date.strftime("%d%m%Y")
    url      = BHAVCOPY_URL.format(date_str=date_str)

    logger.info(f"📦 Fetching NSE bhavcopy | Date={trading_date} | URL={url}")

    session = _get_robust_session()

    for attempt in range(1, MAX_RETRIES + 1):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "

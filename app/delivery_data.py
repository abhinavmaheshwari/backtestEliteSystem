# =====================================================================================
# app/delivery_data.py
#
# WHAT THIS FILE DOES:
#   Fetches NSE end-of-day delivery volume data from the NSE bhavcopy archive.
#   Called once per trading day by eod_scanner.py at the start of the 6:00 PM scan.
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
#   The eod_scanner now starts at 6:00 PM to ensure the file is reliably available.
#   No retry logic is needed at scan time — the file is always present by 6 PM.
#   If the fetch fails (network error, NSE maintenance), the function returns an empty
#   dict. The scanner and scoring engine handle this gracefully — delivery data is
#   treated as an optional scoring bonus, never a hard filter.
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

from datetime import date
from io import StringIO

logger = logging.getLogger(__name__)

# NSE bhavcopy URL template — date formatted as DDMMYYYY
BHAVCOPY_URL = (
    "https://archives.nseindia.com/products/content/"
    "sec_bhavdata_full_{date_str}.csv"
)

# HTTP headers that mimic a browser — NSE returns 403 without a User-Agent
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://www.nseindia.com/",
}

# Timeout for the bhavcopy download (seconds).
# The file is ~3–5 MB. 30 seconds is generous for any reasonable connection.
FETCH_TIMEOUT = 30


def _last_trading_date(reference: date) -> date:
    """
    Returns the most recent trading weekday before `reference`.
    Steps back one day at a time skipping Saturday (5) and Sunday (6).
    Does not account for NSE holidays — bhavcopy fetch will return 404 on those,
    which is handled gracefully by fetch_delivery_data() already.
    """
    d = reference
    while True:
        d = date(d.year, d.month, d.day - 1) if d.day > 1 else date(
            d.year if d.month > 1 else d.year - 1,
            d.month - 1 if d.month > 1 else 12,
            31
        )
        # Use timedelta arithmetic to avoid manual day-rollover bugs
        from datetime import timedelta
        d = reference - timedelta(days=1)
        while d.weekday() >= 5:   # 5=Sat, 6=Sun
            d -= timedelta(days=1)
        return d


def fetch_previous_day_delivery() -> dict[str, float]:
    """
    Fetches delivery data for the most recent completed trading day.
    Used by intraday.py and live_scanner.py at scan-start to enrich
    today's scoring with the previous session's delivery conviction.

    Returns an empty dict if unavailable — callers handle None gracefully.
    """
    from datetime import datetime as _dt, timedelta
    today = _dt.now().date()
    prev  = today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)

    logger.info(f"📦 Fetching previous-day delivery data | Date={prev}")
    return fetch_delivery_data(prev)


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

    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=FETCH_TIMEOUT)

        if response.status_code == 404:
            # NSE returns 404 for non-trading days (weekends, holidays).
            # This is expected and not an error — log info, not warning.
            logger.info(
                f"📦 Bhavcopy not found (404) — likely a non-trading day: {trading_date}"
            )
            return {}

        if response.status_code != 200:
            logger.warning(
                f"⚠️ Bhavcopy fetch failed | Status={response.status_code} | Date={trading_date}"
            )
            return {}

        # ── PARSE CSV ─────────────────────────────────────────────────────────────
        # The bhavcopy CSV has a header row. Column names have leading/trailing spaces
        # in some NSE versions — strip them all.
        raw_csv = response.text
        df      = pd.read_csv(StringIO(raw_csv))

        # Normalize column names: strip whitespace, uppercase
        df.columns = [c.strip().upper() for c in df.columns]

        # Verify required columns exist
        required = {"SYMBOL", "DELIV_QTY", "DELIV_PER"}
        missing  = required - set(df.columns)

        if missing:
            logger.warning(
                f"⚠️ Bhavcopy missing columns {missing} | "
                f"Available: {list(df.columns)[:10]} | Date={trading_date}"
            )
            return {}

        # Strip whitespace from symbol column (NSE sometimes pads with spaces)
        df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()

        # DELIV_PER can contain "-" for stocks with no delivery data (e.g. F&O-only).
        # Coerce those to NaN, then drop them — we only want clean numeric values.
        df["DELIV_PER"] = pd.to_numeric(df["DELIV_PER"], errors="coerce")
        df = df.dropna(subset=["DELIV_PER"])

        # Build the symbol → delivery_pct mapping
        delivery_map = dict(zip(df["SYMBOL"], df["DELIV_PER"].astype(float)))

        logger.info(
            f"✅ Bhavcopy parsed | {len(delivery_map)} symbols with delivery data | Date={trading_date}"
        )

        return delivery_map

    except requests.exceptions.Timeout:
        logger.warning(f"⚠️ Bhavcopy fetch timed out after {FETCH_TIMEOUT}s | Date={trading_date}")
        return {}

    except requests.exceptions.ConnectionError as e:
        logger.warning(f"⚠️ Bhavcopy connection error: {e} | Date={trading_date}")
        return {}

    except Exception:
        logger.exception(f"❌ Unexpected error fetching bhavcopy | Date={trading_date}")
        return {}

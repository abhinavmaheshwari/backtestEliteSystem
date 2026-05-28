# =====================================================================================
# app/delivery_data.py
#
# WHAT THIS FILE DOES:
#   Fetches NSE end-of-day delivery volume data from the NSE bhavcopy archive.
#   Called once per trading day by eod_scanner.py at the start of each 6 PM scan
#   attempt. Includes retry logic (up to 3 attempts with exponential backoff) so
#   that transient NSE server hiccups don't silently drop delivery data.
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
#   The eod_scanner runs multiple attempts between 6:00 PM and 7:00 PM.
#   Retry logic here ensures transient fetch failures are recovered.
#   If ALL retries fail, the function returns an empty dict — delivery data is
#   treated as an optional scoring bonus, never a hard filter.
#
# WHY NOT USE THE NSE API ENDPOINT?
#   NSE's equity API (quote-equity?section=trade_info) requires session cookies
#   that expire frequently. Maintaining cookie sessions in a headless script is
#   fragile and breaks silently. The bhavcopy archive URL is cookie-free, stable,
#   and has been published in the same format since 2010.
#
# RETRY STRATEGY:
#   Up to MAX_RETRIES attempts with exponential backoff (RETRY_BACKOFF_SECONDS).
#   Retries cover: connection errors, timeouts, unexpected HTTP status codes.
#   404 is NOT retried — it is expected on non-trading days and returns {} immediately.
#
# USAGE:
#   from delivery_data import fetch_delivery_data
#   delivery_map = fetch_delivery_data(date)   # returns {symbol: delivery_pct}
#   pct = delivery_map.get("RELIANCE", None)   # None if data unavailable
# =====================================================================================

import logging
import time
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

# Retry configuration.
# 3 attempts covers transient NSE CDN hiccups without delaying the scan too long.
# Backoff: 5s, 10s — total worst-case extra wait ~15s before giving up.
MAX_RETRIES           = 3
RETRY_BACKOFF_SECONDS = 5   # multiplied by attempt number (5s, 10s)


def fetch_delivery_data(trading_date: date) -> dict[str, float]:
    """
    Download and parse the NSE security-wise delivery position file for a given date.
    Retries up to MAX_RETRIES times on transient failures (connection errors, timeouts,
    unexpected HTTP status codes). Returns {} immediately on 404 (non-trading day).

    Parameters
    ----------
    trading_date : date
        The trading date to fetch delivery data for.
        Should always be called with today's date from eod_scanner.py.

    Returns
    -------
    dict[str, float]
        Mapping of NSE symbol → delivery percentage (0.0 to 100.0).
        Returns an empty dict on any unrecoverable error.

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

    logger.info(
        f"📦 Bhavcopy fetch starting | Date={trading_date} | "
        f"URL={url} | MaxRetries={MAX_RETRIES}"
    )

    for attempt in range(1, MAX_RETRIES + 1):

        logger.info(f"📦 Bhavcopy attempt {attempt}/{MAX_RETRIES} | Date={trading_date}")

        try:
            fetch_start = time.monotonic()
            response    = requests.get(url, headers=REQUEST_HEADERS, timeout=FETCH_TIMEOUT)
            elapsed     = time.monotonic() - fetch_start

            logger.info(
                f"📦 HTTP {response.status_code} | "
                f"Elapsed={elapsed:.1f}s | "
                f"ContentLength={len(response.content):,} bytes | "
                f"Attempt={attempt}/{MAX_RETRIES}"
            )

            # ── 404: Non-trading day — do NOT retry, return immediately ──────────────
            if response.status_code == 404:
                logger.info(
                    f"📦 Bhavcopy 404 — non-trading day (weekend/holiday): {trading_date} | "
                    f"Not retrying."
                )
                return {}

            # ── Non-200 that is worth retrying ────────────────────────────────────────
            if response.status_code != 200:
                logger.warning(
                    f"⚠️ Bhavcopy unexpected HTTP {response.status_code} | "
                    f"Date={trading_date} | Attempt={attempt}/{MAX_RETRIES}"
                )
                _maybe_retry(attempt, trading_date)
                continue

            # ── PARSE CSV ─────────────────────────────────────────────────────────────
            # Column names have leading/trailing spaces in some NSE versions — strip all.
            raw_csv = response.text

            if not raw_csv or len(raw_csv.strip()) < 100:
                logger.warning(
                    f"⚠️ Bhavcopy response body is empty or too short "
                    f"({len(raw_csv)} chars) | Date={trading_date} | Attempt={attempt}/{MAX_RETRIES}"
                )
                _maybe_retry(attempt, trading_date)
                continue

            try:
                df = pd.read_csv(StringIO(raw_csv))
            except Exception as parse_err:
                logger.warning(
                    f"⚠️ Bhavcopy CSV parse error: {parse_err} | "
                    f"Date={trading_date} | Attempt={attempt}/{MAX_RETRIES} | "
                    f"First 200 chars: {raw_csv[:200]!r}"
                )
                _maybe_retry(attempt, trading_date)
                continue

            # Normalize column names: strip whitespace, uppercase
            df.columns = [c.strip().upper() for c in df.columns]

            logger.info(
                f"📦 Bhavcopy columns found: {list(df.columns)[:15]} | "
                f"Rows={len(df):,} | Date={trading_date}"
            )

            # Verify required columns exist
            required = {"SYMBOL", "DELIV_QTY", "DELIV_PER"}
            missing  = required - set(df.columns)

            if missing:
                logger.error(
                    f"❌ Bhavcopy missing required columns {missing} | "
                    f"Available columns (first 15): {list(df.columns)[:15]} | "
                    f"Date={trading_date} | Attempt={attempt}/{MAX_RETRIES} — "
                    f"NSE may have changed the file format."
                )
                # Column mismatch is likely a format change, not a transient error.
                # Still retry in case we got a partial/corrupt download.
                _maybe_retry(attempt, trading_date)
                continue

            # Strip whitespace from symbol column (NSE sometimes pads with spaces)
            df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()

            # DELIV_PER can contain "-" for stocks with no delivery data (e.g. F&O-only).
            # Coerce those to NaN, then drop them.
            df["DELIV_PER"] = pd.to_numeric(df["DELIV_PER"], errors="coerce")
            rows_before      = len(df)
            df               = df.dropna(subset=["DELIV_PER"])
            rows_dropped     = rows_before - len(df)

            if rows_dropped > 0:
                logger.info(
                    f"📦 Dropped {rows_dropped:,} rows with non-numeric DELIV_PER "
                    f"(F&O-only / suspended stocks) | Remaining={len(df):,}"
                )

            delivery_map = dict(zip(df["SYMBOL"], df["DELIV_PER"].astype(float)))

            logger.info(
                f"✅ Bhavcopy parsed successfully | "
                f"{len(delivery_map):,} symbols with delivery data | "
                f"Date={trading_date} | Attempt={attempt}/{MAX_RETRIES} | "
                f"Elapsed={elapsed:.1f}s"
            )

            # Spot-check: log a few well-known symbols for sanity
            for sentinel in ("RELIANCE", "INFY", "TCS", "HDFCBANK"):
                val = delivery_map.get(sentinel)
                if val is not None:
                    logger.info(f"📦 Spot-check: {sentinel} delivery={val:.1f}%")
                    break   # one is enough

            return delivery_map

        except requests.exceptions.Timeout:
            logger.warning(
                f"⚠️ Bhavcopy fetch timed out after {FETCH_TIMEOUT}s | "
                f"Date={trading_date} | Attempt={attempt}/{MAX_RETRIES}"
            )
            _maybe_retry(attempt, trading_date)

        except requests.exceptions.ConnectionError as conn_err:
            logger.warning(
                f"⚠️ Bhavcopy connection error: {conn_err} | "
                f"Date={trading_date} | Attempt={attempt}/{MAX_RETRIES}"
            )
            _maybe_retry(attempt, trading_date)

        except Exception:
            logger.exception(
                f"❌ Unexpected error fetching bhavcopy | "
                f"Date={trading_date} | Attempt={attempt}/{MAX_RETRIES}"
            )
            _maybe_retry(attempt, trading_date)

    # All retries exhausted
    logger.error(
        f"❌ Bhavcopy fetch FAILED after {MAX_RETRIES} attempts | "
        f"Date={trading_date} | "
        f"Scoring will proceed WITHOUT delivery bonus for all stocks today."
    )
    return {}


def _maybe_retry(attempt: int, trading_date: date) -> None:
    """
    If not the last attempt, sleep with exponential backoff before the next try.
    Logs the wait so the operator can see what's happening in real time.
    """
    if attempt < MAX_RETRIES:
        wait_secs = RETRY_BACKOFF_SECONDS * attempt
        logger.info(
            f"📦 Retrying bhavcopy in {wait_secs}s | "
            f"Date={trading_date} | Attempt {attempt} of {MAX_RETRIES}"
        )
        time.sleep(wait_secs)
    else:
        logger.warning(
            f"⚠️ Bhavcopy: all {MAX_RETRIES} attempts exhausted | "
            f"Date={trading_date}"
        )

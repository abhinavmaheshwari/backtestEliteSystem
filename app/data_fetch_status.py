"""
app/data_fetch_status.py

Lightweight helper wrapper around the DB health table so all external-fetching
modules can report successes and failures in a consistent way.

API:
  mark_success(source_name: str)
  mark_failure(source_name: str, error: Exception | str)

This file intentionally keeps logic tiny so it can be imported in many places
without pulling heavy dependencies.
"""
from datetime import datetime, timezone
from typing import Optional, Union
import logging
import traceback

from database import upsert_data_fetch_health

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Map external sources to scanners that rely on them. Update this map when adding scanners.
SOURCE_IMPACT_MAP = {
    'yfinance': ["EOD", "INTRADAY", "1H", "REVERSAL", "Wealth Engine"],
    'nse_announcements': ["Wealth Engine", "DAILY_BUILDER"],
    'nse_bhavcopy': ["EOD", "DAILY_BUILDER"],
    'scraperapi': ["Pledge Worker", "Pledge Worker"],
    'telegram': ["Telegram Engine"],
    'gemini': ["AI Worker", "Wealth Engine"],
}


def _split_source(source_name: str) -> tuple[str, Optional[str]]:
    """Split a source_name like 'yfinance:15m' into (base, scope).
    scope can be interval e.g., '15m','1h','1d' or a scanner name like 'INTRADAY'."""
    if ':' in source_name:
        base, scope = source_name.split(':', 1)
        return base, scope
    return source_name, None


INTERVAL_TO_SCANNER = {
    '1m': 'INTRADAY',
    '15m': 'INTRADAY',
    '1h': '1H',
    '60m': '1H',
    '1d': 'EOD',
    'daily': 'EOD',
}


def mark_success(source_name: str) -> None:
    try:
        upsert_data_fetch_health(source_name, last_success=_now_iso(), consecutive_failures=0)
        # Clear the external data source status but DO NOT touch scanner status
        # Scanners should only be marked DOWN/OK by their own main loop, not by data source status
        try:
            from database import upsert_scanner_health
            base, scope = _split_source(source_name)
            # Clear the generic external health row for the base provider as well
            upsert_scanner_health(f"External:{base}", status="OK", last_success=_now_iso(), today_alerts=0, error_msg=None)

            # REMOVED: Marking dependent scanners OK
            # Reason: We no longer mark scanners DOWN for data source failures,
            # so we shouldn't mark them OK either. Scanners manage their own status based on
            # whether their main loop is running (critical), not based on temporary API failures.
            
        except Exception:
            logger.debug(f"Could not update external data source status for {source_name}")
    except Exception:
        logger.exception(f"Failed to mark success for data source {source_name}")


def mark_failure(source_name: str, error: Optional[Union[Exception, str]] = None) -> None:
    try:
        # Fetch current failures and increment is handled inside DB helper; simply pass a new failure record
        if isinstance(error, Exception):
            msg = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        else:
            msg = str(error) if error is not None else None
        upsert_data_fetch_health(source_name, last_failure=_now_iso(), consecutive_failures=None, error_msg=msg)

        # Mark the external data source as DOWN but DO NOT propagate to scanners
        # Individual stock failures should NOT turn scanner RED - they go to fetch_errors table only
        try:
            from database import upsert_scanner_health, upsert_fetch_error
            base, scope = _split_source(source_name)
            # Mark the generic external row for the exact key (include scope if present)
            external_name = f"External:{source_name}" if scope else f"External:{base}"
            upsert_scanner_health(external_name, status="DOWN", last_success=None, today_alerts=0, error_msg=(msg or 'External data source failure'))

            # Log to fetch_errors for detailed tracking, but DO NOT mark scanner DOWN
            # The theory: if yfinance fails for one stock, we log it but scanner keeps running
            # Only CRITICAL (scanner loop crash) should turn scanner RED
            # REMOVED: Propagating impact to known dependent scanners
            # This was incorrectly turning scanners RED for temporary API failures
            
        except Exception:
            logger.debug(f"Could not update external data source status for {source_name}")
    except Exception:
        logger.exception(f"Failed to mark failure for data source {source_name}: {error}")



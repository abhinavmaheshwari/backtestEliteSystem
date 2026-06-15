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
}


def _split_source(source_name: str) -> tuple[str, str | None]:
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
        # Also clear any external-scanner health flag for this data source
        try:
            from database import upsert_scanner_health
            base, scope = _split_source(source_name)
            # Clear the generic external health row for the base provider as well
            upsert_scanner_health(f"External:{base}", status="OK", last_success=_now_iso(), today_alerts=0, error_msg=None)

            # Decide which scanners to mark OK. If scope provided, try to narrow the impact.
            impacted = SOURCE_IMPACT_MAP.get(base, [])
            targeted = []
            if scope:
                # If scope is interval, map to scanner name
                mapped = INTERVAL_TO_SCANNER.get(scope.lower())
                if mapped:
                    targeted = [sc for sc in impacted if sc == mapped]
                else:
                    # If scope directly names a scanner (e.g., 'INTRADAY'), use it
                    targeted = [sc for sc in impacted if sc.upper() == scope.upper()]
            else:
                targeted = impacted

            for sc in targeted:
                try:
                    upsert_scanner_health(sc, status="OK", last_success=_now_iso(), today_alerts=0, error_msg=f"Cleared External:{base} issue")
                except Exception:
                    logger.debug(f"Could not mark scanner {sc} OK for source {source_name}")
        except Exception:
            logger.debug(f"Could not update scanner health for External:{source_name}")
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

        # Also mark an external scanner health row so the dashboard shows which external provider is failing
        try:
            from database import upsert_scanner_health
            base, scope = _split_source(source_name)
            # Mark the generic external row for the exact key (include scope if present)
            external_name = f"External:{source_name}" if scope else f"External:{base}"
            upsert_scanner_health(external_name, status="DOWN", last_success=None, today_alerts=0, error_msg=(msg or 'External data source failure'))

            # Propagate impact to known dependent scanners but narrow if scope is provided
            impacted = SOURCE_IMPACT_MAP.get(base, [])
            targeted = []
            if scope:
                mapped = INTERVAL_TO_SCANNER.get(scope.lower())
                if mapped:
                    targeted = [sc for sc in impacted if sc == mapped]
                else:
                    targeted = [sc for sc in impacted if sc.upper() == scope.upper()]
            else:
                targeted = impacted

            for sc in targeted:
                try:
                    upsert_scanner_health(sc, status="DOWN", last_success=None, today_alerts=0, error_msg=(msg or f'Impacted by {source_name} failure'))
                except Exception:
                    logger.debug(f"Could not mark scanner {sc} DOWN for source {source_name}")
        except Exception:
            logger.debug(f"Could not update scanner health for External:{source_name}")
    except Exception:
        logger.exception(f"Failed to mark failure for data source {source_name}: {error}")


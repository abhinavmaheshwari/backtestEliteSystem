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

from app.database import upsert_data_fetch_health

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_success(source_name: str) -> None:
    try:
        upsert_data_fetch_health(source_name, last_success=_now_iso(), consecutive_failures=0)
    except Exception:
        logger.exception(f"Failed to mark success for data source {source_name}")


def mark_failure(source_name: str, error: Optional[Union[Exception, str]] = None) -> None:
    try:
        # Fetch current failures and increment is handled inside DB helper; simply pass a new failure record
        msg = str(error) if error is not None else None
        upsert_data_fetch_health(source_name, last_failure=_now_iso(), consecutive_failures=None, error_msg=msg)
    except Exception:
        logger.exception(f"Failed to mark failure for data source {source_name}: {error}")


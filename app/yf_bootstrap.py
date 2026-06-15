# Created by Copilot CLI: early yfinance tzcache bootstrap
import os
import logging

logger = logging.getLogger(__name__)

# Ensure app-local data tzcache exists and point yfinance to it before other modules import yfinance.
try:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    TZCACHE_DIR = os.path.join(BASE_DIR, "data", "tzcache")
    os.makedirs(TZCACHE_DIR, exist_ok=True)
    # Set XDG_CACHE_HOME so libraries using XDG respect our writable cache path
    os.environ.setdefault("XDG_CACHE_HOME", os.path.dirname(TZCACHE_DIR))
except Exception as e:
    logger.debug(f"Failed to prepare tzcache dir: {e}")

# Attempt to import yfinance and set its tz cache location safely.
try:
    import yfinance as yf
    try:
        yf.set_tz_cache_location(TZCACHE_DIR)
    except Exception:
        # Not fatal; avoid raising to callers. The goal is to avoid import-time errors.
        logger.debug("yfinance.set_tz_cache_location failed; proceeding")
except Exception as e:
    # Import may fail in rare environments; swallow to avoid crashing importers.
    logger.debug(f"yfinance import during bootstrap failed: {e}")

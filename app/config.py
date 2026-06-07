# =====================================================================================
# app/config.py
# Centralized configuration for all scanners
# =====================================================================================

import os

# =====================================================================================
# BASE DIRECTORY
# =====================================================================================

BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

# =====================================================================================
# TELEGRAM CONFIG (DYNAMIC ENVIRONMENT READ)
# =====================================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

_thread_eod      = os.getenv("THREAD_EOD")
_thread_intraday = os.getenv("THREAD_INTRADAY")
_thread_1h       = os.getenv("THREAD_1H")

THREAD_EOD      = int(_thread_eod)      if _thread_eod      else None
THREAD_INTRADAY = int(_thread_intraday) if _thread_intraday else None
THREAD_1H       = int(_thread_1h)       if _thread_1h       else None

# =====================================================================================
# DATA DIRECTORY & PATHS
# =====================================================================================

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

WATCHLIST_PATH = os.path.join(DATA_DIR, "elite_fundamental_watchlist.parquet")
DB_PATH = os.path.join(DATA_DIR, "alerts.db")

# =====================================================================================
# SCORE THRESHOLDS
# =====================================================================================

SCORE_THRESHOLDS = {
    "15m": 78,
    "1h":  80,
    "1d":  82,
}

# =====================================================================================
# SCAN CONFIGURATION
# =====================================================================================

SCAN_CONFIG = {
    "15m": {
        "MIN_SIGNALS":        2,
        "MIN_BODY_RATIO":     0.60,
        "MIN_CLOSE_POSITION": 0.70,
        "MAX_UPPER_WICK":     0.20,
        "MIN_VOLUME_RATIO":   2.5,
        "MIN_VOLUME_AVG":     150_000,
        "MIN_RSI":            58,
        "MAX_RSI":            85,  
    },
    "1h": {
        "MIN_SIGNALS":        3,
        "MIN_BODY_RATIO":     0.55,
        "MIN_CLOSE_POSITION": 0.65,
        "MAX_UPPER_WICK":     0.25,
        "MIN_VOLUME_RATIO":   2.0,
        "MIN_VOLUME_AVG":     100_000,
        "MIN_RSI":            55,
        "MAX_RSI":            86,  
    },
    "1d": {
        "MIN_SIGNALS":        1,
        "MIN_BODY_RATIO":     0.45,
        "MIN_CLOSE_POSITION": 0.65,
        "MAX_UPPER_WICK":     0.35,
        "MIN_VOLUME_RATIO":   1.8,
        "MIN_VOLUME_AVG":     50_000,
        "MIN_RSI":            55,
        "MAX_RSI":            88,  
    },
}

ADX_MIN_THRESHOLD = 25

DELIVERY_CONVICTION_THRESHOLDS = {
    "institutional": 60,
    "positional":    40,
    "moderate":      25,
    "intraday_churn": 0,
}

BATCH_DOWNLOAD_SIZE = 30
YAHOO_TIMEOUT = 30
DEDUP_DAYS = 7

TELEGRAM_CHUNK_SIZE = 10
TELEGRAM_RETRIES = 3
TELEGRAM_TIMEOUT = 10
LOG_LEVEL = "INFO"

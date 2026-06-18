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
_thread_reversal = os.getenv("THREAD_REVERSAL")

THREAD_EOD      = int(_thread_eod)      if _thread_eod      else None
THREAD_INTRADAY = int(_thread_intraday) if _thread_intraday else None
THREAD_1H       = int(_thread_1h)       if _thread_1h       else None
THREAD_REVERSAL = int(_thread_reversal) if _thread_reversal else None

# =====================================================================================
# DATA DIRECTORY & PATHS
# =====================================================================================

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

WATCHLIST_PATH = os.path.join(DATA_DIR, "elite_fundamental_watchlist.parquet")
DB_PATH = os.path.join(DATA_DIR, "alerts.db")

# =====================================================================================
# SCORE THRESHOLDS & AI
# =====================================================================================

ENABLE_AI_SENTIMENT_SCORE = True  # Set False to disable experimental AI sentiment scoring for audit/backtest runs

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
        "MIN_RSI":            52,
        "MAX_RSI":            87,
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
MIN_STOCK_PRICE = 100.0    # No penny stocks — matches daily_builder MIN_PRICE

# LIQUIDITY THRESHOLDS (in Rupees)
MIN_DAILY_LIQUIDITY_RUPEES_WATCHLIST = 150_000_000  # ₹15 Cr/day for raw watchlist
MIN_DAILY_LIQUIDITY_RUPEES_WEALTH    = 10_000_000   # ₹1 Cr/day for long-term wealth engine

DELIVERY_CONVICTION_THRESHOLDS = {
    "institutional": 60,
    "positional":    40,
    "moderate":      25,
    "intraday_churn": 0,
}

BATCH_DOWNLOAD_SIZE = 30
YAHOO_TIMEOUT = 30
PRICE_CACHE_TTL_SECONDS = 60  # Changed from 180s: Intraday runs every 5min (need fresh cache hit)


TELEGRAM_CHUNK_SIZE = 10
TELEGRAM_RETRIES = 3
TELEGRAM_TIMEOUT = 10
LOG_LEVEL = "INFO"

# =====================================================================================
# ANTI-FAKE-BREAKOUT PARAMETERS
# =====================================================================================

# Minimum % above prior high for a valid breakout (timeframe-aware)
MIN_BREAKOUT_MARGIN = {
    "15m": 0.003,   # 0.3% above prior high
    "1h":  0.005,   # 0.5%
    "1d":  0.007,   # 0.7%
}

# Breakout candle volume must be at least this multiple of 20-bar avg
MIN_BREAKOUT_VOLUME_RATIO = 1.5

# Reject if N prior candles are ALL bearish (no momentum build-up)
MAX_PRE_BREAKOUT_RED_CANDLES = 2

# BASE_WIDTH below this = tight consolidation = bonus-worthy setup
BASE_TIGHTNESS_THRESHOLD = 1.5

# BASE_WIDTH above this = volatile/choppy = penalize
BASE_VOLATILITY_THRESHOLD = 3.0

# =====================================================================================
# ANTI-OPERATOR-TRAP PARAMETERS
# =====================================================================================

# Bars to look back for climax top volume pattern
CLIMAX_VOLUME_LOOKBACK = 20

# Bars to look back for lower-high pattern (failed breakout retest)
LOWER_HIGH_LOOKBACK = 6

# Minimum candle range as % of price (below this = thin spread trap)
MIN_CANDLE_RANGE_PCT = 0.003   # 0.3%

# =====================================================================================
# SL/TARGET ATR CAPS (max target distance from entry, per timeframe)
# =====================================================================================

MAX_TARGET_ATR = {
    "15m": 5.0,     # Intraday targets capped at 5x ATR
    "1h":  8.0,     # 1H targets capped at 8x ATR
    "1d":  12.0,    # EOD targets capped at 12x ATR
}

# =====================================================================================
# FALLBACK PRICE PROVIDER (when YFinance rate-limited)
# =====================================================================================

ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "")  # Set via Railway env vars
ENABLE_PRICE_FALLBACK = os.getenv("ENABLE_PRICE_FALLBACK", "true").lower() == "true"


# =====================================================================================
# app/config.py (FIXED)
# Centralized configuration for all scanners
# FIX: All thresholds in one place instead of scattered across intraday.py, live_scanner.py, eod_scanner.py
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
# TELEGRAM CONFIG
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
# DATA DIRECTORY & PATHS — FIX: Centralized, not hardcoded in main.py
# =====================================================================================

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

WATCHLIST_PATH = os.path.join(DATA_DIR, "elite_fundamental_watchlist.parquet")
DB_PATH = os.path.join(DATA_DIR, "alerts.db")

# =====================================================================================
# SCORE THRESHOLDS — FIX: Centralized instead of hardcoded in each scanner
# Minimum composite score required to alert (0–100 scale)
# =====================================================================================

SCORE_THRESHOLDS = {
    "15m": 72,  # Intraday: higher threshold to filter noise from short timeframes
    "1h":  75,  # Live: balanced threshold for 1-hour bars
    "1d":  78,  # EOD: highest threshold since we have best data/delivery conviction
}

# =====================================================================================
# SCAN CONFIGURATION — FIX: All filter constants in one place per timeframe
# Each timeframe has different volatility/liquidity characteristics
# =====================================================================================

SCAN_CONFIG = {
    
    # INTRADAY (15m) — Tight filters for noisy short-term data
    "15m": {
        "MIN_SIGNALS":        2,       # Require 2+ breakout signals (confluence)
        "MIN_BODY_RATIO":     0.55,    # Candle body ≥ 55% of range (no doji)
        "MIN_CLOSE_POSITION": 0.70,    # Close in top 30% of bar (buyers held control)
        "MAX_UPPER_WICK":     0.25,    # Upper wick ≤ 25% of range (no rejection)
        "MIN_VOLUME_RATIO":   1.8,     # Current bar ≥ 1.8× 20-bar average
        "MIN_VOLUME_AVG":     100_000, # 20-bar average ≥ 100K shares (liquidity floor)
        "MIN_RSI":            55,      # RSI momentum sweet spot (avoid pre-breakout)
        "MAX_RSI":            75,      # RSI cap (avoid overbought chases)
    },
    
    # LIVE (1h) — Medium filters for 1-hour bars
    "1h": {
        "MIN_SIGNALS":        2,       # Require 2+ breakout signals
        "MIN_BODY_RATIO":     0.50,    # Candle body ≥ 50% of range
        "MIN_CLOSE_POSITION": 0.65,    # Close in top 35% of bar
        "MAX_UPPER_WICK":     0.30,    # Upper wick ≤ 30% of range
        "MIN_VOLUME_RATIO":   1.5,     # Current bar ≥ 1.5× 20-bar average
        "MIN_VOLUME_AVG":     50_000,  # 20-bar average ≥ 50K shares
        "MIN_RSI":            52,      # Slightly looser RSI (hourly has less noise)
        "MAX_RSI":            78,      # Slightly higher cap
    },
    
    # EOD (Daily) — Loosest filters since daily data is cleanest
    "1d": {
        "MIN_SIGNALS":        1,       # Can alert on single high-conviction signal
        "MIN_BODY_RATIO":     0.45,    # Candle body ≥ 45% of range
        "MIN_CLOSE_POSITION": 0.60,    # Close in top 40% of bar
        "MAX_UPPER_WICK":     0.35,    # Upper wick ≤ 35% of range
        "MIN_VOLUME_RATIO":   1.2,     # Current bar ≥ 1.2× 20-bar average
        "MIN_VOLUME_AVG":     10_000,  # 20-bar average ≥ 10K shares
        "MIN_RSI":            50,      # Looser RSI for daily (more genuine momentum)
        "MAX_RSI":            80,      # Higher cap OK for daily
    },
}

# =====================================================================================
# DELIVERY CONVICTION THRESHOLDS
# Used for bonus scoring in scoring_engine.py
# =====================================================================================

DELIVERY_CONVICTION_THRESHOLDS = {
    "institutional": 60,  # ≥60% delivery = institutional positioning
    "positional":    40,  # ≥40% delivery = genuine positional interest
    "moderate":      25,  # ≥25% delivery = acceptable participation
    "intraday_churn": 0,  # <25% delivery = mostly intraday churn
}

# =====================================================================================
# BATCH DOWNLOAD SETTINGS — FIX #2: Optimize yfinance API usage
# =====================================================================================

BATCH_DOWNLOAD_SIZE = 30  # Download 30 symbols per batch request (Yahoo handles well)
YAHOO_TIMEOUT = 30        # Timeout for yfinance requests (seconds)

# =====================================================================================
# ALERT DEDUPLICATION — Database lookback window
# =====================================================================================

DEDUP_DAYS = 7  # Don't re-alert the same stock + breakout_type combo within 7 days

# =====================================================================================
# TELEGRAM SETTINGS
# =====================================================================================

TELEGRAM_CHUNK_SIZE = 10  # Max stocks per Telegram message (larger = truncation risk)
TELEGRAM_RETRIES = 3      # Retry failed sends up to 3 times
TELEGRAM_TIMEOUT = 10     # Connection timeout (seconds)

# =====================================================================================
# LOGGING
# =====================================================================================

LOG_LEVEL = "INFO"  # Set to "DEBUG" for verbose per-bar decisions during development

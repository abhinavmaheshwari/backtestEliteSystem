# =====================================================================================
# app/config.py
# Centralized configuration for all scanners
#
# ── PERFORMANCE FIX SUMMARY (v5) ──────────────────────────────────────────────────
#
# Root cause analysis from live results:
#   EOD:      4 alerts, ALL negative (-1.7% to -3%)
#   Intraday: 10 alerts, 6 negative
#   1H:       14 alerts, 12 negative (worst hit-rate)
#
# Changes in this file:
#
# FIX 1 — SCORE_THRESHOLDS raised across all timeframes
#   Old: 72 / 75 / 78   →   New: 78 / 80 / 82
#   Rationale: The old thresholds let too many marginal setups through.
#   At the old levels, a stock with weak trend structure could still hit 72
#   and fire an alert. Raising the floor by 4–6 pts forces every alerting
#   stock to clear more of the additive scoring categories simultaneously.
#
# FIX 2 — SCAN_CONFIG volume minimums raised (all timeframes)
#   15m:  MIN_VOLUME_RATIO 1.8 → 2.5  (was letting low-conviction moves through)
#         MIN_VOLUME_AVG   100K → 150K (tighter liquidity floor for 15m bars)
#   1h:   MIN_VOLUME_RATIO 1.5 → 2.0  (hourly bars need real institutional flow)
#         MIN_VOLUME_AVG   50K → 100K
#   1d:   MIN_VOLUME_RATIO 1.2 → 1.8  (daily bar at only 1.2× avg is noise, not conviction)
#         MIN_VOLUME_AVG   10K → 50K   (was absurdly low — allows illiquid micro-caps)
#
# FIX 3 — SCAN_CONFIG RSI bounds tightened
#   15m:  MIN_RSI 55 → 58  (avoid entering before momentum established)
#         MAX_RSI 75 → 72  (avoid overbought chases on short timeframes)
#   1h:   MIN_RSI 52 → 55
#         MAX_RSI 78 → 74
#   1d:   MIN_RSI 50 → 55  (was so loose it allowed pre-breakout stall entries)
#         MAX_RSI 80 → 75  (daily stocks at RSI 80 are overextended, not breakouts)
#
# FIX 4 — SCAN_CONFIG candle quality tightened
#   15m:  MIN_BODY_RATIO 0.55 → 0.60  (stronger candle body required)
#         MAX_UPPER_WICK  0.25 → 0.20  (even less rejection tolerance intraday)
#   1h:   MIN_BODY_RATIO 0.50 → 0.55
#         MAX_UPPER_WICK  0.30 → 0.25
#   1d:   MIN_CLOSE_POSITION 0.60 → 0.65  (close must be in top 35% of day's range)
#
# FIX 5 — New ADX_MIN_THRESHOLD constant added
#   ADX < 25 was a soft disqualifier (score of 0 if < 22).
#   Raising the hard disqualifier threshold from 22 → 25 removes choppy/ranging
#   stocks that were slipping through with ADX 22–24.
#   Used in scoring_engine.py check_hard_disqualifiers().
#
# FIX 6 — New MIN_SIGNALS raised for 1h timeframe: 2 → 3
#   1H had the worst hit-rate (12/14 negative). A single signal at 1H is not
#   enough — the bar represents a full hour of committed buying.
#   Requiring 3 confluence signals (e.g. Weekly + Daily + Volume Surge) means
#   every 1H alert has structural overlap across multiple lookback windows.
#   15m stays at 2 (short-timeframe noise makes 3 too restrictive early in day).
#   1d stays at 1 (52W breakout on daily is genuinely rare and high-conviction alone).
#
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
# DATA DIRECTORY & PATHS
# =====================================================================================

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

WATCHLIST_PATH = os.path.join(DATA_DIR, "elite_fundamental_watchlist.parquet")
DB_PATH = os.path.join(DATA_DIR, "alerts.db")

# =====================================================================================
# SCORE THRESHOLDS
#
# FIX 1: Raised all thresholds to cut marginal setups.
#   Old: 72 / 75 / 78
#   New: 78 / 80 / 82
#
# Why these specific numbers?
#   The scoring breakdown is: Category(30) + Signals(24) + RSI(15) + Volume(20) +
#   Trend(10) + Bonuses(up to ~21). A stock with a weak category (8 pts), 2 signals
#   (16 pts), RSI 60 (15 pts), volume 2× (10 pts), good trend (8 pts) = 57 base.
#   With bonuses (bull stack +3, top-close +2, RSI accel +2) = 64. Under the old
#   72 threshold this was rejected. Under the new 78 threshold it's still rejected.
#   To clear 78 a stock now needs either: Elite/Financial Compounder category (30 pts)
#   OR strong volume (≥3× for 17–20 pts) AND good RSI alignment AND 3 signals.
#   This is intentional — we want fewer, higher-conviction alerts.
# =====================================================================================

SCORE_THRESHOLDS = {
    "15m": 78,   # RAISED from 72 — intraday: require strong multi-factor confluence
    "1h":  80,   # RAISED from 75 — 1H: was worst performer (12/14 negative)
    "1d":  82,   # RAISED from 78 — EOD: daily is our highest-conviction timeframe
}

# =====================================================================================
# SCAN CONFIGURATION
#
# FIX 2/3/4/6: Volume, RSI, candle quality, and signal count all tightened.
# See module-level docstring above for detailed rationale per change.
# =====================================================================================

SCAN_CONFIG = {

    # INTRADAY (15m) — Tighter filters for noisy short-term data
    "15m": {
        # FIX 6: stays at 2 — early-session momentum needs some flexibility
        "MIN_SIGNALS":        2,

        # FIX 4: raised from 0.55 → 0.60 — weaker candle bodies = more fakeouts
        "MIN_BODY_RATIO":     0.60,

        "MIN_CLOSE_POSITION": 0.70,   # unchanged — close in top 30% is already tight

        # FIX 4: tightened from 0.25 → 0.20 — less upper wick tolerance intraday
        "MAX_UPPER_WICK":     0.20,

        # FIX 2: raised from 1.8 → 2.5 — need real conviction on 15m bar
        "MIN_VOLUME_RATIO":   2.5,

        # FIX 2: raised from 100K → 150K — tighter liquidity floor
        "MIN_VOLUME_AVG":     150_000,

        # FIX 3: raised from 55 → 58 — avoid pre-momentum entries
        "MIN_RSI":            58,

        # FIX 3: tightened from 75 → 72 — avoid overbought chases on short bars
        "MAX_RSI":            72,
    },

    # LIVE (1h) — Tighter filters for 1-hour bars
    "1h": {
        # FIX 6: RAISED from 2 → 3 — 1H was worst performer; require 3 confluence signals
        # (e.g. Weekly Breakout + Daily Breakout + Volume Surge, or Monthly + Weekly + BB)
        # A single 1H breakout signal is not enough — the bar spans a full hour of price action.
        "MIN_SIGNALS":        3,

        # FIX 4: raised from 0.50 → 0.55
        "MIN_BODY_RATIO":     0.55,

        "MIN_CLOSE_POSITION": 0.65,   # unchanged

        # FIX 4: tightened from 0.30 → 0.25
        "MAX_UPPER_WICK":     0.25,

        # FIX 2: raised from 1.5 → 2.0 — hourly breakouts need institutional volume
        "MIN_VOLUME_RATIO":   2.0,

        # FIX 2: raised from 50K → 100K
        "MIN_VOLUME_AVG":     100_000,

        # FIX 3: raised from 52 → 55
        "MIN_RSI":            55,

        # FIX 3: tightened from 78 → 74
        "MAX_RSI":            74,
    },

    # EOD (Daily) — Tighter filters since this is our highest-conviction timeframe
    "1d": {
        # stays at 1 — 52W breakout on daily is rare and high-conviction alone
        "MIN_SIGNALS":        1,

        "MIN_BODY_RATIO":     0.45,   # unchanged — daily candles are naturally cleaner

        # FIX 4: raised from 0.60 → 0.65 — close must be in top 35% of the day's range
        "MIN_CLOSE_POSITION": 0.65,

        "MAX_UPPER_WICK":     0.35,   # unchanged — daily upper wick tolerance OK

        # FIX 2: raised from 1.2 → 1.8 — 1.2× daily volume is barely above average
        "MIN_VOLUME_RATIO":   1.8,

        # FIX 2: raised from 10K → 50K — 10K daily avg is a micro-cap with no fills
        "MIN_VOLUME_AVG":     50_000,

        # FIX 3: raised from 50 → 55 — RSI 50 is neutral, not momentum
        "MIN_RSI":            55,

        # FIX 3: tightened from 80 → 75 — daily RSI 80+ entries have poor follow-through
        "MAX_RSI":            75,
    },
}

# =====================================================================================
# ADX MINIMUM THRESHOLD
#
# FIX 5: Raised from 22 → 25.
#
# The hard disqualifier in scoring_engine.py previously blocked ADX < 22.
# ADX 22–24 is still a weak/establishing trend — not strong enough for a reliable
# breakout. Stocks in this band were firing alerts but lacking directional follow-through.
#
# ADX interpretation:
#   < 20:  No trend (ranging/choppy) — hard disqualify
#   20–24: Weak trend beginning — now also disqualified
#   25–34: Established trend — minimum acceptable
#   35+:   Strong trend — ideal breakout territory
#
# Used in: scoring_engine.check_hard_disqualifiers()
# =====================================================================================

ADX_MIN_THRESHOLD = 25   # RAISED from 22 → 25

# =====================================================================================
# DELIVERY CONVICTION THRESHOLDS
# =====================================================================================

DELIVERY_CONVICTION_THRESHOLDS = {
    "institutional": 60,
    "positional":    40,
    "moderate":      25,
    "intraday_churn": 0,
}

# =====================================================================================
# BATCH DOWNLOAD SETTINGS
# =====================================================================================

BATCH_DOWNLOAD_SIZE = 30
YAHOO_TIMEOUT = 30

# =====================================================================================
# ALERT DEDUPLICATION
# =====================================================================================

DEDUP_DAYS = 7

# =====================================================================================
# TELEGRAM SETTINGS
# =====================================================================================

TELEGRAM_CHUNK_SIZE = 10
TELEGRAM_RETRIES = 3
TELEGRAM_TIMEOUT = 10

# =====================================================================================
# LOGGING
# =====================================================================================

LOG_LEVEL = "INFO"

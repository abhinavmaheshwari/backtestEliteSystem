# =====================================================================================
# app/config.py
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

# =====================================================================================
# TELEGRAM GROUP TOPIC THREAD IDs
# =====================================================================================
# How to get these:
#   1. Enable Topics in your group (Group Settings → Topics → Enable)
#   2. Create topics: e.g. "⚡ Intraday", "🚀 1H Scan", "📊 EOD Alerts"
#   3. Send any message inside a topic, then open:
#      https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
#   4. Find "message_thread_id" in the response — copy the number for each topic
#   5. Set them as environment variables (same as BOT_TOKEN / CHAT_ID)
#
# If not set, messages fall back to General (no topic) — nothing breaks.
# =====================================================================================
_thread_eod      = os.getenv("THREAD_EOD")
_thread_intraday = os.getenv("THREAD_INTRADAY")
_thread_1h       = os.getenv("THREAD_1H")

THREAD_EOD      = int(_thread_eod)      if _thread_eod      else None
THREAD_INTRADAY = int(_thread_intraday) if _thread_intraday else None
THREAD_1H       = int(_thread_1h)       if _thread_1h       else None

# =====================================================================================
# DATA DIRECTORY
# =====================================================================================
DATA_DIR = os.path.join(
    BASE_DIR,
    "data"
)
os.makedirs(
    DATA_DIR,
    exist_ok=True
)

# =====================================================================================
# FILE PATHS
# =====================================================================================
WATCHLIST_PATH = os.path.join(
    DATA_DIR,
    "elite_fundamental_watchlist.parquet"
)
DB_PATH = os.path.join(
    DATA_DIR,
    "alerts.db"
)

# =====================================================================================
# BREAKOUT SETTINGS
# =====================================================================================
BREAKOUT_SCORE_THRESHOLD = 70

# =====================================================================================
# SCAN SETTINGS
# =====================================================================================
MIN_VOLUME_RATIO = 1.5
MIN_RSI          = 55
MAX_RSI          = 85

# =====================================================================================
# app/telegram_engine.py
# =====================================================================================
#
# HOW TO SET UP GROUP TOPICS
# =====================================================================================
#
#  Step 1 — Add bot to your Telegram group
#            Group → Edit → Administrators → Add Admin → search your bot
#
#  Step 2 — Enable Topics
#            Group Settings → Topics → Enable
#
#  Step 3 — Create three topics inside the group:
#            e.g. "⚡ Intraday", "🚀 1H Scan", "📊 EOD Alerts"
#
#  Step 4 — Get each topic's message_thread_id:
#            Send any message inside the topic, then open:
#            https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
#            Look for "message_thread_id" in the response — one per topic.
#
#  Step 5 — Add to config.py:
#            THREAD_EOD      = 123    # replace with real IDs
#            THREAD_INTRADAY = 456
#            THREAD_1H       = 789
#
#  If THREAD_* values are not set in config.py, messages go to General (no topic).
#
# =====================================================================================

import logging
import time
import requests

from config import BOT_TOKEN, CHAT_ID

logger = logging.getLogger(__name__)

# =====================================================================================
# OPTIONAL THREAD IDs — loaded from config if present
# =====================================================================================

try:
    from config import THREAD_EOD
except ImportError:
    THREAD_EOD = None

try:
    from config import THREAD_INTRADAY
except ImportError:
    THREAD_INTRADAY = None

try:
    from config import THREAD_1H
except ImportError:
    THREAD_1H = None

# =====================================================================================
# THREAD ROUTING — scan_type → message_thread_id
# =====================================================================================

THREAD_MAP = {
    "EOD":      THREAD_EOD,
    "INTRADAY": THREAD_INTRADAY,
    "1H":       THREAD_1H,
}

# =====================================================================================
# SEND
# =====================================================================================

def send_telegram_message(message: str, scan_type: str = None, retries: int = 3) -> bool:
    """
    Sends a message to the configured Telegram group.

    Parameters
    ----------
    message   : str  — alert text (HTML tags supported: <b>, <i>, <code>, <pre>)
    scan_type : str  — "EOD" | "INTRADAY" | "1H"
                       Routes to the matching group topic if THREAD_* is set in config.
                       Pass None to post to General.
    retries   : int  — retry attempts on failure (default 3)

    Returns True on success, False after all retries exhausted.
    """

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id":    CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",   # safer than Markdown — no escaping issues
    }

    thread_id = THREAD_MAP.get(scan_type) if scan_type else None
    if thread_id:
        payload["message_thread_id"] = thread_id

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                logger.info(f"📨 Sent | scan={scan_type} | thread={thread_id}")
                return True

            # Telegram rate limit — respect retry_after
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"⏳ Rate limited — waiting {retry_after}s (attempt {attempt}/{retries})")
                time.sleep(retry_after)
                continue

            logger.error(f"❌ Telegram {response.status_code}: {response.text}")

        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Timeout (attempt {attempt}/{retries})")
        except Exception as e:
            logger.exception(f"❌ Telegram exception: {e}")

        if attempt < retries:
            time.sleep(2 * attempt)  # back-off: 2s, 4s

    logger.error(f"❌ Failed after {retries} attempts | scan={scan_type}")
    return False

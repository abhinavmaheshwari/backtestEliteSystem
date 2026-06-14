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
import os
from data_fetch_status import mark_success, mark_failure

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

try:
    from config import THREAD_REVERSAL
except ImportError:
    THREAD_REVERSAL = None

# =====================================================================================
# THREAD ROUTING — scan_type → message_thread_id
# =====================================================================================

THREAD_MAP = {
    "EOD":      THREAD_EOD,
    "INTRADAY": THREAD_INTRADAY,
    "1H":       THREAD_1H,
    "REVERSAL": THREAD_REVERSAL,
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
    
    # Cloud-safe fallback override
    active_token = os.getenv("BOT_TOKEN", BOT_TOKEN)
    active_chat  = os.getenv("CHAT_ID", CHAT_ID)

    if not active_token or not active_chat:
        logger.warning("⚠️ Telegram skipped: BOT_TOKEN or CHAT_ID is missing.")
        return False

    url = f"https://api.telegram.org/bot{active_token}/sendMessage"

    payload = {
        "chat_id":                  active_chat,
        "text":                     message,
        "parse_mode":               "HTML",   # safer than Markdown — no escaping issues
        "disable_web_page_preview": True,     # Prevents ugly link bubbles in chat
    }

    thread_id = THREAD_MAP.get(scan_type) if scan_type else None
    if thread_id:
        payload["message_thread_id"] = thread_id

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                logger.info(f"📨 Sent | scan={scan_type} | thread={thread_id}")
                try:
                    mark_success('telegram')
                except Exception:
                    logger.exception('Failed to report telegram success')
                return True

            # Telegram rate limit — respect retry_after
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"⏳ Rate limited — waiting {retry_after}s (attempt {attempt}/{retries})")
                time.sleep(retry_after)
                continue

            # Thread not found — the topic was deleted or the thread_id is wrong.
            # Fall back to General (remove message_thread_id and retry immediately)
            # so alerts are never silently lost due to a misconfigured topic ID.
            if response.status_code == 400:
                error_body = response.json()
                description = error_body.get("description", "")
                if "message thread not found" in description and "message_thread_id" in payload:
                    logger.warning(
                        f"⚠️ Thread {thread_id} not found for scan={scan_type} — "
                        f"falling back to General chat"
                    )
                    payload.pop("message_thread_id")
                    thread_id = None
                    continue  # retry immediately without thread_id

            logger.error(f"❌ Telegram {response.status_code}: {response.text}")

        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Timeout (attempt {attempt}/{retries})")
        except Exception as e:
            logger.exception("❌ Telegram exception (unexpected)")
            try:
                mark_failure('telegram', e)
            except Exception:
                logger.exception('Failed to report telegram exception')

        if attempt < retries:
            time.sleep(2 * attempt)  # back-off: 2s, 4s

    logger.error(f"❌ Failed after {retries} attempts | scan={scan_type}")
    try:
        mark_failure('telegram', f'Failed after {retries} attempts')
    except Exception:
        logger.exception('Failed to report telegram final failure')
    return False

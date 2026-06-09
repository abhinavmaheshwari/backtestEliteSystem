# =====================================================================================
# app/telegram_engine.py (RATE-LIMIT GUARDIAN EDITION)
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

from config import BOT_TOKEN, CHAT_ID, THREAD_EOD, THREAD_INTRADAY, THREAD_1H

logger = logging.getLogger(__name__)

def get_thread_id(scan_type: str) -> int | None:
    if scan_type == "EOD":
        return THREAD_EOD
    elif scan_type == "INTRADAY":
        return THREAD_INTRADAY
    elif scan_type == "1H":
        return THREAD_1H
    return None

def send_telegram_message(message: str, scan_type: str = "GENERAL", retries: int = 3) -> bool:
    """
    Dispatches a message to the specified Telegram group/topic.
    Includes smart rate-limit handling (429) to prevent bot bans.
    """
    # Environment variables override config.py imports to ensure cloud safety
    bot_token = os.getenv("BOT_TOKEN", BOT_TOKEN)
    chat_id   = os.getenv("CHAT_ID", CHAT_ID)

    if not bot_token or not chat_id:
        logger.warning("⚠️ Telegram skipped: BOT_TOKEN or CHAT_ID missing.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    thread_id = get_thread_id(scan_type)

    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if thread_id:
        payload["message_thread_id"] = thread_id

    attempt = 0
    while attempt < retries:
        attempt += 1
        try:
            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                return True

            # ── SMART RATE LIMIT HANDLING (429) ──
            if response.status_code == 429:
                error_body = response.json()
                retry_after = error_body.get("parameters", {}).get("retry_after", 5)
                logger.warning(f"⏳ Telegram Rate Limited (429) — pausing thread for {retry_after}s (attempt {attempt}/{retries})...")
                time.sleep(retry_after)
                continue # Retry without incrementing attempt counter heavily

            # ── TOPIC DELETION FALLBACK ──
            # Thread not found — the topic was deleted or the thread_id is wrong.
            # Fall back to General (remove message_thread_id and retry immediately)
            # so alerts are never silently lost due to a misconfigured topic ID.
            if response.status_code == 400:
                error_body = response.json()
                description = error_body.get("description", "")
                if "message thread not found" in description and "message_thread_id" in payload:
                    logger.warning(f"⚠️ Topic {thread_id} missing for {scan_type}. Falling back to main group.")
                    payload.pop("message_thread_id")
                    thread_id = None
                    continue # Retry immediately in general chat

            logger.error(f"❌ Telegram API {response.status_code}: {response.text}")

        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Telegram request timed out (attempt {attempt}/{retries})")
        except Exception as e:
            logger.exception(f"❌ Telegram unknown exception: {e}")

        # Exponential backoff for generic errors
        if attempt < retries:
            time.sleep(2 ** attempt)

    logger.error(f"❌ Telegram dispatch permanently failed after {retries} attempts.")
    return False

# app/dispatch_engine.py
import logging
import os
import requests
from email_engine import send_html_email

logger = logging.getLogger(__name__)

def dispatch_report(subject: str, html_content: str, attachment_path: str, fallback_caption: str):
    """Unified handler: Attempt Email first, then fallback to Telegram."""
    email_success = send_html_email(subject, html_content, attachment_path=attachment_path)
    
    if not email_success:
        logger.warning("⚠️ Email delivery failed. Activating Telegram Fallback...")
        bot_token = os.getenv("BOT_TOKEN")
        chat_id   = os.getenv("CHAT_ID")
        
        if bot_token and chat_id and os.path.exists(attachment_path):
            url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
            with open(attachment_path, 'rb') as doc:
                resp = requests.post(
                    url, 
                    data={'chat_id': chat_id, 'caption': fallback_caption, 'parse_mode': 'Markdown'}, 
                    files={'document': doc}, 
                    timeout=15
                )
                if resp.status_code == 200:
                    logger.info("✅ Report successfully delivered to Telegram.")
                else:
                    logger.error(f"❌ Telegram fallback failed: {resp.text}")

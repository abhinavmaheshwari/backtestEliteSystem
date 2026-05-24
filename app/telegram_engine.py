# =====================================================================================
# app/telegram_engine.py
# =====================================================================================

import requests

from config import BOT_TOKEN, CHAT_ID

# =====================================================================================
# SEND TELEGRAM MESSAGE
# =====================================================================================

def send_telegram_message(message):

    try:

        url = (

            f"https://api.telegram.org/"
            f"bot{BOT_TOKEN}/sendMessage"
        )

        payload = {

            "chat_id": CHAT_ID,

            "text": message
        }

        requests.post(

            url,

            json=payload,

            timeout=10
        )

    except Exception as e:

        print(f"❌ TELEGRAM ERROR -> {e}")

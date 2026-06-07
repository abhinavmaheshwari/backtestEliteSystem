# =====================================================================================
# app/email_engine.py
# DYNAMIC ENVIRONMENT CONTROLLED EMAIL PIPELINE
# =====================================================================================

import smtplib
from email.message import EmailMessage
import logging
import os

logger = logging.getLogger(__name__)

def send_html_email(subject: str, html_content: str) -> bool:
    """
    Assembles and pushes custom metrics to your personal mailbox using 
    Railway injected container context parameters.
    """
    sender_email    = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    receiver_email  = os.getenv("RECEIVER_EMAIL")

    if not sender_email or not sender_password or not receiver_email:
        logger.warning("⚠️ Email processing bypassed. Missing config environment parameters.")
        return False

    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From']    = sender_email
        msg['To']      = receiver_email

        msg.set_content("Please enable HTML viewing features to safely render today's metrics table.")
        msg.add_alternative(html_content, subtype='html')

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
            
        logger.info("📧 Daily Consolidated Summary pushed to system mailbox destination.")
        return True

    except Exception as e:
        logger.error(f"❌ Critical exception encountered during email generation workflow: {e}")
        return False

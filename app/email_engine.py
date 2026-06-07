# =====================================================================================
# app/email_engine.py
# CLOUD-OPTIMIZED EMAIL PIPELINE (IPv4 Forced + TLS + Attachments)
# =====================================================================================

import smtplib
import socket
from email.message import EmailMessage
import mimetypes
import logging
import os

logger = logging.getLogger(__name__)

# ============================================================================
# 🚨 CRITICAL CLOUD FIX: Force IPv4 Resolution
# ============================================================================
original_getaddrinfo = socket.getaddrinfo

def ipv4_getaddrinfo(*args, **kwargs):
    responses = original_getaddrinfo(*args, **kwargs)
    return [res for res in responses if res[0] == socket.AF_INET]

socket.getaddrinfo = ipv4_getaddrinfo
# ============================================================================

def send_html_email(subject: str, html_content: str, attachment_path: str = None) -> bool:
    """
    Attempts to send an HTML email with an optional file attachment EXACTLY ONCE.
    Returns True if successful, False if blocked/failed.
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

        # Attach the CSV file if provided
        if attachment_path and os.path.exists(attachment_path):
            ctype, encoding = mimetypes.guess_type(attachment_path)
            if ctype is None or encoding is not None:
                ctype = 'application/octet-stream'
            maintype, subtype = ctype.split('/', 1)
            
            with open(attachment_path, 'rb') as f:
                msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(attachment_path))

        logger.info("🔌 Connecting to SMTP server over IPv4 (Port 587)...")
        
        # EXACTLY ONE ATTEMPT with a 15-second timeout
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
            
        logger.info("📧 Email successfully sent.")
        return True

    except TimeoutError:
        logger.error("❌ Email connection timed out after 15 seconds. (Firewall block)")
        return False
    except Exception as e:
        logger.error(f"❌ Email generation failed: {e}")
        return False

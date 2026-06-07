# =====================================================================================
# app/email_engine.py
# CLOUD-OPTIMIZED EMAIL PIPELINE (IPv4 Forced + TLS)
# =====================================================================================

import smtplib
import socket
from email.message import EmailMessage
import logging
import os

logger = logging.getLogger(__name__)

# ============================================================================
# 🚨 CRITICAL CLOUD FIX: Force IPv4 Resolution
# Railway and other cloud providers often have broken IPv6 routing for Google.
# This patch forces Python's socket library to only use IPv4 for the connection,
# which bypasses the [Errno 101] Network is unreachable error.
# ============================================================================
original_getaddrinfo = socket.getaddrinfo

def ipv4_getaddrinfo(*args, **kwargs):
    responses = original_getaddrinfo(*args, **kwargs)
    # Filter out IPv6 (AF_INET6), keep only IPv4 (AF_INET)
    return [res for res in responses if res[0] == socket.AF_INET]

# Apply the patch
socket.getaddrinfo = ipv4_getaddrinfo
# ============================================================================

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

        logger.info("🔌 Connecting to SMTP server over IPv4 (Port 587)...")
        
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()  # Secure the connection
            smtp.ehlo()
            
            logger.info("🔐 Authenticating...")
            smtp.login(sender_email, sender_password)
            
            logger.info("📤 Dispatching payload...")
            smtp.send_message(msg)
            
        logger.info("✅ Daily Consolidated Summary pushed to system mailbox destination.")
        return True

    except TimeoutError:
        logger.error("❌ Email connection timed out after 15 seconds.")
        return False
    except smtplib.SMTPAuthenticationError:
        logger.error("❌ Email Auth Failed. Check your SENDER_PASSWORD (16-char App Password).")
        return False
    except Exception as e:
        logger.error(f"❌ Critical exception encountered during email generation workflow: {e}")
        return False

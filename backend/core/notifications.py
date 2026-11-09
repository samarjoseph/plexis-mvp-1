"""
Plexis Founder Notification Service.

Sends instant email alerts via SMTP whenever an account upgrades or interacts
with key monetization milestones.
"""
from __future__ import annotations

import logging
import smtplib
import threading
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)

# IST timezone: UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))


def _send_email_thread(
    recipient: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
) -> bool:
    """Internal synchronous email dispatcher via SMTP."""
    smtp_host = Config.SMTP_HOST
    smtp_port = Config.SMTP_PORT
    smtp_user = Config.SMTP_USER or Config.ADMIN_NOTIFICATION_EMAIL
    smtp_pass = Config.SMTP_PASSWORD

    if not smtp_pass:
        logger.warning(
            "[NOTIFICATIONS] SMTP_PASSWORD is not configured in environment. "
            "Skipping email delivery to %s for subject '%s'. Set SMTP_PASSWORD in Render/.env to enable live emails.",
            recipient,
            subject,
        )
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Plexis Alerts <{smtp_user}>"
        msg["To"] = recipient

        part1 = MIMEText(body_text, "plain")
        msg.attach(part1)

        if body_html:
            part2 = MIMEText(body_html, "html")
            msg.attach(part2)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())

        logger.info("[NOTIFICATIONS] Successfully sent email to %s (subject: %s)", recipient, subject)
        return True
    except Exception as e:
        logger.error("[NOTIFICATIONS] Failed to dispatch email via SMTP: %s", e, exc_info=True)
        return False


def dispatch_founder_email_async(
    recipient: Optional[str],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
) -> None:
    """Dispatches email in a background daemon thread so HTTP response returns instantly."""
    target_email = (recipient or Config.ADMIN_NOTIFICATION_EMAIL or "samaryadavclass2b@gmail.com").strip()
    thread = threading.Thread(
        target=_send_email_thread,
        args=(target_email, subject, body_text, body_html),
        daemon=True,
    )
    thread.start()


def notify_plus_upgrade(
    account_name: str,
    email: str,
    account_id: str,
    timestamp_str: Optional[str] = None,
    action_type: str = "Upgrade to Plus (Free Launch Access)",
) -> None:
    """
    Format and send instant upgrade notification to founder.
    Format:
    account name , email name , account id and timestamp{xx:xx}
    """
    if not timestamp_str:
        now_ist = datetime.now(IST)
        timestamp_str = now_ist.strftime("%H:%M IST (%d-%b-%Y)")

    clean_name = (account_name or "").strip() or email.split("@")[0] or "User"
    subject = f"🚀 New Plus Upgrade: {clean_name} ({email})"

    body_text = (
        f"An account just upgraded to Plexis Plus (Free Launch Access)!\n\n"
        f"• Account Name: {clean_name}\n"
        f"• Email Name: {email}\n"
        f"• Account ID: {account_id}\n"
        f"• Timestamp: {timestamp_str}\n"
        f"• Action: {action_type}\n\n"
        f"— Plexis Telemetry System"
    )

    body_html = f"""
    <html>
      <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0b0f17; color: #f1f5f9; padding: 24px;">
        <div style="max-width: 560px; margin: 0 auto; background: #131b2e; border: 1px solid #1e293b; border-radius: 12px; padding: 28px; box-shadow: 0 10px 25px rgba(0,0,0,0.5);">
          <div style="font-size: 22px; font-weight: 700; color: #38bdf8; margin-bottom: 12px;">
            🚀 New Plus Upgrade Event!
          </div>
          <p style="color: #94a3b8; font-size: 14px; margin-bottom: 20px;">
            A user just unlocked <strong>Plexis Plus</strong> via Free Launch Access:
          </p>
          <table style="width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 24px;">
            <tr style="border-bottom: 1px solid #1e293b;">
              <td style="padding: 10px 0; color: #64748b; font-weight: 600; width: 140px;">Account Name:</td>
              <td style="padding: 10px 0; color: #f8fafc; font-weight: 600;">{clean_name}</td>
            </tr>
            <tr style="border-bottom: 1px solid #1e293b;">
              <td style="padding: 10px 0; color: #64748b; font-weight: 600;">Email Name:</td>
              <td style="padding: 10px 0; color: #38bdf8; font-weight: 600;">{email}</td>
            </tr>
            <tr style="border-bottom: 1px solid #1e293b;">
              <td style="padding: 10px 0; color: #64748b; font-weight: 600;">Account ID:</td>
              <td style="padding: 10px 0; color: #cbd5e1; font-family: monospace;">{account_id}</td>
            </tr>
            <tr>
              <td style="padding: 10px 0; color: #64748b; font-weight: 600;">Timestamp:</td>
              <td style="padding: 10px 0; color: #10b981; font-weight: 600;">{timestamp_str}</td>
            </tr>
          </table>
          <div style="padding-top: 16px; border-top: 1px solid #1e293b; font-size: 12px; color: #64748b;">
            Plexis Automated Analytics & Telemetry System
          </div>
        </div>
      </body>
    </html>
    """

    dispatch_founder_email_async(
        recipient=Config.ADMIN_NOTIFICATION_EMAIL,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )

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


def _send_via_resend(
    api_key: str,
    from_email: str,
    recipient: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    attachments: Optional[list] = None,
) -> bool:
    """Dispatches email via Resend HTTPS REST API (Port 443 — works 100% on Render)."""
    try:
        import base64
        import requests

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        sender = from_email or "Plexis Alerts <onboarding@resend.dev>"
        payload = {
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "text": body_text,
        }
        if body_html:
            payload["html"] = body_html

        if attachments:
            resend_att = []
            for att in attachments:
                fname = att.get("filename", "attachment.bin")
                raw_bytes = att.get("content_bytes", b"")
                b64 = (
                    base64.b64encode(raw_bytes).decode("utf-8")
                    if isinstance(raw_bytes, (bytes, bytearray))
                    else str(raw_bytes)
                )
                resend_att.append({
                    "filename": fname,
                    "content": b64,
                })
            payload["attachments"] = resend_att

        resp = requests.post("https://api.resend.com/emails", headers=headers, json=payload, timeout=12)
        if 200 <= resp.status_code < 300:
            logger.info("[NOTIFICATIONS] Successfully sent email via Resend API to %s (subject: '%s')", recipient, subject)
            return True
        else:
            logger.error("[NOTIFICATIONS] Resend API error (status %d): %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logger.error("[NOTIFICATIONS] Failed to dispatch email via Resend API: %s", e, exc_info=True)
        return False


def _send_via_brevo(
    api_key: str,
    recipient: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    attachments: Optional[list] = None,
) -> bool:
    """Dispatches email via Brevo HTTPS REST API (Port 443 — works 100% on Render)."""
    try:
        import base64
        import requests

        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }
        sender_email = Config.ADMIN_NOTIFICATION_EMAIL or "samaryadavclass2b@gmail.com"
        payload = {
            "sender": {"name": "Plexis Alerts", "email": sender_email},
            "to": [{"email": recipient}],
            "subject": subject,
            "textContent": body_text,
        }
        if body_html:
            payload["htmlContent"] = body_html

        if attachments:
            brevo_att = []
            for att in attachments:
                fname = att.get("filename", "attachment.bin")
                raw_bytes = att.get("content_bytes", b"")
                b64 = (
                    base64.b64encode(raw_bytes).decode("utf-8")
                    if isinstance(raw_bytes, (bytes, bytearray))
                    else str(raw_bytes)
                )
                brevo_att.append({
                    "name": fname,
                    "content": b64,
                })
            payload["attachment"] = brevo_att

        resp = requests.post("https://api.brevo.com/v3/smtp/email", headers=headers, json=payload, timeout=12)
        if 200 <= resp.status_code < 300:
            logger.info("[NOTIFICATIONS] Successfully sent email via Brevo API to %s (subject: '%s')", recipient, subject)
            return True
        else:
            logger.error("[NOTIFICATIONS] Brevo API error (status %d): %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logger.error("[NOTIFICATIONS] Failed to dispatch email via Brevo API: %s", e, exc_info=True)
        return False


def _send_via_discord(
    webhook_url: str,
    title: str,
    description: str,
    color: int = 0x2563EB,
) -> bool:
    """Dispatches instant notification to a Discord channel webhook (Port 443)."""
    if not webhook_url:
        return False
    try:
        import requests
        payload = {
            "embeds": [
                {
                    "title": title[:256],
                    "description": description[:2000],
                    "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": {"text": "Plexis Telemetry System"},
                }
            ]
        }
        requests.post(webhook_url, json=payload, timeout=6)
        logger.info("[NOTIFICATIONS] Successfully dispatched alert to Discord Webhook")
        return True
    except Exception as e:
        logger.warning("[NOTIFICATIONS] Discord webhook dispatch failed: %s", e)
        return False


def _send_email_thread(
    recipient: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    attachments: Optional[list] = None,
) -> bool:
    """
    Internal synchronous email dispatcher with multiple transports:
    1. Resend HTTPS API (Recommended for Render — port 443 is never blocked)
    2. Brevo HTTPS API (Alternative HTTPS provider)
    3. Direct SMTP (Fallback for localhost or servers with unblocked ports 25/587)
    """
    # Forward to Discord Webhook if configured
    if Config.DISCORD_WEBHOOK_URL:
        _send_via_discord(Config.DISCORD_WEBHOOK_URL, subject, body_text)

    # 1. Prefer Resend API (HTTPS Port 443)
    if Config.RESEND_API_KEY:
        return _send_via_resend(
            api_key=Config.RESEND_API_KEY,
            from_email=Config.RESEND_FROM_EMAIL,
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
        )

    # 2. Prefer Brevo API (HTTPS Port 443)
    if Config.BREVO_API_KEY:
        return _send_via_brevo(
            api_key=Config.BREVO_API_KEY,
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
        )

    # 3. Direct SMTP Fallback
    smtp_host = Config.SMTP_HOST
    smtp_port = Config.SMTP_PORT
    smtp_user = Config.SMTP_USER or Config.ADMIN_NOTIFICATION_EMAIL
    smtp_pass = Config.SMTP_PASSWORD

    if not smtp_pass:
        logger.warning(
            "[NOTIFICATIONS] No email service configured (RESEND_API_KEY, BREVO_API_KEY, or SMTP_PASSWORD). "
            "Skipping notification delivery to %s for subject '%s'. "
            "To receive email alerts on Render (which blocks raw SMTP), add RESEND_API_KEY in Render Environment Variables.",
            recipient,
            subject,
        )
        return False

    try:
        # Create multipart message (mixed if attachments exist, else alternative)
        msg = MIMEMultipart("mixed" if attachments else "alternative")
        msg["Subject"] = subject
        msg["From"] = f"Plexis Alerts <{smtp_user}>"
        msg["To"] = recipient

        # Message body container
        body_container = MIMEMultipart("alternative")
        part1 = MIMEText(body_text, "plain")
        body_container.attach(part1)

        if body_html:
            part2 = MIMEText(body_html, "html")
            body_container.attach(part2)

        msg.attach(body_container)

        # Process attachments (e.g. user issue screenshots)
        if attachments:
            from email.mime.base import MIMEBase
            from email import encoders

            for att in attachments:
                fname = att.get("filename", "attachment.bin")
                content = att.get("content_bytes", b"")
                mpart = MIMEBase("application", "octet-stream")
                mpart.set_payload(content)
                encoders.encode_base64(mpart)
                mpart.add_header("Content-Disposition", f'attachment; filename="{fname}"')
                msg.attach(mpart)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=12) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())

        logger.info("[NOTIFICATIONS] Successfully sent email to %s (subject: %s)", recipient, subject)
        return True
    except OSError as err:
        if getattr(err, "errno", None) == 101 or "Network is unreachable" in str(err):
            logger.error(
                "[NOTIFICATIONS] Render blocked outbound SMTP traffic ([Errno 101] Network is unreachable). "
                "Render Free tier blocks outbound SMTP ports 25, 465, and 587. "
                "Fix: Add RESEND_API_KEY in Render Environment Variables (free at https://resend.com) to send via HTTPS Port 443."
            )
        else:
            logger.error("[NOTIFICATIONS] Failed to dispatch email via SMTP: %s", err, exc_info=True)
        return False
    except Exception as e:
        logger.error("[NOTIFICATIONS] Failed to dispatch email via SMTP: %s", e, exc_info=True)
        return False


def dispatch_founder_email_async(
    recipient: Optional[str],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    attachments: Optional[list] = None,
) -> None:
    """Dispatches email in a background daemon thread so HTTP response returns instantly."""
    target_email = (recipient or Config.ADMIN_NOTIFICATION_EMAIL or "samaryadavclass2b@gmail.com").strip()
    thread = threading.Thread(
        target=_send_email_thread,
        args=(target_email, subject, body_text, body_html, attachments),
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


def send_support_ticket(
    name: str,
    email: str,
    category: str,
    message: str,
    user_id: Optional[str] = None,
    screenshot_base64: Optional[str] = None,
) -> None:
    """
    Format and dispatch a user support/complaint/feedback ticket via SMTP.
    Includes full description, account identity, and attached screenshot.
    """
    now_ist = datetime.now(IST)
    timestamp_str = now_ist.strftime("%H:%M IST (%d-%b-%Y)")

    clean_name = (name or "").strip() or (email.split("@")[0] if "@" in email else "User")
    clean_cat = (category or "Support / Issue").strip()
    clean_email = (email or "No email provided").strip()
    clean_uid = user_id or "Guest / Unauthenticated"

    subject = f"🚨 Plexis Support Ticket: [{clean_cat}] from {clean_name}"

    body_text = (
        f"New Support & Problem Ticket from Plexis User:\n\n"
        f"• Name: {clean_name}\n"
        f"• Email: {clean_email}\n"
        f"• Category: {clean_cat}\n"
        f"• User ID: {clean_uid}\n"
        f"• Timestamp: {timestamp_str}\n\n"
        f"Problem Description & Feedback:\n"
        f"--------------------------------------------------\n"
        f"{message}\n"
        f"--------------------------------------------------\n"
    )

    attachments = []
    has_image = False
    if screenshot_base64 and len(screenshot_base64) > 30:
        try:
            import base64
            raw_b64 = screenshot_base64
            if "," in raw_b64:
                raw_b64 = raw_b64.split(",", 1)[1]
            img_bytes = base64.b64decode(raw_b64)
            attachments.append({
                "filename": f"screenshot_{int(now_ist.timestamp())}.png",
                "content_bytes": img_bytes,
                "mime_type": "image/png",
            })
            has_image = True
            body_text += "\n[Screenshot attached to this email]\n"
        except Exception as b64_err:
            logger.error("[NOTIFICATIONS] Could not decode screenshot attachment: %s", b64_err)

    formatted_msg_html = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    body_html = f"""
    <html>
      <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0b0f17; color: #f1f5f9; padding: 24px;">
        <div style="max-width: 620px; margin: 0 auto; background: #131b2e; border: 1px solid #1e293b; border-radius: 12px; padding: 28px; box-shadow: 0 10px 25px rgba(0,0,0,0.5);">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; border-bottom: 1px solid #1e293b; padding-bottom: 12px;">
            <div style="font-size: 20px; font-weight: 700; color: #f43f5e;">
              🚨 User Complaint & Support Ticket
            </div>
            <span style="background: #1e293b; color: #38bdf8; font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px;">
              {clean_cat}
            </span>
          </div>

          <table style="width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 20px;">
            <tr style="border-bottom: 1px solid #1e293b;">
              <td style="padding: 8px 0; color: #64748b; font-weight: 600; width: 120px;">User Name:</td>
              <td style="padding: 8px 0; color: #f8fafc; font-weight: 600;">{clean_name}</td>
            </tr>
            <tr style="border-bottom: 1px solid #1e293b;">
              <td style="padding: 8px 0; color: #64748b; font-weight: 600;">Email:</td>
              <td style="padding: 8px 0; color: #38bdf8; font-weight: 600;"><a href="mailto:{clean_email}" style="color: #38bdf8; text-decoration: none;">{clean_email}</a></td>
            </tr>
            <tr style="border-bottom: 1px solid #1e293b;">
              <td style="padding: 8px 0; color: #64748b; font-weight: 600;">User ID:</td>
              <td style="padding: 8px 0; color: #94a3b8; font-family: monospace; font-size: 13px;">{clean_uid}</td>
            </tr>
            <tr>
              <td style="padding: 8px 0; color: #64748b; font-weight: 600;">Submitted:</td>
              <td style="padding: 8px 0; color: #10b981; font-weight: 600;">{timestamp_str}</td>
            </tr>
          </table>

          <div style="margin-top: 16px; margin-bottom: 20px;">
            <div style="font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8; margin-bottom: 8px;">
              Problem Description:
            </div>
            <div style="background: #090d16; border: 1px solid #1e293b; border-radius: 8px; padding: 16px; font-size: 14px; line-height: 1.6; color: #e2e8f0; white-space: pre-wrap;">
{formatted_msg_html}
            </div>
          </div>

          {"<div style='background: #1e293b; color: #38bdf8; padding: 10px 14px; border-radius: 6px; font-size: 13px; margin-bottom: 16px;'>📎 <strong>1 Screenshot attached</strong> (see email attachments)</div>" if has_image else ""}

          <div style="padding-top: 16px; border-top: 1px solid #1e293b; font-size: 12px; color: #64748b; display: flex; justify-content: space-between;">
            <span>Plexis Help & Support Telemetry</span>
            <span>Reply directly to this email to contact the user</span>
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
        attachments=attachments,
    )

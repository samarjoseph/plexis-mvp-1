"""
Plexis Help, Support & Feedback API Blueprint.

Receives user issue reports, feedback, and screenshots from the /help center
and dispatches instant notifications to the founder.
"""
from __future__ import annotations

import logging
from flask import Blueprint, jsonify, request

from db.identity import identity_resolver
from core.notifications import send_support_ticket

logger = logging.getLogger(__name__)

help_bp = Blueprint("help", __name__, url_prefix="/api/help")


@help_bp.route("/feedback", methods=["POST", "OPTIONS"])
def submit_feedback():
    """
    Submit a user complaint, issue report, or feedback with optional screenshot.
    """
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({
            "error": "VALIDATION_ERROR",
            "message": "Please enter a description of the problem or feedback."
        }), 400

    category = (data.get("category") or "Bug / Issue").strip()
    screenshot_base64 = data.get("screenshot")

    # Try resolving authenticated identity, or fallback to body inputs
    resolved_email = None
    resolved_name = None
    user_id_str = None

    try:
        current_user = identity_resolver.resolve(request)
        if current_user:
            resolved_email = current_user.email
            user_id_str = str(current_user.user_id)
            if getattr(current_user, "is_guest", False):
                user_id_str = f"Guest ({current_user.guest_id or current_user.session_id})"
    except Exception:
        pass

    email = (data.get("email") or resolved_email or "anonymous@user.plexis").strip()
    name = (data.get("name") or resolved_name or email.split("@")[0] or "User").strip()

    logger.info("[HELP] Received support submission: category=%s email=%s has_screenshot=%s",
                category, email, bool(screenshot_base64))

    try:
        send_support_ticket(
            name=name,
            email=email,
            category=category,
            message=message,
            user_id=user_id_str,
            screenshot_base64=screenshot_base64,
        )
    except Exception as e:
        logger.error("[HELP] Failed to send support ticket email: %s", e, exc_info=True)

    return jsonify({
        "success": True,
        "message": "Thank you! Your message and screenshot have been sent directly to the Plexis team. We will review it promptly."
    }), 200

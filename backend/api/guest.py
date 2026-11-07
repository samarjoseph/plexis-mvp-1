"""
Guest Mode API Blueprint.

Handles:
- POST /api/guest/session: Initialize an isolated guest session and preloaded dataset.
- GET  /api/guest/status: Check guest session validity and remaining transformation quota.
"""
from __future__ import annotations

import logging
import uuid
from flask import Blueprint, jsonify, request

from core.guest import (
    GUEST_MAX_MONTHLY_TRANSFORMATIONS,
    compute_guest_id,
    create_guest_token,
    check_guest_transformation_quota,
    provision_guest_dataset,
    verify_guest_token,
)
from db.identity import identity_resolver
from db.session import get_db

logger = logging.getLogger(__name__)

guest_bp = Blueprint("guest", __name__, url_prefix="/api/guest")


def _get_client_ip() -> str:
    """Extract client IP handling reverse proxy headers."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # First IP in comma-separated list is client IP
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


@guest_bp.route("/session", methods=["POST", "OPTIONS"])
def create_session():
    """Create a new temporary guest session and provision preloaded sample dataset."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    client_seed = data.get("seed")

    client_ip = _get_client_ip()
    user_agent = request.headers.get("User-Agent", "")

    guest_id = compute_guest_id(client_ip, user_agent, client_seed)
    session_id = uuid.uuid4().hex[:16]

    guest_token, expires_at = create_guest_token(session_id, guest_id)

    db = get_db()
    allowed, used, limit = check_guest_transformation_quota(db, guest_id)

    # Provision isolated copy of sample dataset
    try:
        dataset_entry = provision_guest_dataset(session_id)
        df = dataset_entry.dataframe
        dataset_info = {
            "id": dataset_entry.dataset_id,
            "filename": dataset_entry.filename,
            "row_count": dataset_entry.row_count,
            "column_count": dataset_entry.column_count,
            "columns": list(df.columns) if df is not None else [],
            "preview_rows": df.head(10).to_dict(orient="records") if df is not None else [],
        }
    except Exception as e:
        logger.error("[GUEST] Error provisioning guest dataset: %s", e)
        dataset_info = {
            "id": f"guest_ds_{session_id}",
            "filename": "sample_customers.csv",
            "row_count": 0,
            "column_count": 0,
            "columns": [],
            "preview_rows": [],
        }

    logger.info("[GUEST] Created guest session id=%s for guest_id=%s (used=%d/%d)", session_id, guest_id, used, limit)

    return jsonify({
        "guest_token": guest_token,
        "session_id": session_id,
        "guest_id": guest_id,
        "expires_at": expires_at,
        "transformations_used": used,
        "transformations_max": limit,
        "transformations_remaining": max(0, limit - used),
        "dataset": dataset_info,
        "is_guest": True,
    }), 200


@guest_bp.route("/status", methods=["GET", "OPTIONS"])
def get_status():
    """Get status and remaining transformation quota for the current guest session."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200

    try:
        current_user = identity_resolver.resolve(request)
    except Exception:
        return jsonify({"is_guest": False, "authenticated": False}), 200

    if not current_user.is_guest:
        return jsonify({
            "is_guest": False,
            "plan": current_user.plan,
            "email": current_user.email,
        }), 200

    db = get_db()
    allowed, used, limit = check_guest_transformation_quota(db, current_user.guest_id or "")

    return jsonify({
        "is_guest": True,
        "session_id": current_user.session_id,
        "guest_id": current_user.guest_id,
        "transformations_used": used,
        "transformations_max": limit,
        "transformations_remaining": max(0, limit - used),
        "quota_exhausted": not allowed,
    }), 200

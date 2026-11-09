"""
User Plan and Subscription API.

Endpoints:
    GET  /api/user/plan  — Return current user subscription plan, usage, and limits
    POST /api/user/plan  — Upgrade or switch plan ('free' or 'plus')
"""
from __future__ import annotations

import logging
from flask import Blueprint, jsonify, request

from db.session import get_db
from db.identity import identity_resolver, IdentityError
from config import Config
from core.plans import (
    PLAN_FREE,
    PLAN_PLUS,
    PLAN_LIMITS,
    normalize_plan,
    get_plan_limits,
    check_daily_transformation_quota,
    check_chat_creation_quota,
)
from db.repositories.user_repository import user_repository

logger = logging.getLogger(__name__)

user_plan_bp = Blueprint("user_plan", __name__, url_prefix="/api/user")


def _resolve_identity():
    """Resolve current user from request identity."""
    try:
        current_user = identity_resolver.resolve(request)
        return current_user, None
    except IdentityError as e:
        return None, (jsonify({"error": str(e)}), 401)
    except Exception as e:
        logger.error("[USER_PLAN] Identity resolution failed: %s", e)
        return None, (jsonify({"error": "Identity resolution failed"}), 500)


@user_plan_bp.route("/plan", methods=["GET"])
def get_user_plan():
    """Return user's current subscription plan, usage metrics, and tier limits."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    user_id = current_user.user_id

    db_user = None
    if db is not None:
        db_user = user_repository.get_by_id(db, user_id)

    plan = normalize_plan(getattr(db_user, "plan", getattr(current_user, "plan", PLAN_FREE)))

    # Self-healing: if plan is free but user has an active Stripe subscription, restore Plus
    if plan != PLAN_PLUS and Config.STRIPE_SECRET_KEY:
        try:
            import stripe
            stripe.api_key = Config.STRIPE_SECRET_KEY
            cust_id = getattr(db_user, "stripe_customer_id", None) if db_user else None
            subs = []
            if cust_id:
                subs = stripe.Subscription.list(customer=cust_id, status="active", limit=1).data
            if not subs:
                customers = stripe.Customer.list(email=current_user.email, limit=3).data
                for c in customers:
                    s_list = stripe.Subscription.list(customer=c.id, status="active", limit=1).data
                    if s_list:
                        subs = s_list
                        cust_id = c.id
                        break
            if subs:
                active_sub = subs[0]
                plan = PLAN_PLUS
                if db_user and db is not None:
                    db_user.plan = PLAN_PLUS
                    db_user.stripe_customer_id = cust_id
                    db_user.stripe_subscription_id = active_sub.id
                    db.commit()
                logger.info(
                    "[USER_PLAN] Restored Plus plan for user %s (%s) from active Stripe subscription %s",
                    user_id,
                    current_user.email,
                    active_sub.id,
                )
        except Exception as sub_err:
            logger.warning("[USER_PLAN] Stripe active subscription verification error: %s", sub_err)

    limits = get_plan_limits(plan)

    # Calculate real-time usage stats
    _, daily_transforms_used, daily_transforms_limit = check_daily_transformation_quota(
        db, user_id, plan
    )
    _, chats_count, chats_limit = check_chat_creation_quota(db, user_id, plan)

    total_messages_count = 0
    if db is not None:
        try:
            from db.models.message import Message
            from db.models.chat import Chat
            from sqlalchemy import func

            total_messages_count = (
                db.query(func.count(Message.id))
                .join(Chat, Chat.id == Message.chat_id)
                .filter(Chat.user_id == user_id)
                .scalar()
            ) or 0
        except Exception as e:
            logger.warning("[USER_PLAN] Could not calculate total_messages: %s", e)

    return jsonify({
        "success": True,
        "plan": plan,
        "email": current_user.email,
        "usage": {
            "daily_transformations_used": daily_transforms_used,
            "daily_transformations_limit": daily_transforms_limit,
            "chats_count": chats_count,
            "chats_limit": chats_limit,
            "total_messages_count": total_messages_count,
            "total_messages_limit": limits["max_messages_total"],
        },
        "limits": limits,
        "all_plans": PLAN_LIMITS,
    }), 200


@user_plan_bp.route("/plan", methods=["POST"])
def update_user_plan():
    """Update or switch user's subscription plan ('free' or 'plus')."""
    current_user, err = _resolve_identity()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    requested_plan = data.get("plan", "").strip().lower()

    if requested_plan not in (PLAN_FREE, PLAN_PLUS):
        return jsonify({
            "error": "Invalid plan",
            "message": f"Plan must be one of: '{PLAN_FREE}', '{PLAN_PLUS}'"
        }), 400

    db = get_db()
    account_name = None
    if db is not None:
        user = user_repository.update_plan(db, current_user.user_id, requested_plan)
        if not user:
            return jsonify({"error": "User not found"}), 404
        account_name = getattr(user, "name", None) or getattr(user, "display_name", None)

    if not account_name:
        account_name = current_user.email.split("@")[0]

    if requested_plan == PLAN_PLUS:
        try:
            from core.notifications import notify_plus_upgrade
            notify_plus_upgrade(
                account_name=account_name,
                email=current_user.email,
                account_id=str(current_user.user_id),
                action_type="Plan Switched to Plus",
            )
        except Exception as n_err:
            logger.error("[USER_PLAN] Failed to dispatch upgrade notification: %s", n_err)

    logger.info("[USER_PLAN] User %s switched to plan: %s", current_user.email, requested_plan)
    limits = get_plan_limits(requested_plan)

    return jsonify({
        "success": True,
        "message": f"Successfully switched to {requested_plan.capitalize()} plan",
        "plan": requested_plan,
        "limits": limits,
    }), 200

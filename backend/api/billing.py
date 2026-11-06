"""
Stripe Billing and Subscription API.

Provides endpoints for:
- Creating Stripe Checkout Sessions for the Plexis Plus Plan ($30/month)
- Verifying completed Checkout Sessions upon return to /billing
- Creating Stripe Customer Portal Sessions for managing cards/invoices/cancellations
- Handling Stripe Webhooks for subscription state synchronization
"""
from __future__ import annotations

import logging
from typing import Optional
from flask import Blueprint, jsonify, request
import stripe

from config import Config
from db.session import get_db
from db.identity import identity_resolver, IdentityError
from db.repositories.user_repository import user_repository

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing", __name__, url_prefix="/api/billing")


def _resolve_identity():
    """Resolve authenticated user identity from request headers."""
    try:
        current_user = identity_resolver.resolve(request)
        return current_user, None
    except IdentityError as e:
        return None, (jsonify({"error": str(e)}), 401)
    except Exception as e:
        logger.error("[BILLING] Identity resolution failed: %s", e)
        return None, (jsonify({"error": "Identity resolution failed"}), 500)


def _init_stripe() -> bool:
    """Ensure Stripe API key is set."""
    if not Config.STRIPE_SECRET_KEY:
        logger.error("[BILLING] STRIPE_SECRET_KEY is not configured")
        return False
    stripe.api_key = Config.STRIPE_SECRET_KEY
    return True


@billing_bp.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Create a Stripe Checkout Session for upgrading to Plus ($30/month)."""
    current_user, err = _resolve_identity()
    if err:
        return err

    if not _init_stripe():
        return jsonify({"error": "Stripe is not configured on the server."}), 503

    try:
        # Determine origin / return URLs
        origin = (request.headers.get("Origin") or Config.FRONTEND_URL).rstrip("/")
        success_url = f"{origin}/billing?session_id={{CHECKOUT_SESSION_ID}}&success=true"
        cancel_url = f"{origin}/billing?canceled=true"

        # Look for existing Stripe customer ID on user if recorded
        db = get_db()
        customer_id = None
        if db is not None:
            db_user = user_repository.get_by_id(db, current_user.user_id)
            if db_user and getattr(db_user, "stripe_customer_id", None):
                customer_id = db_user.stripe_customer_id

        session_kwargs = {
            "mode": "subscription",
            "client_reference_id": str(current_user.user_id),
            "line_items": [
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": "Plexis Plus Plan",
                            "description": "Unlimited rows & chats, 50MB uploads, persistent transformations, VIP priority inference",
                            "tax_code": "txcd_10000000",
                        },
                        "unit_amount": Config.PLUS_PRICE_CENTS,  # 3000 = $30.00
                        "recurring": {"interval": "month"},
                    },
                    "quantity": 1,
                }
            ],
            "metadata": {
                "user_id": str(current_user.user_id),
                "email": current_user.email,
                "plan": "plus",
            },
            "success_url": success_url,
            "cancel_url": cancel_url,
        }

        try:
            if customer_id:
                session_kwargs["customer"] = customer_id
            else:
                session_kwargs["customer_email"] = current_user.email
            session = stripe.checkout.Session.create(**session_kwargs)
        except stripe.InvalidRequestError as inv_err:
            if "No such customer" in str(inv_err) and customer_id:
                logger.warning("[BILLING] Customer ID %s not found in Stripe, retrying with email", customer_id)
                session_kwargs.pop("customer", None)
                session_kwargs["customer_email"] = current_user.email
                session = stripe.checkout.Session.create(**session_kwargs)
            else:
                raise inv_err

        logger.info(
            "[BILLING] Created Stripe Checkout Session %s for user %s (%s)",
            session.id,
            current_user.user_id,
            current_user.email,
        )

        return jsonify({
            "success": True,
            "checkout_url": session.url,
            "session_id": session.id,
        }), 200

    except stripe.StripeError as e:
        logger.error("[BILLING] Stripe checkout error: %s", e)
        return jsonify({"error": str(e.user_message or e)}), 400
    except Exception as e:
        logger.exception("[BILLING] Unexpected error creating checkout session: %s", e)
        return jsonify({"error": "Failed to create checkout session. Please try again."}), 500


@billing_bp.route("/verify-session/<session_id>", methods=["GET"])
def verify_session(session_id: str):
    """
    Verify a completed Checkout Session upon return to /billing.
    Instantly upgrades the user to Plus if payment is completed.
    """
    current_user, err = _resolve_identity()
    if err:
        return err

    if not _init_stripe():
        return jsonify({"error": "Stripe is not configured."}), 503

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        is_paid = (
            session.payment_status in ("paid", "no_payment_required")
            or getattr(session, "status", None) == "complete"
        )

        if not is_paid:
            return jsonify({
                "success": False,
                "verified": False,
                "status": session.payment_status,
                "message": "Payment has not completed yet.",
            }), 400

        # Verify user matches
        session_user_id = session.metadata.get("user_id") or session.client_reference_id
        session_email = session.customer_email or session.metadata.get("email")

        # Upgrade user in DB
        db = get_db()
        if db is not None:
            updated_user = user_repository.update_plan(db, current_user.user_id, "plus")
            if updated_user:
                if hasattr(updated_user, "stripe_customer_id") and session.customer:
                    updated_user.stripe_customer_id = session.customer
                if hasattr(updated_user, "stripe_subscription_id") and session.subscription:
                    updated_user.stripe_subscription_id = session.subscription
                db.commit()

        logger.info(
            "[BILLING] Successfully verified session %s. Upgraded user %s to Plus.",
            session_id,
            current_user.user_id,
        )

        return jsonify({
            "success": True,
            "verified": True,
            "plan": "plus",
            "session_id": session.id,
            "customer_id": session.customer,
            "subscription_id": session.subscription,
            "message": "Payment verified! Your account is now upgraded to Plexis Plus.",
        }), 200

    except stripe.StripeError as e:
        logger.error("[BILLING] Stripe session retrieval error: %s", e)
        return jsonify({"error": str(e.user_message or e)}), 400
    except Exception as e:
        logger.exception("[BILLING] Error verifying session %s: %s", session_id, e)
        return jsonify({"error": "Could not verify session."}), 500


@billing_bp.route("/create-portal-session", methods=["POST"])
def create_portal_session():
    """Create a Stripe Customer Portal session for managing subscription and billing info."""
    current_user, err = _resolve_identity()
    if err:
        return err

    if not _init_stripe():
        return jsonify({"error": "Stripe is not configured."}), 503

    try:
        origin = (request.headers.get("Origin") or Config.FRONTEND_URL).rstrip("/")
        db = get_db()
        customer_id = None

        if db is not None:
            db_user = user_repository.get_by_id(db, current_user.user_id)
            if db_user and getattr(db_user, "stripe_customer_id", None):
                customer_id = db_user.stripe_customer_id

        # If not recorded in DB, search Stripe by email
        if not customer_id:
            customers = stripe.Customer.list(email=current_user.email, limit=1)
            if customers and len(customers.data) > 0:
                customer_id = customers.data[0].id

        if not customer_id:
            return jsonify({
                "error": "No Stripe customer found for this account. Please subscribe first."
            }), 404

        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{origin}/billing",
        )

        return jsonify({
            "success": True,
            "portal_url": portal_session.url,
        }), 200

    except stripe.StripeError as e:
        logger.error("[BILLING] Stripe portal error: %s", e)
        return jsonify({"error": str(e.user_message or e)}), 400
    except Exception as e:
        logger.exception("[BILLING] Error creating portal session: %s", e)
        return jsonify({"error": "Failed to create portal session."}), 500


@billing_bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    """Handle Stripe asynchronous webhook events."""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    event = None
    if Config.STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, Config.STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            logger.error("[BILLING_WEBHOOK] Invalid payload: %s", e)
            return jsonify({"error": "Invalid payload"}), 400
        except stripe.SignatureVerificationError as e:
            logger.error("[BILLING_WEBHOOK] Invalid signature: %s", e)
            return jsonify({"error": "Invalid signature"}), 400
    else:
        # Fallback in dev when webhook secret is not yet configured
        try:
            import json
            data = json.loads(payload.decode("utf-8"))
            event = stripe.Event.construct_from(data, stripe.api_key)
        except Exception as e:
            logger.error("[BILLING_WEBHOOK] Event parse error: %s", e)
            return jsonify({"error": "Could not parse event"}), 400

    event_type = event.get("type", "")
    event_data = event.get("data", {}).get("object", {})

    logger.info("[BILLING_WEBHOOK] Received event: %s (id=%s)", event_type, event.get("id"))

    db = get_db()
    if db is None:
        return jsonify({"received": True}), 200

    if event_type in ("checkout.session.completed", "invoice.payment_succeeded"):
        # Payment succeeded - activate Plus
        client_ref = event_data.get("client_reference_id")
        metadata = event_data.get("metadata", {}) or {}
        user_id_str = metadata.get("user_id") or client_ref
        customer_email = event_data.get("customer_email") or metadata.get("email")

        try:
            target_user = None
            if user_id_str:
                import uuid
                target_user = user_repository.get_by_id(db, uuid.UUID(user_id_str))
            elif customer_email:
                target_user = user_repository.get_or_create_by_email(db, customer_email)

            if target_user:
                target_user.plan = "plus"
                customer_id = event_data.get("customer")
                subscription_id = event_data.get("subscription")
                if customer_id and hasattr(target_user, "stripe_customer_id"):
                    target_user.stripe_customer_id = customer_id
                if subscription_id and hasattr(target_user, "stripe_subscription_id"):
                    target_user.stripe_subscription_id = subscription_id
                db.commit()
                logger.info("[BILLING_WEBHOOK] User %s upgraded to plus via webhook", target_user.id)
        except Exception as e:
            logger.error("[BILLING_WEBHOOK] Error handling %s: %s", event_type, e)

    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        # Subscription cancelled or paused - downgrade to Free
        customer_id = event_data.get("customer")
        try:
            from db.models.user import User
            users = db.query(User).filter(
                (User.stripe_customer_id == customer_id) if hasattr(User, "stripe_customer_id") else False
            ).all()
            for u in users:
                u.plan = "free"
                logger.info("[BILLING_WEBHOOK] User %s downgraded to free after subscription deletion", u.id)
            db.commit()
        except Exception as e:
            logger.error("[BILLING_WEBHOOK] Error handling subscription cancellation: %s", e)

    return jsonify({"received": True}), 200

"""
Identity resolution layer — DEVELOPMENT ONLY.

Resolves the current user from the X-User-Email request header.

WARNING: This is NOT production-secure authentication.
         It is designed for development multi-user isolation only.
         Replace with JWT/session-cookie resolver before production deployment.

Future replacement:
    - Implement JWTIdentityResolver(IdentityResolver)
    - Register it instead of HeaderIdentityResolver in identity_resolver singleton
    - Repositories/services require no changes — they work with CurrentUser only
"""
import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from flask import Request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CurrentUser — the resolved identity for a single request
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CurrentUser:
    """
    Resolved identity for the current request.
    Populated by IdentityResolver and passed to services/repositories.
    Routes never read request headers directly for user identity.
    """
    user_id: UUID
    email: str
    plan: str = "free"
    is_guest: bool = False
    guest_id: Optional[str] = None
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# IdentityError — raised when identity cannot be resolved
# ---------------------------------------------------------------------------

class IdentityError(Exception):
    """Raised when the request cannot be associated with a valid user."""
    pass


# ---------------------------------------------------------------------------
# Base resolver interface
# ---------------------------------------------------------------------------

class IdentityResolver:
    """Base interface for identity resolution strategies."""

    def resolve(self, request: Request) -> CurrentUser:
        """
        Resolve the current user from the request.
        Returns CurrentUser if successful.
        Raises IdentityError if the request cannot be authenticated.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# DEV ONLY: Header-based identity resolver
# ---------------------------------------------------------------------------

class HeaderIdentityResolver(IdentityResolver):
    """
    DEV ONLY: Resolves identity from X-User-Email header.

    This is NOT production-secure. Anyone can set any email.
    Designed for development and local multi-user testing only.
    Replace with JWTIdentityResolver for production.
    """

    HEADER_NAME = "X-User-Email"
    DEFAULT_DEV_EMAIL = "dev@plexis.local"  # fallback for local solo dev

    def resolve(self, request: Request) -> CurrentUser:
        """
        Resolve user from request.
        First checks for guest session token (X-Guest-Token or Authorization: Bearer gst...).
        If not a guest, falls back to X-User-Email header.
        Creates user in DB if not exists (upsert by normalized email).
        """
        # 1. Check for guest token header first (backend-authoritative guest session)
        guest_token = (request.headers.get("X-Guest-Token") or "").strip()
        if not guest_token:
            auth_header = (request.headers.get("Authorization") or "").strip()
            if auth_header.lower().startswith("bearer gst."):
                guest_token = auth_header[7:].strip()
            elif auth_header.startswith("gst."):
                guest_token = auth_header

        if guest_token:
            from core.guest import verify_guest_token
            session_data = verify_guest_token(guest_token)
            if not session_data:
                logger.warning("[IDENTITY] Invalid or expired guest token provided")
                raise IdentityError("Invalid or expired guest session. Please refresh or sign in.")

            import uuid
            session_id = session_data["session_id"]
            guest_id = session_data["guest_id"]
            guest_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"guest:{session_id}")
            logger.debug("[IDENTITY] Resolved guest identity session=%s guest_id=%s", session_id, guest_id)
            return CurrentUser(
                user_id=guest_uuid,
                email=f"guest_{session_id[:8]}@guest.plexis.local",
                plan="guest",
                is_guest=True,
                guest_id=guest_id,
                session_id=session_id,
            )

        # Import here to avoid circular imports at module load
        from db.session import get_db
        from db.repositories.user_repository import user_repository

        email = (request.headers.get(self.HEADER_NAME) or "").strip().lower()
        if not email:
            # In dev mode, fall back to default dev identity rather than failing
            email = self.DEFAULT_DEV_EMAIL
            logger.debug("[IDENTITY] No %s header; using dev fallback: %s", self.HEADER_NAME, email)
        else:
            logger.debug("[IDENTITY] Resolved identity from header: %s", email)

        header_plan = (request.headers.get("X-User-Plan") or "").strip().lower()

        try:
            db = get_db()
            if db is None:
                import uuid
                dev_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, email)
                plan = header_plan if header_plan in ("free", "plus") else "free"
                return CurrentUser(user_id=dev_uuid, email=email, plan=plan)
            try:
                user = user_repository.get_or_create_by_email(db, email)
            except Exception as query_err:
                db.rollback()
                err_str = str(query_err).lower()
                if "column" in err_str and "plan" in err_str:
                    logger.warning("[IDENTITY] Missing plan column detected in users table. Auto-migrating on the fly...")
                    from sqlalchemy import text
                    from db.engine import engine
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR(50) DEFAULT 'free' NOT NULL;"))
                        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255);"))
                        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255);"))
                    user = user_repository.get_or_create_by_email(db, email)
                else:
                    raise query_err

            # User plan in DB is authoritative; auto-sync to Plus if client header has Plus
            user_plan = getattr(user, "plan", "free") or "free"
            if header_plan == "plus" and user_plan != "plus":
                try:
                    user_repository.update_plan(db, user.id, "plus")
                    user_plan = "plus"
                    logger.info("[IDENTITY] Auto-synced user %s to Plus plan from client header", user.id)
                except Exception as sync_err:
                    logger.warning("[IDENTITY] Failed to auto-sync user plan: %s", sync_err)
            return CurrentUser(user_id=user.id, email=user.email_normalized, plan=user_plan)
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.error("[IDENTITY] Failed to resolve user for email=%s: %s", email, e)
            raise IdentityError(f"Could not resolve user identity: {e}") from e


# ---------------------------------------------------------------------------
# Singleton — import and use this everywhere
# ---------------------------------------------------------------------------

# DEV ONLY: swap this for JWTIdentityResolver in production
identity_resolver: IdentityResolver = HeaderIdentityResolver()

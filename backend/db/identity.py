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
        Resolve user from X-User-Email header.
        Creates user in DB if not exists (upsert by normalized email).
        """
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
            user = user_repository.get_or_create_by_email(db, email)
            # Sync plan if client explicitly specified in dev header, otherwise use DB
            if header_plan in ("free", "plus") and getattr(user, "plan", None) != header_plan:
                user.plan = header_plan
                db.flush()
            user_plan = getattr(user, "plan", "free") or "free"
            db.commit()
            return CurrentUser(user_id=user.id, email=user.email_normalized, plan=user_plan)
        except Exception as e:
            logger.error("[IDENTITY] Failed to resolve user for email=%s: %s", email, e)
            raise IdentityError(f"Could not resolve user identity: {e}") from e


# ---------------------------------------------------------------------------
# Singleton — import and use this everywhere
# ---------------------------------------------------------------------------

# DEV ONLY: swap this for JWTIdentityResolver in production
identity_resolver: IdentityResolver = HeaderIdentityResolver()

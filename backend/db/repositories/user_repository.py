"""User repository — all DB queries for the User model."""
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from db.models.user import User

logger = logging.getLogger(__name__)


class UserRepository:
    """
    All DB access for User model.
    No route or service should call SQLAlchemy directly for User queries.
    """

    def get_or_create_by_email(self, db: Session, email: str) -> User:
        """
        Normalize email, fetch existing User, or create a new one.
        Used by IdentityResolver on every request.

        Args:
            db: SQLAlchemy session
            email: Raw email string (will be normalized to lowercase, stripped)

        Returns:
            Existing or newly created User
        """
        normalized = email.strip().lower()

        user = db.query(User).filter(User.email_normalized == normalized).first()
        if user:
            return user

        # Create new user
        user = User(
            email=email.strip(),
            email_normalized=normalized,
            display_name=normalized.split("@")[0],
        )
        db.add(user)
        db.flush()  # Get the id without committing
        logger.info("[USER_REPO] Created new user: %s (id=%s)", normalized, user.id)
        return user

    def get_by_id(self, db: Session, user_id: UUID) -> Optional[User]:
        """
        Fetch User by UUID primary key.

        Args:
            db: SQLAlchemy session
            user_id: User UUID

        Returns:
            User or None
        """
        return db.query(User).filter(User.id == user_id).first()

    def update_last_seen(self, db: Session, user_id: UUID) -> None:
        """Update last_seen_at timestamp for a user."""
        from sqlalchemy.sql import func
        db.query(User).filter(User.id == user_id).update(
            {"last_seen_at": func.now()},
            synchronize_session=False,
        )

    def update_plan(self, db: Session, user_id: UUID, plan: str) -> Optional[User]:
        """Update subscription plan for a user ('free' or 'plus')."""
        normalized_plan = str(plan).strip().lower()
        if normalized_plan not in ("free", "plus"):
            normalized_plan = "free"
        user = self.get_by_id(db, user_id)
        if user:
            user.plan = normalized_plan
            db.commit()
            logger.info("[USER_REPO] Updated user %s plan to %s", user_id, normalized_plan)
        return user


user_repository = UserRepository()

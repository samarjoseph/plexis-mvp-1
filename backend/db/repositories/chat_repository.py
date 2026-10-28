"""Chat repository — all DB queries for the Chat model."""
import logging
import uuid
import re
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from db.models.chat import Chat
from db.isolation import compute_account_tag, compute_chat_tag, generate_chat_slug

logger = logging.getLogger(__name__)


def _generate_slug(title: str, uid: str) -> str:
    """
    Generate a URL-safe slug from a chat title + unique suffix.

    Examples:
        "My Analysis" → "my-analysis-a3f2"
        "New Chat"    → "new-chat-b9c1"
    """
    # Normalize title
    slug_base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug_base = slug_base[:40] or "chat"
    suffix = uid[:4]
    return f"{slug_base}-{suffix}"


class ChatRepository:
    """
    All DB access for Chat model.
    Ownership is verified on every resource-fetch method.
    """

    def ensure_unique_title(
        self,
        db: Session,
        user_id: UUID,
        desired_title: str,
        exclude_chat_id: Optional[UUID] = None,
    ) -> str:
        """
        Ensure chat title is unique within the user's account without mechanical numbering.
        Uses natural semantic variations/modifiers (e.g., 'Overview', 'Review', 'Insights').
        """
        base = (desired_title or "New Chat").strip()
        query = db.query(Chat.title).filter(
            Chat.user_id == user_id,
            Chat.archived_at.is_(None),
        )
        if exclude_chat_id:
            query = query.filter(Chat.id != exclude_chat_id)
        existing = {row[0].strip().lower() for row in query.all()}

        if base.lower() not in existing:
            return base

        modifiers = [
            "Overview", "Review", "Insights", "Deep Dive", "Exploration",
            "Summary", "Discussion", "Exchange", "Perspective", "Notes"
        ]
        for mod in modifiers:
            candidate = f"{base} {mod}"
            if candidate.lower() not in existing:
                return candidate

        for mod in modifiers:
            words = base.split()
            if len(words) > 1:
                candidate = f"{words[0]} {mod}"
                if candidate.lower() not in existing:
                    return candidate

        return f"{base} Exploration"

    def create_chat(
        self,
        db: Session,
        user_id: UUID,
        title: str = "New Chat",
        current_dataset_id: Optional[UUID] = None,
    ) -> Chat:
        """
        Create a new Chat for a user with isolated slug and unique title.

        Args:
            db: SQLAlchemy session
            user_id: Owner's UUID
            title: Chat display title
            current_dataset_id: Initially active dataset (optional)

        Returns:
            Newly created Chat
        """
        unique_title = self.ensure_unique_title(db, user_id=user_id, desired_title=title)
        new_id = uuid.uuid4()
        slug = generate_chat_slug(user_id, new_id)

        # Ensure slug uniqueness (retry with fallback suffix on rare collision)
        attempts = 0
        while db.query(Chat.id).filter(Chat.slug == slug).first() is not None:
            slug = f"{slug[:-2]}{uuid.uuid4().hex[:2]}"
            attempts += 1
            if attempts > 10:
                break

        chat = Chat(
            id=new_id,
            user_id=user_id,
            title=unique_title,
            slug=slug,
            current_dataset_id=current_dataset_id,
        )
        db.add(chat)
        db.flush()
        logger.info("[CHAT_REPO] Created chat slug=%s user=%s title=%s", slug, user_id, unique_title)
        return chat

    def get_by_slug(self, db: Session, slug: str, user_id: UUID) -> Optional[Chat]:
        """
        Fetch a Chat by its slug or isolated tag, verifying ownership.

        Returns None if not found OR if owned by a different user.
        """
        chat = (
            db.query(Chat)
            .filter(Chat.slug == slug, Chat.user_id == user_id)
            .first()
        )
        if chat:
            return chat

        # Fallback 1: check if slug is formatted as accountTag-chatTag
        if "-" in slug:
            parts = slug.split("-", 1)
            chat_tag_target = parts[1]
            user_chats = db.query(Chat).filter(Chat.user_id == user_id).all()
            for c in user_chats:
                if compute_chat_tag(c.id) == chat_tag_target:
                    return c

        # Fallback 2: check if slug matches chat_tag directly
        user_chats = db.query(Chat).filter(Chat.user_id == user_id).all()
        for c in user_chats:
            if compute_chat_tag(c.id) == slug:
                return c

        return None

    def get_by_id(self, db: Session, chat_id: UUID, user_id: UUID) -> Optional[Chat]:
        """
        Fetch a Chat by UUID, verifying ownership.

        Returns None if not found OR if owned by a different user.
        """
        return (
            db.query(Chat)
            .filter(Chat.id == chat_id, Chat.user_id == user_id)
            .first()
        )

    def get_by_id_unsafe(self, db: Session, chat_id: UUID) -> Optional[Chat]:
        """
        Fetch a Chat by UUID WITHOUT ownership check.

        INTERNAL USE ONLY — only for internal pipeline resolution where
        ownership was already verified upstream. Never expose to routes.
        """
        return db.query(Chat).filter(Chat.id == chat_id).first()

    def list_for_user(self, db: Session, user_id: UUID) -> List[Chat]:
        """List all non-archived chats for a user, ordered by last_message_at desc."""
        return (
            db.query(Chat)
            .filter(Chat.user_id == user_id, Chat.archived_at.is_(None))
            .order_by(Chat.last_message_at.desc().nullslast(), Chat.created_at.desc())
            .all()
        )

    def update_current_dataset(
        self,
        db: Session,
        chat_id: UUID,
        dataset_id: UUID,
        user_id: UUID,
    ) -> bool:
        """
        Update the currently active dataset for a chat.
        Verifies ownership.

        Returns True if updated, False if chat not found/not owned.
        """
        rows = (
            db.query(Chat)
            .filter(Chat.id == chat_id, Chat.user_id == user_id)
            .update({"current_dataset_id": dataset_id}, synchronize_session=False)
        )
        return rows > 0

    def update_title(
        self,
        db: Session,
        chat_id: UUID,
        title: str,
        user_id: UUID,
    ) -> bool:
        """Update title of a chat. Verifies ownership and enforces title uniqueness per user."""
        unique_title = self.ensure_unique_title(db, user_id=user_id, desired_title=title, exclude_chat_id=chat_id)
        rows = (
            db.query(Chat)
            .filter(Chat.id == chat_id, Chat.user_id == user_id)
            .update({"title": unique_title}, synchronize_session=False)
        )
        return rows > 0

    def update_last_message(self, db: Session, chat_id: UUID) -> None:
        """Update last_message_at to now for a chat."""
        from sqlalchemy.sql import func
        db.query(Chat).filter(Chat.id == chat_id).update(
            {"last_message_at": func.now()},
            synchronize_session=False,
        )

    def archive(self, db: Session, chat_id: UUID, user_id: UUID) -> bool:
        """Soft-delete (archive) a chat. Returns True if archived."""
        from sqlalchemy.sql import func
        rows = (
            db.query(Chat)
            .filter(Chat.id == chat_id, Chat.user_id == user_id)
            .update({"archived_at": func.now()}, synchronize_session=False)
        )
        return rows > 0

    def delete(self, db: Session, chat_id: UUID, user_id: UUID) -> bool:
        """Hard delete a chat (cascades to messages, sessions, memories). Returns True if deleted."""
        chat = self.get_by_id(db, chat_id, user_id)
        if not chat:
            return False
        db.delete(chat)
        return True


chat_repository = ChatRepository()

"""Chat repository — all DB queries for the Chat model."""
import logging
import uuid
import re
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from db.models.chat import Chat

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

    def create_chat(
        self,
        db: Session,
        user_id: UUID,
        title: str = "New Chat",
        current_dataset_id: Optional[UUID] = None,
    ) -> Chat:
        """
        Create a new Chat for a user.

        Args:
            db: SQLAlchemy session
            user_id: Owner's UUID
            title: Chat display title
            current_dataset_id: Initially active dataset (optional)

        Returns:
            Newly created Chat
        """
        uid = uuid.uuid4().hex
        slug = _generate_slug(title, uid)

        # Ensure slug uniqueness (retry with longer suffix on collision)
        attempts = 0
        while db.query(Chat).filter(Chat.slug == slug).first() is not None:
            slug = f"{slug}-{uuid.uuid4().hex[:4]}"
            attempts += 1
            if attempts > 10:
                break

        chat = Chat(
            id=uuid.uuid4(),
            user_id=user_id,
            title=title,
            slug=slug,
            current_dataset_id=current_dataset_id,
        )
        db.add(chat)
        db.flush()
        logger.info("[CHAT_REPO] Created chat slug=%s user=%s", slug, user_id)
        return chat

    def get_by_slug(self, db: Session, slug: str, user_id: UUID) -> Optional[Chat]:
        """
        Fetch a Chat by its slug, verifying ownership.

        Returns None if not found OR if owned by a different user.
        """
        return (
            db.query(Chat)
            .filter(Chat.slug == slug, Chat.user_id == user_id)
            .first()
        )

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
        """Update title of a chat. Verifies ownership."""
        rows = (
            db.query(Chat)
            .filter(Chat.id == chat_id, Chat.user_id == user_id)
            .update({"title": title}, synchronize_session=False)
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

"""
Conversation memory for multi-turn interactions.

Phase 8 update: L1/L2 cache pattern.
  L1 = in-memory _history dict (fast, lost on restart)
  L2 = PostgreSQL messages table (persistent, reconstructed on miss)

get_history() checks L1 first, falls back to PostgreSQL if chat_id is known.
append() writes to L1 AND PostgreSQL (if chat_id is known).

Thread safety:
  All mutations to _history and _session_to_chat are protected by threading.Lock.
  Flask may run multiple threads concurrently — without locking, concurrent appends
  could corrupt the message list via race conditions.
"""
import logging
import threading
from typing import Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_MAX_MESSAGES = 50


@dataclass
class ConvMessage:
    """A single conversation message. Named ConvMessage to avoid shadowing db.models.Message."""
    role: str  # 'user' or 'assistant'
    content: str
    timestamp: Optional[str] = None


class ConversationMemory:
    """
    Manages conversation history per session.

    session_id → list of ConvMessages (L1 in-memory cache)
    chat_id mapping → used for DB fallback (L2)

    Thread-safe: all mutations protected by self._lock.
    """

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES) -> None:
        self._history: Dict[str, List[ConvMessage]] = {}  # session_id → messages
        self._session_to_chat: Dict[str, str] = {}        # session_id → chat_id (for DB fallback)
        self._max_messages = max_messages
        self._lock = threading.Lock()

    def register_chat_id(self, session_id: str, chat_id: str) -> None:
        """
        Register the DB chat_id for a session to enable DB fallback.
        This MUST be called by api/chat.py after resolving chat_id so that
        get_history() can reconstruct from PostgreSQL on L1 cache miss.
        """
        with self._lock:
            self._session_to_chat[session_id] = chat_id
            logger.debug("[CONV_MEMORY] Registered chat_id=%s for session=%s", chat_id, session_id)

    def append(self, session_id: str, role: str, content: str, timestamp: Optional[str] = None, dataset_id: Optional[str] = None) -> None:
        session_id = f"{session_id}::{dataset_id}" if dataset_id else session_id
        """Append a message to session history (L1 only — thread-safe)."""
        with self._lock:
            if session_id not in self._history:
                self._history[session_id] = []

            msg = ConvMessage(role=role, content=content, timestamp=timestamp)
            self._history[session_id].append(msg)

            # Enforce max depth
            if len(self._history[session_id]) > self._max_messages:
                self._history[session_id] = self._history[session_id][-self._max_messages:]

    def get_history(
        self,
        session_id: str,
        last_n: Optional[int] = None,
        dataset_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        client_messages: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        orig_session_id = session_id
        session_key = f"{session_id}::{dataset_id}" if dataset_id else session_id

        # 1. Tier 1: Client-provided messages (highest freshness across distributed cloud workers)
        if client_messages and isinstance(client_messages, list) and len(client_messages) > 0:
            with self._lock:
                self._history[session_key] = [
                    ConvMessage(role=m.get('role', 'user'), content=str(m.get('content', '')))
                    for m in client_messages[-self._max_messages:]
                ]
                if chat_id:
                    self._session_to_chat[session_id] = str(chat_id)
            messages = client_messages[-last_n:] if last_n else client_messages
            return [{'role': m['role'], 'content': m['content']} for m in messages]

        # 2. Tier 2: In-memory L1 cache
        with self._lock:
            messages = list(self._history.get(session_key, []))
            if not messages and dataset_id:
                messages = list(self._history.get(session_id, []))
            resolved_chat = chat_id or self._session_to_chat.get(session_id) or self._session_to_chat.get(session_key)

        # 3. Tier 3: L2 DB fallback from PostgreSQL if cache is empty
        if not messages and resolved_chat:
            messages = self._load_from_db(session_key, resolved_chat, last_n or self._max_messages, dataset_id)

        if last_n is not None:
            messages = messages[-last_n:]
        return [{'role': m.role, 'content': m.content} for m in messages]

    def _load_from_db(self, session_id: str, chat_id: str, last_n: int, dataset_id: Optional[str] = None) -> List[ConvMessage]:
        """Load messages from PostgreSQL and populate L1 cache."""
        try:
            from config import Config
            if not Config.DATABASE_URL:
                return []

            from db.session import db_session
            from db.repositories.message_repository import message_repository
            from db.models.chat import Chat
            import uuid as _uuid

            chat_uuid = None
            try:
                chat_uuid = _uuid.UUID(str(chat_id).strip())
            except (ValueError, AttributeError):
                chat_uuid = None

            with db_session() as db:
                if not chat_uuid:
                    # Resolve slug or isolated tag to chat UUID
                    raw_chat_str = str(chat_id).strip()
                    chat_obj = db.query(Chat).filter(Chat.slug == raw_chat_str).first()
                    if not chat_obj and "-" in raw_chat_str:
                        chat_tag = raw_chat_str.split("-")[-1]
                        from db.isolation import compute_chat_tag
                        all_chats = db.query(Chat).all()
                        for c in all_chats:
                            if compute_chat_tag(c.id) == chat_tag:
                                chat_obj = c
                                break
                    if chat_obj:
                        chat_uuid = chat_obj.id

                if not chat_uuid:
                    return []

                db_msgs = message_repository.get_recent_simple(
                    db, chat_uuid, last_n=last_n
                )

            messages = [
                ConvMessage(
                    role=m.role,
                    content=m.content,
                    timestamp=m.created_at.isoformat() if m.created_at else None,
                )
                for m in db_msgs
                if m.role in ('user', 'assistant') and (not dataset_id or (m.metadata_json or {}).get('dataset_id') in (None, dataset_id))
            ]

            # Populate L1 cache with DB data (thread-safe write)
            if messages:
                with self._lock:
                    self._history[session_id] = messages
                logger.info(
                    "[CONV_MEMORY] Reconstructed %d messages from DB for session=%s chat=%s",
                    len(messages), session_id, chat_id,
                )

            return messages
        except Exception as e:
            logger.warning("[CONV_MEMORY] DB fallback failed for session=%s: %s", session_id, e)
            return []

    def clear(self, session_id: str) -> None:
        """Clear history for a session (thread-safe)."""
        with self._lock:
            self._history.pop(session_id, None)
            self._session_to_chat.pop(session_id, None)

    def message_count(self, session_id: str) -> int:
        """Get message count for a session (thread-safe)."""
        with self._lock:
            return len(self._history.get(session_id, []))


conversation_memory = ConversationMemory()

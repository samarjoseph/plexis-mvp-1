"""Session management for multi-turn conversations."""
import uuid
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict

logger = logging.getLogger(__name__)

@dataclass
class Session:
    """Represents a user session."""
    session_id: str
    user_id: Optional[str] = None  # Future: auth integration
    active_dataset_id: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_active: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    message_count: int = 0

class SessionManager:
    """In-memory session manager (designed for PostgreSQL migration later)."""
    
    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}
    
    def create_session(self, user_id: Optional[str] = None, session_id: Optional[str] = None) -> Session:
        """Create a new session."""
        sid = session_id or str(uuid.uuid4())
        session = Session(session_id=sid, user_id=user_id)
        self._sessions[sid] = session
        logger.info(f"Created session {sid}")
        return session
    
    def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve an existing session."""
        session = self._sessions.get(session_id)
        if session:
            session.last_active = datetime.now(timezone.utc).isoformat()
        return session
    
    def get_or_create(self, session_id: Optional[str] = None) -> Session:
        """Get existing session or create a new one."""
        if session_id:
            session = self.get_session(session_id)
            if session:
                return session
            return self.create_session(session_id=session_id)
        return self.create_session()
    
    def update_dataset(self, session_id: str, dataset_id: str) -> None:
        """Update the active dataset for a session."""
        session = self._sessions.get(session_id)
        if session:
            session.active_dataset_id = dataset_id
    
    def increment_message_count(self, session_id: str) -> None:
        """Increment message count for a session."""
        session = self._sessions.get(session_id)
        if session:
            session.message_count += 1
    
    def all_sessions(self) -> list:
        return list(self._sessions.values())

session_manager = SessionManager()

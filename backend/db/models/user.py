"""User ORM model."""
import uuid
from sqlalchemy import Column, String, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(320), nullable=False)
    email_normalized = Column(String(320), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=True)
    plan = Column(String(50), default="free", nullable=False)  # 'free' or 'plus'
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    chats = relationship("Chat", back_populates="user", cascade="all, delete-orphan")
    datasets = relationship("Dataset", back_populates="user", cascade="all, delete-orphan")
    user_memories = relationship("UserMemory", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email_normalized}>"

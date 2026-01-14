from typing import Optional, List, Dict, Any
from datetime import datetime
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, JSON, BigInteger


# --- 1. The User ---
class User(SQLModel, table=True):
    __tablename__ = "users"

    # Telegram IDs can exceed the standard 32-bit Integer limit
    user_id: int = Field(primary_key=True, index=True, sa_type=BigInteger)
    username: Optional[str] = None
    full_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_active_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    messages: List["Message"] = Relationship(back_populates="user")
    uploads: List["Upload"] = Relationship(back_populates="user")


# --- 2. The Message (Conversation History) ---
class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Link to User with BigInteger support
    user_id: int = Field(
        foreign_key="users.user_id",
        index=True,
        sa_type=BigInteger,
        ondelete="CASCADE"
    )

    role: str = Field(description="user | assistant")
    content: str = Field(description="The text content")

    # Observability
    trace_id: Optional[str] = Field(default=None, index=True)

    # Metadata for storing extra details (tokens, model name, etc.)
    meta: Dict[str, Any] = Field(default={}, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Back-link to User
    user: Optional[User] = Relationship(back_populates="messages")


# --- 3. The Upload (Knowledge Base tracking) ---
class Upload(SQLModel, table=True):
    __tablename__ = "uploads"

    id: Optional[int] = Field(default=None, primary_key=True)

    # FIX: Added user_id to resolve the AttributeError in worker.py
    user_id: int = Field(
        foreign_key="users.user_id",
        index=True,
        sa_type=BigInteger,
        ondelete="CASCADE"
    )

    filename: str
    file_id: Optional[str] = None  # Telegram's unique file identifier
    qdrant_collection: str = Field(default="knowledge_base")
    chunk_count: int = 0

    uploaded_at: datetime = Field(default_factory=datetime.utcnow)

    # Back-link to User
    user: Optional[User] = Relationship(back_populates="uploads")
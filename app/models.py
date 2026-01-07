from typing import Optional, List, Dict, Any
from datetime import datetime
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, JSON, BigInteger


# --- 1. The User ---
class User(SQLModel, table=True):
    __tablename__ = "users"

    user_id: int = Field(primary_key=True, index=True, sa_type=BigInteger)
    username: Optional[str] = None
    full_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_active_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationship to messages
    messages: List["Message"] = Relationship(back_populates="user")


# --- 2. The Message ---
class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: Optional[int] = Field(default=None, primary_key=True)

    # FIX: Use sa_type=BigInteger here too
    user_id: int = Field(foreign_key="users.user_id", index=True, sa_type=BigInteger)

    role: str = Field(description="user | assistant")
    content: str = Field(description="The text content")

    # Observability
    trace_id: Optional[str] = Field(default=None, index=True)

    # Metadata
    meta: Dict[str, Any] = Field(default={}, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Back-link to User
    user: Optional[User] = Relationship(back_populates="messages")


# --- 3. The Upload (Audit Log) ---
class Upload(SQLModel, table=True):
    __tablename__ = "uploads"

    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    qdrant_collection: str
    chunk_count: int
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
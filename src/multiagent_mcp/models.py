"""Data models for multiagent-mcp."""

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from pydantic import BaseModel, Field, field_validator


def normalize_handle(handle: str) -> str:
    """Normalize a handle to canonical '@Handle' format."""
    clean = handle.strip()
    if not clean.startswith("@"):
        clean = f"@{clean}"
    return clean


class Participant(BaseModel):
    """Participant in a multi-agent room."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    handle: str
    status: str = "active"
    last_read_seq_id: int = 0
    joined_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("handle", mode="before")
    @classmethod
    def validate_handle(cls, value: str) -> str:
        return normalize_handle(value)


class Message(BaseModel):
    """Message sent in a room."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    seq_id: int
    sender: str
    recipients: list[str] = Field(default_factory=list)
    content: str
    is_private: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("sender", mode="before")
    @classmethod
    def validate_sender(cls, value: str) -> str:
        return normalize_handle(value)

    @field_validator("recipients", mode="before")
    @classmethod
    def validate_recipients(cls, values: list[str]) -> list[str]:
        return [normalize_handle(v) for v in values]


class TurnResult(BaseModel):
    """Turn status and unread messages for a participant."""

    status: str
    active_turn: Optional[str] = None
    new_messages: list[Message] = Field(default_factory=list)
    current_queue: list[str] = Field(default_factory=list)
    active_participants: list[str] = Field(default_factory=list)
    system_notice: Optional[str] = None

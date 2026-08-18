"""Data models for multiagent-mcp."""

from datetime import datetime, timezone
from typing import Any, Optional, Union
from uuid import uuid4
from pydantic import BaseModel, Field, field_validator


def normalize_handle(handle: str) -> str:
    """Normalize a handle to canonical '@Handle' format."""
    clean = handle.strip()
    if not clean.startswith("@"):
        clean = f"@{clean}"
    return clean


def parse_aliases(raw: Any) -> dict[str, list[str]]:
    """Parse raw aliases input (dict, JSON string, list of strings) into canonical mapping."""
    import json

    result: dict[str, list[str]] = {}
    if not raw:
        return result

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                raw = parsed
            elif isinstance(parsed, list):
                raw = parsed
            else:
                raw = [raw]
        except Exception:
            raw = [raw]

    if isinstance(raw, dict):
        for handle, aliases in raw.items():
            canonical = normalize_handle(str(handle))
            if isinstance(aliases, str):
                alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
            elif isinstance(aliases, list):
                alias_list = [str(a).strip() for a in aliases if str(a).strip()]
            else:
                alias_list = [str(aliases).strip()] if str(aliases).strip() else []
            result[canonical] = list(dict.fromkeys(alias_list))
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, str):
                continue
            item = item.strip()
            if not item:
                continue
            if item.startswith("{") and item.endswith("}"):
                try:
                    parsed_dict = json.loads(item)
                    if isinstance(parsed_dict, dict):
                        sub_res = parse_aliases(parsed_dict)
                        for k, v in sub_res.items():
                            result.setdefault(k, []).extend(v)
                        continue
                except Exception:
                    pass
            if ":" in item:
                handle_part, alias_part = item.split(":", 1)
                canonical = normalize_handle(handle_part)
                aliases = [a.strip() for a in alias_part.split(",") if a.strip()]
                result.setdefault(canonical, []).extend(aliases)
            elif "=" in item:
                handle_part, alias_part = item.split("=", 1)
                canonical = normalize_handle(handle_part)
                aliases = [a.strip() for a in alias_part.split(",") if a.strip()]
                result.setdefault(canonical, []).extend(aliases)

    # Deduplicate alias lists
    for k in result:
        result[k] = list(dict.fromkeys(result[k]))

    return result


class Participant(BaseModel):
    """Participant in a multi-agent room."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    handle: str
    status: str = "active"
    last_read_seq_id: int = 0
    aliases: list[str] = Field(default_factory=list)
    joined_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("handle", mode="before")
    @classmethod
    def validate_handle(cls, value: str) -> str:
        return normalize_handle(value)


class Message(BaseModel):
    """Message sent in a room."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    client_msg_id: Optional[str] = None
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
    error: Optional[str] = None


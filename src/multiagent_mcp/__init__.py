"""Multi-Agent MCP package."""

from multiagent_mcp.models import Message, Participant, TurnResult, normalize_handle
from multiagent_mcp.room import RoomManager
from multiagent_mcp.server import (
    init_conversation,
    join_conversation,
    list_participants,
    mcp,
    room,
    send_message,
)

__version__ = "0.1.0"
__all__ = [
    "Message",
    "Participant",
    "TurnResult",
    "normalize_handle",
    "RoomManager",
    "mcp",
    "room",
    "init_conversation",
    "join_conversation",
    "list_participants",
    "send_message",
]

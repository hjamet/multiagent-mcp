"""Multi-Agent MCP package."""

from typing import Any

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


def __getattr__(name: str) -> Any:
    """Lazy-load package exports to keep CLI startup instant."""
    if name in ("Message", "Participant", "TurnResult", "normalize_handle"):
        import multiagent_mcp.models as models

        return getattr(models, name)
    elif name == "RoomManager":
        import multiagent_mcp.room as room_mod

        return getattr(room_mod, name)
    elif name in (
        "mcp",
        "room",
        "init_conversation",
        "join_conversation",
        "list_participants",
        "send_message",
    ):
        import multiagent_mcp.server as srv

        return getattr(srv, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

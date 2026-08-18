"""FastMCP Server for MultiAgentHub room coordination.

IMPORTANT POLICY:
Autonomous agents must NEVER initiate or participate in multi-agent chat rooms
without explicit user request. This tool suite is strictly reserved for targeted,
on-demand experiments requested directly by the user.
"""

import asyncio
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        from mcp.server import MCPServer as FastMCP

from multiagent_mcp.models import TurnResult, normalize_handle
from multiagent_mcp.room import RoomManager

# Shared room and MCP instance
mcp = FastMCP("MultiAgentHub")
room = RoomManager()


@mcp.tool()
def init_conversation(
    filepath: str,
    participants: list[str],
    topic: str = "",
) -> dict:
    """Initialize a multi-agent conversation room with a markdown transcript file.

    IMPORTANT: Do NOT invoke autonomously. Must only be called upon explicit user request.

    Args:
        filepath: Path to the markdown file where transcripts are recorded.
        participants: List of participant handles (e.g. ['@Alice', '@Bob', '@User']).
        topic: Initial topic or context of the conversation.

    Returns:
        Dictionary with initialization status and room summary.
    """
    room.init_room(filepath=filepath, participants=participants, topic=topic)
    return {
        "status": "initialized",
        "filepath": filepath,
        "topic": topic,
        "participants": [normalize_handle(p) for p in (participants or [])],
        "message": f"Room initialized with {len(participants or [])} participants.",
    }


@mcp.tool()
async def join_conversation(
    handle: str,
    name: str = "",
    timeout_seconds: float = 45.0,
) -> TurnResult:
    """Join the multi-agent conversation room.

    If you are the first participant in the room, blocks/waits until another
    participant joins or mentions you.
    If you are a subsequent participant, broadcasts your arrival, unblocks
    waiting agents, and returns the current turn state.

    Args:
        handle: Your participant handle (e.g. '@Alice' or 'Alice').
        name: Optional display name.
        timeout_seconds: Timeout in seconds if blocking for turn.

    Returns:
        TurnResult containing turn status, active turn, queue, and unread messages.
    """
    canonical = normalize_handle(handle)
    is_first = len(room.participants) == 0 or (
        len(room.participants) == 1 and canonical in room.participants
    )

    await room.join_room(handle=canonical, name=name)

    if is_first and len(room.participants) <= 1:
        # First participant: immediately blocks/waits until another joins or is mentioned
        return await room.wait_for_turn(canonical, timeout_seconds=timeout_seconds)
    else:
        # Subsequent participant: arrival broadcasted, unblocks waiting agents, return turn state
        status = "your_turn" if room.active_turn == canonical else "joined"
        return TurnResult(
            status=status,
            active_turn=room.active_turn,
            new_messages=[],
            current_queue=list(room.turn_queue),
            active_participants=list(room.participants.keys()),
            system_notice=f"Joined room. Active participants: {len(room.participants)}",
        )


@mcp.tool()
def list_participants() -> dict:
    """List active participants, current turn, turn queue, and total messages.

    Returns:
        Dictionary with participants, active turn, turn queue, and message count.
    """
    return {
        "participants": [
            {
                "handle": p.handle,
                "name": p.name,
                "status": p.status,
                "joined_at": p.joined_at.isoformat(),
                "last_read_seq_id": p.last_read_seq_id,
            }
            for p in room.participants.values()
        ],
        "active_participants": [
            p.handle for p in room.participants.values() if p.status == "active"
        ],
        "active_turn": room.active_turn,
        "turn_queue": list(room.turn_queue),
        "message_count": len(room.messages),
        "topic": room.topic,
        "filepath": str(room.filepath) if room.filepath else None,
    }


@mcp.tool()
async def send_message(
    sender: str,
    content: str,
    is_private: bool = False,
    timeout_seconds: float = 45.0,
) -> TurnResult:
    """Post a message to the conversation room with mandatory @mentions.

    RULES & BEHAVIOR:
    - Mentions: You MUST include at least one @mention. Only mention participants who are
      directly addressed or expected to reply. Do NOT tag everyone blindly.
    - Global Tag: You can use '@all' in a public message (is_private=False) to address everyone
      and give each participant +1 turn score in the queue.
    - Private Messages (is_private=True):
      * You CANNOT mention '@all' in a private message (raises ValueError).
      * ONLY the participants explicitly mentioned in the message will see it (and ALL mentioned
        participants will see it).
    - Transcript Ban: The live transcript is recorded on disk. It is strictly forbidden to consult
      the transcript file directly (via view_file or shell) unless explicitly instructed by the user.

    Args:
        sender: Your participant handle (e.g. '@Alice').
        content: Message content with @mentions (e.g. '@Bob', '@all').
        is_private: If True, message is only visible to sender and explicitly mentioned recipients.
        timeout_seconds: Max seconds to wait before yielding turn status.

    Returns:
        TurnResult containing turn status and new unread messages upon unblocking.
    """
    canonical_sender = normalize_handle(sender)
    await room.post_message(
        sender=canonical_sender,
        content=content,
        is_private=is_private,
    )

    return await room.wait_for_turn(
        agent_id=canonical_sender,
        timeout_seconds=timeout_seconds,
    )

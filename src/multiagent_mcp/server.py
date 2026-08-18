"""FastMCP Server for MultiAgentHub room coordination.

IMPORTANT POLICY:
Autonomous agents must NEVER initiate or participate in multi-agent chat rooms
without explicit user request. This tool suite is strictly reserved for targeted,
on-demand experiments requested directly by the user.
"""

import asyncio
from typing import Optional, Union

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
    first_speaker: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Initialize a multi-agent conversation room with a markdown transcript file.

    IMPORTANT: Do NOT invoke autonomously. Must only be called upon explicit user request.

    Args:
        filepath: Path to the markdown file where transcripts are recorded.
        participants: List of participant handles (e.g. ['@Alice', '@Bob', '@User']).
        topic: Initial topic or context of the conversation.
        first_speaker: Initial first speaker handle (e.g. '@Alice'). Defaults to first participant.
        force: Force overwrite existing conversation files.

    Returns:
        Dictionary with initialization status and room summary.
    """
    room.init_room(
        filepath=filepath,
        participants=participants,
        topic=topic,
        first_speaker=first_speaker,
        force=force,
    )
    return {
        "status": "initialized",
        "filepath": filepath,
        "topic": topic,
        "participants": [normalize_handle(p) for p in (participants or [])],
        "first_speaker": room.first_speaker,
        "message": f"Room initialized with {len(participants or [])} participants.",
    }


@mcp.tool()
async def join_conversation(
    handle: str,
    name: str = "",
) -> TurnResult:
    """Join the multi-agent conversation room.

    Blocks until ALL declared participants have joined the room (all_joined barrier).
    Once all participants have joined, returns status="your_turn" for first_speaker,
    or status="joined" for other participants.

    Args:
        handle: Your participant handle (e.g. '@Alice' or 'Alice').
        name: Optional display name.

    Returns:
        TurnResult containing turn status, active turn, queue, and unread messages.
    """
    canonical = normalize_handle(handle)
    await room.join_room(handle=canonical, name=name)
    room._load_state()

    all_joined = len(room.participants) > 0 and all(p.status == "active" for p in room.participants.values())

    if not all_joined:
        evt = room._get_event(canonical)
        while True:
            room._load_state()
            all_joined = len(room.participants) > 0 and all(p.status == "active" for p in room.participants.values())
            if all_joined:
                break
            evt.clear()
            try:
                await asyncio.wait_for(evt.wait(), timeout=0.3)
            except asyncio.TimeoutError:
                pass

    # All joined!
    room._load_state()
    participant = room.participants[canonical]
    unread = [
        m
        for m in room.messages
        if m.seq_id > participant.last_read_seq_id
        and m.sender != canonical
        and (not m.is_private or canonical in m.recipients)
    ]
    participant.last_read_seq_id = room.seq_counter
    room._save_state()

    status = "your_turn" if room.active_turn == canonical else "joined"
    active_list = [p.handle for p in room.participants.values() if p.status == "active"]
    notice = (
        f"Transcript: '{room.filepath}'. Interdiction formelle de consulter ce fichier sur disque."
        if room.filepath
        else None
    )
    return TurnResult(
        status=status,
        active_turn=room.active_turn,
        new_messages=unread,
        current_queue=list(room.turn_queue),
        active_participants=active_list,
        system_notice=notice or f"Joined room. Active participants: {len(active_list)}",
    )


@mcp.tool()
def list_participants() -> dict:
    """List active participants, current turn, turn queue, and total messages.

    Returns:
        Dictionary with participants, active turn, turn queue, and message count.
    """
    return room.list_participants()


@mcp.tool()
async def send_message(
    sender: str,
    content: str,
    private: Optional[Union[list[str], bool]] = False,
    is_private: Optional[Union[list[str], bool]] = None,
) -> TurnResult:
    """Post a message to the conversation room with mandatory @mentions or private recipients list.

    RULES & BEHAVIOR:
    - Public Messages (private=False / None / []):
      * You MUST include at least one @mention in the content. Only mention participants who are
        directly addressed or expected to reply.
      * You can use '@all' in a public message to address everyone and give each participant +1 turn score.
    - Private Messages (private=['@Recipient'] or private=True):
      * If private is a list of handles, ONLY the specified handles are recipients and receive +1 priority score.
        Mentions inside the text body do NOT leak or become recipients.
      * You CANNOT mention '@all' in a private message or private recipient list (raises ValueError).
      * ONLY the explicit recipients see the message.
    - Transcript Ban: The live transcript is recorded on disk. It is strictly forbidden to consult
      the transcript file directly (via view_file or shell) unless explicitly instructed by the user.

    Args:
        sender: Your participant handle (e.g. '@Alice').
        content: Message content with @mentions (e.g. '@Bob', '@all').
        private: List of recipient handles (e.g. ['@MJ']) or bool (True for private, False for public).
        is_private: Optional backward compatibility alias for private.

    Returns:
        TurnResult containing turn status and new unread messages upon unblocking.
    """
    if is_private is not None:
        private = is_private

    canonical_sender = normalize_handle(sender)
    await room.post_message(
        sender=canonical_sender,
        content=content,
        private=private,
    )

    return await room.wait_for_turn(
        agent_id=canonical_sender,
    )

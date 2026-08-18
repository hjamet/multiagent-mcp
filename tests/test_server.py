"""Tests for FastMCP server tools and multiagent coordination."""

import asyncio
from pathlib import Path
import pytest

from multiagent_mcp.models import TurnResult
from multiagent_mcp.server import (
    init_conversation,
    join_conversation,
    list_participants,
    mcp,
    room,
    send_message,
    wait_for_turn,
)


@pytest.fixture(autouse=True)
def reset_room(tmp_path: Path):
    """Reset shared room state before each test."""
    test_file = tmp_path / "test_transcript.md"
    room.init_room(filepath=str(test_file))
    yield
    room.init_room(filepath="")


def test_mcp_instance_registered():
    """Verify FastMCP server instance name and tool registration."""
    assert mcp.name == "MultiAgentHub"


def test_init_conversation_tool(tmp_path: Path):
    """Test init_conversation tool execution."""
    file_path = tmp_path / "conv.md"
    result = init_conversation(
        filepath=str(file_path),
        participants=["@Alice", "Bob"],
        topic="Project Planning",
    )

    assert result["status"] == "initialized"
    assert result["filepath"] == str(file_path)
    assert result["topic"] == "Project Planning"
    assert result["participants"] == ["@Alice", "@Bob"]
    assert file_path.exists()
    assert "Project Planning" in file_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_join_conversation_first_and_subsequent_participants():
    """Test join_conversation blocking for first participant and unblocking on second."""
    # First participant Alice joins in background (should block until Bob joins)
    alice_res: TurnResult | None = None

    async def join_alice():
        nonlocal alice_res
        alice_res = await join_conversation(
            handle="@Alice", name="Alice Wonderland", timeout_seconds=2.0
        )

    task = asyncio.create_task(join_alice())
    await asyncio.sleep(0.05)

    # At this point, Alice is waiting
    assert len(room.participants) == 1
    assert "@Alice" in room.participants

    # Second participant Bob joins
    bob_res = await join_conversation(handle="@Bob", name="Bob Builder")
    await task

    # Alice unblocked and received Bob's arrival notice
    assert alice_res is not None
    assert len(alice_res.new_messages) == 1
    assert "@Bob est arrivé dans la conversation" in alice_res.new_messages[0].content

    # Bob's return state
    assert bob_res.status == "joined"
    assert "@Alice" in bob_res.active_participants
    assert "@Bob" in bob_res.active_participants


def test_list_participants_tool():
    """Test list_participants tool output format."""
    room.init_room(filepath="", participants=["@Agent1", "@Agent2"], topic="Testing")
    data = list_participants()

    assert data["topic"] == "Testing"
    assert len(data["participants"]) == 2
    assert "@Agent1" in data["active_participants"]
    assert "@Agent2" in data["active_participants"]
    assert data["message_count"] == 0


@pytest.mark.asyncio
async def test_send_message_non_blocking_and_blocking(tmp_path: Path):
    """Test send_message tool both non-blocking and blocking behaviors."""
    transcript = tmp_path / "chat.md"
    room.init_room(filepath=str(transcript))

    # Pre-register Alice and Bob
    await room.join_room("@Alice")
    await room.join_room("@Bob")

    # Alice sends a message without blocking
    res_sent = await send_message(
        sender="@Alice",
        content="Hello @Bob! What is the update?",
        block_until_turn=False,
    )
    assert res_sent.status == "sent"
    assert room.active_turn == "@Bob"

    # Bob sends response and blocks until next turn/message
    bob_res: TurnResult | None = None

    async def bob_turn():
        nonlocal bob_res
        bob_res = await send_message(
            sender="@Bob",
            content="Everything is on track @Alice!",
            block_until_turn=True,
            timeout_seconds=2.0,
        )

    bob_task = asyncio.create_task(bob_turn())
    await asyncio.sleep(0.05)

    # Alice replies to Bob
    await send_message(
        sender="@Alice",
        content="Great job @Bob!",
        block_until_turn=False,
    )
    await bob_task

    # Bob should unblock and see ONLY the new message from Alice
    assert bob_res is not None
    assert len(bob_res.new_messages) == 1
    assert bob_res.new_messages[0].content == "Great job @Bob!"
    assert bob_res.new_messages[0].sender == "@Alice"


@pytest.mark.asyncio
async def test_send_message_mention_validation_error():
    """Test that send_message raises ValueError if no valid mentions exist."""
    await room.join_room("@Alice")
    await room.join_room("@Bob")

    with pytest.raises(ValueError) as exc:
        await send_message(
            sender="@Alice",
            content="Message with no mentions at all",
            block_until_turn=False,
        )
    assert "Écrivez à au moins l'une des personnes suivantes : @Bob" in str(exc.value)


@pytest.mark.asyncio
async def test_wait_for_turn_tool():
    """Test wait_for_turn tool directly."""
    await room.join_room("@Alice")
    await room.join_room("@Bob")

    # Trigger turn for Bob
    await room.post_message(sender="@Alice", content="Turn for @Bob")

    # Bob waits for turn
    res = await wait_for_turn("@Bob", timeout_seconds=1.0)
    assert res.status == "your_turn"
    assert len(res.new_messages) == 1
    assert res.new_messages[0].content == "Turn for @Bob"

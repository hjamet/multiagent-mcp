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
    assert "🔌 not joined yet" in file_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_join_conversation_first_and_subsequent_participants():
    """Test join_conversation blocking for first participant and unblocking on second."""
    alice_res: TurnResult | None = None

    async def join_alice():
        nonlocal alice_res
        alice_res = await join_conversation(
            handle="@Alice", name="Alice Wonderland"
        )

    task = asyncio.create_task(join_alice())
    await asyncio.sleep(0.05)

    # At this point, Alice is waiting
    assert len(room.participants) == 1
    assert "@Alice" in room.participants
    assert room.participants["@Alice"].status == "active"

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
    assert data["participants"][0]["status"] == "not_joined"
    assert data["participants"][1]["status"] == "not_joined"
    assert data["active_participants"] == []
    assert data["message_count"] == 0


@pytest.mark.asyncio
async def test_send_message_blocking_turn_taking(tmp_path: Path):
    """Test send_message tool blocking until reply arrives."""
    transcript = tmp_path / "chat.md"
    room.init_room(filepath=str(transcript))

    # Pre-register Alice and Bob
    await room.join_room("@Alice")
    await room.join_room("@Bob")

    # Bob sends message and blocks waiting for Alice
    bob_res: TurnResult | None = None

    async def bob_turn():
        nonlocal bob_res
        bob_res = await send_message(
            sender="@Bob",
            content="Hello @Alice, what is your status?",
        )

    bob_task = asyncio.create_task(bob_turn())
    await asyncio.sleep(0.05)

    # Alice replies to Bob
    alice_res: TurnResult | None = None

    async def alice_turn():
        nonlocal alice_res
        alice_res = await send_message(
            sender="@Alice",
            content="All systems operational @Bob!",
        )

    alice_task = asyncio.create_task(alice_turn())
    await bob_task

    # Bob should unblock and see ONLY the new message from Alice
    assert bob_res is not None
    assert len(bob_res.new_messages) == 1
    assert bob_res.new_messages[0].content == "All systems operational @Bob!"
    assert bob_res.new_messages[0].sender == "@Alice"

    # Bob replies to Alice to unblock Alice
    await room.post_message(sender="@Bob", content="Thanks @Alice!")
    await alice_task
    assert alice_res is not None


@pytest.mark.asyncio
async def test_send_message_private_explicit_recipients():
    """Test send_message tool with explicit private recipients list and callout rendering."""
    await room.join_room("@Alice")
    await room.join_room("@Bob")
    await room.join_room("@Charlie")

    # Catch up unread seqs
    await room.wait_for_turn("@Bob")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(room.wait_for_turn("@Charlie"), timeout=0.05)

    # Alice sends private message targeting only @Bob, even though @Charlie is mentioned in text
    alice_task = asyncio.create_task(
        send_message(
            sender="@Alice",
            content="Dis @Bob, je veux vérifier un point sur @Charlie",
            private=["@Bob"],
        )
    )
    await asyncio.sleep(0.05)

    # Bob checks unread messages
    bob_res = await room.wait_for_turn("@Bob")
    assert len(bob_res.new_messages) == 1
    assert bob_res.new_messages[0].is_private is True
    assert bob_res.new_messages[0].recipients == ["@Bob"]

    # Charlie checks unread messages -> must timeout because Charlie has no unread messages
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(room.wait_for_turn("@Charlie"), timeout=0.1)

    # Unblock Alice
    await room.post_message(sender="@Bob", content="Reçu @Alice")
    await alice_task

    # Validate file transcript has WARNING callouts for private message
    assert room.filepath is not None
    file_content = room.filepath.read_text(encoding="utf-8")
    assert "> [!WARNING] 🔒 Message Privé : @Alice ➔ @Bob" in file_content
    assert "> 🔒 **Dernier message (Privé) :** **@Alice** ➔ @Bob" in file_content


@pytest.mark.asyncio
async def test_send_message_mention_validation_error():
    """Test that send_message raises ValueError if no valid mentions exist."""
    await room.join_room("@Alice")
    await room.join_room("@Bob")

    with pytest.raises(ValueError) as exc:
        await send_message(
            sender="@Alice",
            content="Message with no mentions at all",
        )
    assert "Écrivez à au moins l'une des personnes suivantes : @Bob" in str(exc.value)


@pytest.mark.asyncio
async def test_join_conversation_delivers_pending_unread_messages(tmp_path: Path):
    """Test that join_conversation delivers pending unread messages when a participant joins."""
    file_path = tmp_path / "conv_pending.md"
    init_conversation(
        filepath=str(file_path),
        participants=["@Alice", "@Bob"],
        topic="Pending messages test",
    )

    # Alice joins and posts a message targeting Bob
    await room.join_room("@Alice")
    await room.post_message(sender="@Alice", content="Hello @Bob, welcome!")

    # Bob joins conversation
    bob_res = await join_conversation(handle="@Bob")

    # Bob must receive Alice's pending message and have his turn
    assert len(bob_res.new_messages) == 1
    assert bob_res.new_messages[0].content == "Hello @Bob, welcome!"
    assert bob_res.new_messages[0].sender == "@Alice"
    assert bob_res.status == "your_turn"


@pytest.mark.asyncio
async def test_mcp_state_file_persistence(tmp_path: Path):
    """Test that MCP tools correctly create and maintain .state.json on disk."""
    import json
    file_path = tmp_path / "mcp_persisted_room.md"
    state_file = tmp_path / "mcp_persisted_room.md.state.json"

    res = init_conversation(
        filepath=str(file_path),
        participants=["@Alice", "@Bob"],
        topic="MCP Persistence Test",
    )
    assert res["status"] == "initialized"
    assert state_file.exists()

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["topic"] == "MCP Persistence Test"
    assert "@Alice" in state["participants"]
    assert state["participants"]["@Alice"]["status"] == "not_joined"

    # Join Alice
    alice_task = asyncio.create_task(
        join_conversation(handle="@Alice")
    )
    await asyncio.sleep(0.05)

    # Check state file shows Alice active
    state2 = json.loads(state_file.read_text(encoding="utf-8"))
    assert state2["participants"]["@Alice"]["status"] == "active"

    # Unblock Alice by joining Bob
    await join_conversation(handle="@Bob")
    await alice_task



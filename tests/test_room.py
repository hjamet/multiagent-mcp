"""Comprehensive tests for multiagent-mcp Chantier 1 models and RoomManager."""

import asyncio
from pathlib import Path
import pytest

from multiagent_mcp.models import Message, Participant, TurnResult, normalize_handle
from multiagent_mcp.room import RoomManager


def test_normalize_handle():
    assert normalize_handle("Alice") == "@Alice"
    assert normalize_handle("@Bob") == "@Bob"
    assert normalize_handle("  @Charlie  ") == "@Charlie"


def test_participant_model():
    p = Participant(name="Alice", handle="Alice")
    assert p.handle == "@Alice"
    assert p.name == "Alice"
    assert p.status == "active"
    assert p.last_read_seq_id == 0


def test_message_model():
    m = Message(
        seq_id=1,
        sender="Alice",
        recipients=["Bob", "@Charlie"],
        content="Hello team!",
    )
    assert m.sender == "@Alice"
    assert m.recipients == ["@Bob", "@Charlie"]
    assert not m.is_private


def test_turn_result_model():
    res = TurnResult(
        status="your_turn",
        active_turn="@Alice",
        new_messages=[],
        current_queue=["@Bob"],
        active_participants=["@Alice", "@Bob"],
    )
    assert res.status == "your_turn"
    assert res.active_turn == "@Alice"
    assert res.current_queue == ["@Bob"]


@pytest.mark.asyncio
async def test_room_init_and_file_creation(tmp_path: Path):
    room_file = tmp_path / "room_test.md"
    rm = RoomManager()
    rm.init_room(
        filepath=str(room_file),
        participants=["@Alice", "Bob"],
        topic="Brainstorming MCP Architecture",
    )

    assert room_file.exists()
    content = room_file.read_text(encoding="utf-8")
    assert "# Multi-Agent Room" in content
    assert "Brainstorming MCP Architecture" in content
    assert "@Alice" in content
    assert "@Bob" in content
    assert "## Fil de discussion" in content


@pytest.mark.asyncio
async def test_join_room_and_arrival_broadcast(tmp_path: Path):
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))

    # First participant joins
    p1 = await rm.join_room("@Alice", name="Alice Wonderland")
    assert p1.handle == "@Alice"
    assert len(rm.participants) == 1
    assert len(rm.messages) == 0  # No arrival broadcast for 1st participant

    # Second participant joins -> should broadcast arrival notice & wake up Alice
    alice_woken = False

    async def wait_alice():
        nonlocal alice_woken
        res = await rm.wait_for_turn("@Alice", timeout_seconds=2.0)
        if len(res.new_messages) > 0 and "@Bob est arrivé" in res.new_messages[0].content:
            alice_woken = True

    wait_task = asyncio.create_task(wait_alice())
    await asyncio.sleep(0.05)

    p2 = await rm.join_room("@Bob", name="Bob Builder")
    await wait_task

    assert p2.handle == "@Bob"
    assert len(rm.participants) == 2
    assert len(rm.messages) == 1
    assert rm.messages[0].content == "@Bob est arrivé dans la conversation"
    assert alice_woken is True

    # Check transcript file
    file_content = room_file.read_text(encoding="utf-8")
    assert "@Bob est arrivé dans la conversation" in file_content


@pytest.mark.asyncio
async def test_post_message_code_block_stripping_and_mentions(tmp_path: Path):
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")
    await rm.join_room("@Charlie")

    # Mention inside fenced code block should be ignored
    # Only @Bob outside code block should be detected
    content = (
        "Bonjour @Bob !\n"
        "Regarde ce code :\n"
        "```python\n"
        "@Charlie\n"
        "@decorator\n"
        "def foo(): pass\n"
        "```\n"
        "Et aussi la fonction `@Charlie` en inline code."
    )

    msg = await rm.post_message(sender="@Alice", content=content)
    assert msg.recipients == ["@Bob"]
    assert rm.active_turn == "@Bob"
    assert rm.turn_queue == []


@pytest.mark.asyncio
async def test_post_message_rejection_no_valid_mentions(tmp_path: Path):
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")
    await rm.join_room("@Charlie")

    # No mentions at all
    with pytest.raises(ValueError) as exc_info:
        await rm.post_message(sender="@Alice", content="Salut tout le monde sans mentionner personne.")
    assert "Écrivez à au moins l'une des personnes suivantes : @Bob, @Charlie" in str(exc_info.value)

    # Only unknown participant mentioned
    with pytest.raises(ValueError) as exc_info2:
        await rm.post_message(sender="@Alice", content="Salut @David comment vas-tu ?")
    assert "Écrivez à au moins l'une des personnes suivantes : @Bob, @Charlie" in str(exc_info2.value)


@pytest.mark.asyncio
async def test_post_message_deduplication_in_turn_queue(tmp_path: Path):
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")
    await rm.join_room("@Charlie")

    # Bob is mentioned 3 times, Charlie once
    content = "Hello @Bob, check with @Bob please, and also @Bob and @Charlie."
    msg = await rm.post_message(sender="@Alice", content=content)

    assert msg.recipients == ["@Bob", "@Charlie"]
    # First speaker popped into active_turn is @Bob, remaining in queue is @Charlie
    assert rm.active_turn == "@Bob"
    assert rm.turn_queue == ["@Charlie"]


@pytest.mark.asyncio
async def test_private_message_visibility_and_formatting(tmp_path: Path):
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")
    await rm.join_room("@Charlie")

    # Clear previous read seqs
    rm.participants["@Charlie"].last_read_seq_id = rm.seq_counter
    rm.participants["@Bob"].last_read_seq_id = rm.seq_counter

    # Alice sends private message to Bob
    await rm.post_message(sender="@Alice", content="Secret message for @Bob", is_private=True)

    # Bob checks turn
    bob_res = await rm.wait_for_turn("@Bob", timeout_seconds=1.0)
    assert bob_res.status == "your_turn"
    assert len(bob_res.new_messages) == 1
    assert bob_res.new_messages[0].content == "Secret message for @Bob"
    assert bob_res.new_messages[0].is_private is True

    # Charlie checks messages -> should NOT see the private message
    charlie_res = await rm.wait_for_turn("@Charlie", timeout_seconds=0.1)
    assert len(charlie_res.new_messages) == 0

    # Transcript file has private tag
    file_content = room_file.read_text(encoding="utf-8")
    assert "🔒 [Message Privé] @Alice ➔ @Bob" in file_content
    assert "Secret message for @Bob" in file_content


@pytest.mark.asyncio
async def test_wait_for_turn_unread_tracking(tmp_path: Path):
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")

    # Alice posts message 1 to Bob
    await rm.post_message(sender="@Alice", content="Question 1 @Bob")

    # Bob waits and gets message 1
    res1 = await rm.wait_for_turn("@Bob", timeout_seconds=1.0)
    assert res1.status == "your_turn"
    assert len(res1.new_messages) == 1
    assert res1.new_messages[0].content == "Question 1 @Bob"

    # Bob posts reply to Alice
    await rm.post_message(sender="@Bob", content="Answer 1 @Alice")

    # Bob calls wait_for_turn immediately without new messages -> should timeout
    res_empty = await rm.wait_for_turn("@Bob", timeout_seconds=0.2)
    assert res_empty.status == "timeout"
    assert len(res_empty.new_messages) == 0

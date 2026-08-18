"""Advanced multi-agent scenario and edge-case tests."""

import asyncio
from pathlib import Path
import pytest

from multiagent_mcp.room import RoomManager


@pytest.mark.asyncio
async def test_three_agent_turn_sequence(tmp_path: Path):
    room_file = tmp_path / "team_chat.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file), topic="Projet Alpha")

    await rm.join_room("@Alice")
    await rm.join_room("@Bob")
    await rm.join_room("@Charlie")

    # 1. Alice addresses Bob and Charlie
    await rm.post_message(sender="@Alice", content="Salut @Bob et @Charlie, commençons la réunion !")
    assert rm.active_turn == "@Bob"
    assert rm.turn_queue == ["@Charlie"]

    # 2. Bob receives turn and responds to Alice
    bob_turn = await rm.wait_for_turn("@Bob", timeout_seconds=1.0)
    assert bob_turn.status == "your_turn"
    # Bob receives Charlie's arrival notice (seq 2) and Alice's message (seq 3)
    assert len(bob_turn.new_messages) == 2
    assert "@Charlie est arrivé" in bob_turn.new_messages[0].content
    assert "Salut @Bob et @Charlie" in bob_turn.new_messages[1].content

    await rm.post_message(sender="@Bob", content="Je suis prêt @Alice.")
    # Next speaker should be Charlie (who was in queue from Alice's message)
    assert rm.active_turn == "@Charlie"
    assert rm.turn_queue == ["@Alice"]

    # 3. Charlie receives turn and responds to Bob
    charlie_turn = await rm.wait_for_turn("@Charlie", timeout_seconds=1.0)
    assert charlie_turn.status == "your_turn"
    # Charlie was new at seq 2, so he receives Alice's message (seq 3) + Bob's message (seq 4)
    assert len(charlie_turn.new_messages) == 2
    assert "Salut @Bob et @Charlie" in charlie_turn.new_messages[0].content
    assert "Je suis prêt @Alice." in charlie_turn.new_messages[1].content

    await rm.post_message(sender="@Charlie", content="Moi aussi @Bob !")
    # Next speaker should be Alice (enqueued by Bob)
    assert rm.active_turn == "@Alice"
    assert rm.turn_queue == ["@Bob"]

    # 4. Alice receives turn and her unread messages
    alice_turn = await rm.wait_for_turn("@Alice", timeout_seconds=1.0)
    assert alice_turn.status == "your_turn"
    # Alice receives Bob's reply (seq 4) + Charlie's reply (seq 5)
    assert len(alice_turn.new_messages) == 2
    assert "Je suis prêt @Alice." in alice_turn.new_messages[0].content
    assert "Moi aussi @Bob !" in alice_turn.new_messages[1].content


@pytest.mark.asyncio
async def test_case_insensitive_mention_resolution(tmp_path: Path):
    room_file = tmp_path / "case_test.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")

    # Lowercase mention in text should resolve to canonical @Bob
    msg = await rm.post_message(sender="@Alice", content="Message pour @bob s'il te plaît")
    assert msg.recipients == ["@Bob"]
    assert rm.active_turn == "@Bob"


@pytest.mark.asyncio
async def test_file_transcript_preservation(tmp_path: Path):
    room_file = tmp_path / "history.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file), topic="Histoire")
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")

    await rm.post_message(sender="@Alice", content="Premier message pour @Bob")
    await rm.join_room("@Charlie")
    await rm.post_message(sender="@Charlie", content="Troisième message pour @Alice")

    content = room_file.read_text(encoding="utf-8")
    assert "@Alice" in content
    assert "@Bob" in content
    assert "@Charlie" in content
    assert "Premier message pour @Bob" in content
    assert "@Charlie est arrivé dans la conversation" in content
    assert "Troisième message pour @Alice" in content


@pytest.mark.asyncio
async def test_mixed_private_public_with_unjoined_agents(tmp_path: Path):
    """Test scenario with mixed private and public messages with unjoined agents."""
    room_file = tmp_path / "mixed.md"
    rm = RoomManager()
    rm.init_room(
        filepath=str(room_file),
        participants=["@Alice", "@Bob", "@Charlie", "@David"],
        topic="Confidential Strategy",
    )

    # Alice and Bob join
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")

    # Alice sends private message to Bob discussing Charlie and David
    await rm.post_message(
        sender="@Alice",
        content="Hey @Bob, attendons avant de sonder @Charlie et @David.",
        private=["@Bob"],
    )

    assert rm.priority_scores["@Bob"] == 1
    assert rm.priority_scores["@Charlie"] == 0
    assert rm.priority_scores["@David"] == 0

    # Charlie joins
    await rm.join_room("@Charlie")
    # Charlie should not see the private message between Alice and Bob
    charlie_res = await rm.wait_for_turn("@Charlie", timeout_seconds=0.1)
    assert len(charlie_res.new_messages) == 0

    # Bob sends public message to all
    await rm.post_message(sender="@Bob", content="Bienvenue à tous @all !")
    charlie_turn = await rm.wait_for_turn("@Charlie", timeout_seconds=1.0)
    assert len(charlie_turn.new_messages) == 1
    assert "Bienvenue à tous @all !" in charlie_turn.new_messages[0].content

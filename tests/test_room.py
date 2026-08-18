"""Comprehensive tests for multiagent-mcp models and RoomManager."""

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
    assert rm.participants["@Alice"].status == "not_joined"
    assert rm.participants["@Bob"].status == "not_joined"
    assert rm.priority_scores["@Alice"] == 0
    assert rm.priority_scores["@Bob"] == 0

    content = room_file.read_text(encoding="utf-8")
    assert "# Multi-Agent Room" in content
    assert "Brainstorming MCP Architecture" in content
    assert "@Alice" in content
    assert "@Bob" in content
    assert "🔌 not joined yet" in content
    assert "## Fil de discussion" in content


@pytest.mark.asyncio
async def test_join_room_and_arrival_broadcast(tmp_path: Path):
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))

    # First participant joins
    p1 = await rm.join_room("@Alice", name="Alice Wonderland")
    assert p1.handle == "@Alice"
    assert p1.status == "active"
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
    assert p2.status == "active"
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

    # Bob is mentioned 3 times in same message, Charlie once
    content = "Hello @Bob, check with @Bob please, and also @Bob and @Charlie."
    msg = await rm.post_message(sender="@Alice", content=content)

    assert msg.recipients == ["@Bob", "@Charlie"]
    # Both have score 1, Bob was mentioned first so Bob is active_turn
    assert rm.priority_scores["@Bob"] == 1
    assert rm.priority_scores["@Charlie"] == 1
    assert rm.priority_scores.get("@Alice", 0) == 0
    assert rm.active_turn == "@Bob"

    # Bob speaks: Bob's score MUST reset to 0, Charlie gets +1 (score 2)
    await rm.post_message(sender="@Bob", content="Understood! Checking with @Charlie now.")
    assert rm.priority_scores["@Bob"] == 0
    assert rm.priority_scores["@Charlie"] == 2
    assert rm.active_turn == "@Charlie"

    # Charlie speaks and mentions Alice: Charlie's score MUST reset to 0, Alice gets score 1
    await rm.post_message(sender="@Charlie", content="All good, back to you @Alice!")
    assert rm.priority_scores["@Charlie"] == 0
    assert rm.priority_scores["@Alice"] == 1
    assert rm.active_turn == "@Alice"


@pytest.mark.asyncio
async def test_explicit_private_recipients_list_no_leak(tmp_path: Path):
    """Test that private=['@MJ'] does NOT leak to @Antoine even if @Antoine is in text body."""
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file), participants=["@Claire", "@MJ", "@Antoine"])
    await rm.join_room("@Claire")
    await rm.join_room("@MJ")
    await rm.join_room("@Antoine")

    # Clear previous read seqs
    rm.participants["@Antoine"].last_read_seq_id = rm.seq_counter
    rm.participants["@MJ"].last_read_seq_id = rm.seq_counter

    # Claire sends private message to MJ discussing Antoine
    content = "Je veux sonder @Antoine discrètement pour connaître son avis."
    msg = await rm.post_message(sender="@Claire", content=content, private=["@MJ"])

    assert msg.is_private is True
    assert msg.recipients == ["@MJ"]
    assert rm.priority_scores["@MJ"] == 1
    assert rm.priority_scores["@Antoine"] == 0
    assert rm.active_turn == "@MJ"

    # MJ receives the message
    mj_res = await rm.wait_for_turn("@MJ", timeout_seconds=1.0)
    assert mj_res.status == "your_turn"
    assert len(mj_res.new_messages) == 1
    assert mj_res.new_messages[0].content == content
    assert mj_res.new_messages[0].is_private is True

    # Antoine checks messages -> must NOT receive this private message
    antoine_res = await rm.wait_for_turn("@Antoine", timeout_seconds=0.1)
    assert len(antoine_res.new_messages) == 0

    # Verify transcript header
    file_content = room_file.read_text(encoding="utf-8")
    assert "### 🔒 [Message Privé] @Claire ➔ @MJ" in file_content
    assert "➔ @Antoine" not in file_content.split("### 🔒 [Message Privé]")[1].split("\n\n")[0]


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

    # Alice sends private message to Bob with boolean is_private=True
    await rm.post_message(sender="@Alice", content="Secret message for @Bob", private=True)

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
    assert "### 🔒 [Message Privé] @Alice ➔ @Bob" in file_content
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


@pytest.mark.asyncio
async def test_all_mention_public_and_private_rejection(tmp_path: Path):
    room_file = tmp_path / "transcript.md"
    rm = RoomManager()
    rm.init_room(filepath=str(room_file))
    await rm.join_room("@Alice")
    await rm.join_room("@Bob")
    await rm.join_room("@Charlie")

    # 1. Public message with @all
    msg = await rm.post_message(sender="@Alice", content="Hello everyone @all!", private=False)
    assert "@Bob" in msg.recipients
    assert "@Charlie" in msg.recipients
    assert "@Alice" not in msg.recipients

    # 2. Private message with @all in text should raise ValueError
    with pytest.raises(ValueError) as exc1:
        await rm.post_message(sender="@Alice", content="Secret to @all", private=True)
    assert "Impossible de mentionner @all dans un message privé" in str(exc1.value)

    # 3. Private message with @all in private list should raise ValueError
    with pytest.raises(ValueError) as exc2:
        await rm.post_message(sender="@Alice", content="Secret message", private=["@all"])
    assert "Impossible de mentionner @all dans un message privé" in str(exc2.value)


@pytest.mark.asyncio
async def test_not_joined_status_transition_and_urgency_sorting(tmp_path: Path):
    """Test participant states ('not_joined' -> 'active') and priority table ordering."""
    room_file = tmp_path / "dashboard.md"
    rm = RoomManager()
    rm.init_room(
        filepath=str(room_file),
        participants=["@Alice", "@Bob", "@Charlie"],
        topic="Live Dashboard Test",
    )

    # Initial state: all not joined yet
    init_content = room_file.read_text(encoding="utf-8")
    assert "🔌 not joined yet" in init_content
    assert rm.participants["@Alice"].status == "not_joined"

    # Alice joins -> active (sleeping)
    await rm.join_room("@Alice")
    assert rm.participants["@Alice"].status == "active"
    assert rm.participants["@Bob"].status == "not_joined"

    # Alice mentions Bob (Bob is still not_joined)
    await rm.post_message(sender="@Alice", content="Message pour @Bob")
    assert rm.priority_scores["@Bob"] == 1

    content_after_msg = room_file.read_text(encoding="utf-8")
    table_section = content_after_msg.split("## 📊 File d'Attente")[1].split("## Fil de discussion")[0]

    # Alice is active/sleeping, Bob and Charlie are not_joined
    assert "| **@Alice** | 💤 sleeping |" in table_section
    assert "| **@Bob** | 🔌 not joined yet |" in table_section
    assert "| **@Charlie** | 🔌 not joined yet |" in table_section

    # Bob joins -> Bob becomes active and has score 1
    await rm.join_room("@Bob")
    content_after_bob = room_file.read_text(encoding="utf-8")
    table_bob = content_after_bob.split("## 📊 File d'Attente")[1].split("## Fil de discussion")[0]

    assert "| **@Bob** | ⏳ 1 mention |" in table_bob
    assert "| **@Alice** | 💤 sleeping |" in table_bob
    assert "| **@Charlie** | 🔌 not joined yet |" in table_bob

    # Check order: Bob (score 1) > Alice (sleeping) > Charlie (not joined yet)
    bob_idx = table_bob.find("**@Bob**")
    alice_idx = table_bob.find("**@Alice**")
    charlie_idx = table_bob.find("**@Charlie**")
    assert bob_idx != -1 and alice_idx != -1 and charlie_idx != -1
    assert bob_idx < alice_idx < charlie_idx

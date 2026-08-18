"""Comprehensive tests for multiagent-mcp daemon IPC server."""

import asyncio
from pathlib import Path
import struct
import pytest

from multiagent_mcp.daemon import (
    DaemonClient,
    DaemonServer,
    get_daemon_port,
    read_msg,
    write_msg,
)


@pytest.mark.asyncio
async def test_framing_read_write():
    """Test TCP length-prefixed binary framing utilities."""
    reader = asyncio.StreamReader()
    writer_transport = asyncio.Transport()

    # Test encoding and decoding manually
    data = {"action": "ping", "id": 42, "payload": "héhé voilà"}
    encoded_json = b'{"action": "ping", "id": 42, "payload": "h\xc3\xa9h\xc3\xa9 voil\xc3\xa0"}'
    header = struct.pack(">I", len(encoded_json))

    reader.feed_data(header + encoded_json)
    reader.feed_eof()

    msg = await read_msg(reader)
    assert msg is not None
    assert msg["action"] == "ping"
    assert msg["id"] == 42
    assert msg["payload"] == "héhé voilà"


@pytest.mark.asyncio
async def test_daemon_lifecycle_and_discovery(tmp_path: Path):
    """Test DaemonServer start, discovery files, and stop."""
    config_dir = tmp_path / "config"
    state_file = tmp_path / "test_room.state.json"
    transcript_file = tmp_path / "transcript.md"

    server = DaemonServer(host="127.0.0.1", port=0, config_dir=config_dir, state_file=state_file)
    await server.start()
    assert server.port > 0

    json_file = config_dir / "daemon.json"
    port_file = config_dir / "daemon.port"
    assert json_file.exists()
    assert port_file.exists()
    assert int(port_file.read_text(encoding="utf-8").strip()) == server.port
    assert get_daemon_port(config_dir) == server.port

    client = DaemonClient(host="127.0.0.1", port=server.port)

    # Test ping
    ping_res = await client.ping()
    assert ping_res["status"] == "ok"

    # Test init
    init_res = await client.init(
        filepath=str(transcript_file),
        participants=["@Alice", "@Bob"],
        topic="Test Topic",
    )
    assert init_res["status"] == "initialized"
    assert transcript_file.exists()

    # Test join
    join_res = await client.join(handle="@Alice", name="Alice Wonderland")
    assert join_res["status"] == "joined"
    assert "@Alice" in join_res["active_participants"]

    # Test list
    list_res = await client.list_participants()
    assert len(list_res["participants"]) == 2
    assert list_res["topic"] == "Test Topic"

    # Test stop
    stop_res = await client.stop()
    assert stop_res["status"] == "stopped"

    # Wait for server shutdown
    await server.stop()
    assert not json_file.exists()
    assert not port_file.exists()


@pytest.mark.asyncio
async def test_daemon_turn_coordination_and_in_memory_wakeup(tmp_path: Path):
    """Test fast turn coordination and in-memory waking of waiting clients."""
    config_dir = tmp_path / "config"
    transcript_file = tmp_path / "transcript.md"

    server = DaemonServer(host="127.0.0.1", port=0, config_dir=config_dir)
    await server.start()

    client_alice = DaemonClient(host="127.0.0.1", port=server.port)
    client_bob = DaemonClient(host="127.0.0.1", port=server.port)

    try:
        # Initialize room
        await client_alice.init(
            filepath=str(transcript_file),
            participants=["@Alice", "@Bob"],
            topic="Pair Programming",
        )

        # Alice joins
        await client_alice.join("@Alice")

        # Bob joins
        await client_bob.join("@Bob")

        # Bob waits in background task
        bob_wait_task = asyncio.create_task(client_bob.wait("@Bob"))

        # Give small tick to ensure Bob is waiting
        await asyncio.sleep(0.02)
        assert "@Bob" in server.waiting_clients

        # Alice sends message addressing Bob
        send_res = await client_alice.send(
            sender="@Alice",
            content="@Bob hello! Peux-tu vérifier le code ?",
        )
        assert send_res["status"] == "sent"
        assert send_res["active_turn"] == "@Bob"

        # Bob should wake up immediately via in-memory future
        bob_turn = await asyncio.wait_for(bob_wait_task, timeout=1.0)
        assert bob_turn["status"] == "your_turn"
        assert bob_turn["active_turn"] == "@Bob"
        assert len(bob_turn["new_messages"]) >= 1
        assert bob_turn["new_messages"][-1]["content"] == "@Bob hello! Peux-tu vérifier le code ?"

        # Bob sends reply back to Alice
        await client_bob.send(sender="@Bob", content="@Alice tout est bon !")

        # Alice calls wait and gets fast-path immediate return
        alice_turn = await client_alice.wait("@Alice")
        assert alice_turn["status"] == "your_turn"
        assert alice_turn["active_turn"] == "@Alice"
        assert any(m["sender"] == "@Bob" for m in alice_turn["new_messages"])

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_daemon_error_handling(tmp_path: Path):
    """Test error handling in DaemonClient and server."""
    config_dir = tmp_path / "config"
    transcript_file = tmp_path / "transcript.md"

    server = DaemonServer(host="127.0.0.1", port=0, config_dir=config_dir)
    await server.start()

    client = DaemonClient(host="127.0.0.1", port=server.port)

    try:
        await client.init(
            filepath=str(transcript_file),
            participants=["@Alice"],
            topic="Error testing",
        )

        # Wait for unregistered participant should raise error
        with pytest.raises(RuntimeError, match="Participant @Unknown not registered in room"):
            await client.wait("@Unknown")

        # Send with no mention should raise error
        await client.join("@Alice")
        with pytest.raises(RuntimeError, match="Écrivez à au moins l'une des personnes suivantes"):
            await client.send(sender="@Alice", content="Hello without mention")

    finally:
        await server.stop()

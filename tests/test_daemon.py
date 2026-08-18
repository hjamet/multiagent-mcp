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
    """Test fast turn coordination and blocking send with in-memory waking."""
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

        # Alice sends message addressing Bob and blocks waiting for next turn
        alice_send_task = asyncio.create_task(
            client_alice.send(
                sender="@Alice",
                content="@Bob hello! Peux-tu vérifier le code ?",
            )
        )

        # Give small tick to ensure Alice is waiting
        await asyncio.sleep(0.02)
        assert "@Alice" in server.waiting_clients

        # Bob sends reply back to Alice and blocks waiting for next turn
        bob_send_task = asyncio.create_task(
            client_bob.send(sender="@Bob", content="@Alice tout est bon !")
        )

        # Alice should wake up immediately via in-memory future with Bob's reply
        alice_turn = await asyncio.wait_for(alice_send_task, timeout=1.0)
        assert alice_turn["status"] == "your_turn"
        assert alice_turn["active_turn"] == "@Alice"
        assert any(m["sender"] == "@Bob" for m in alice_turn["new_messages"])
        assert any("tout est bon" in m["content"] for m in alice_turn["new_messages"])

        # Alice sends follow-up in task to unblock Bob
        alice_send_task2 = asyncio.create_task(
            client_alice.send(sender="@Alice", content="@Bob merci beaucoup !")
        )
        bob_turn = await asyncio.wait_for(bob_send_task, timeout=1.0)
        assert bob_turn["status"] == "your_turn" or bob_turn["status"] == "message_received"
        assert any(m["sender"] == "@Alice" for m in bob_turn["new_messages"])
        assert any("merci beaucoup" in m["content"] for m in bob_turn["new_messages"])

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

        # Send with no mention should raise error
        await client.join("@Alice")
        with pytest.raises(RuntimeError, match="Écrivez à au moins l'une des personnes suivantes"):
            await client.send(sender="@Alice", content="Hello without mention")

    finally:
        await server.stop()

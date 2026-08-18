"""Tests for multiagent-mcp CLI interface."""

import json
from pathlib import Path
from unittest.mock import patch
import pytest

from multiagent_mcp.cli import build_parser, main
from multiagent_mcp.server import room


def test_cli_parser_defaults():
    """Test argument parsing for all subcommands."""
    parser = build_parser()

    # Serve command defaults
    args_serve = parser.parse_args(["serve"])
    assert args_serve.command == "serve"
    assert args_serve.host == "127.0.0.1"
    assert args_serve.port == 8000

    # Serve with custom host and port
    args_custom = parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "9000"])
    assert args_custom.host == "0.0.0.0"
    assert args_custom.port == 9000

    # Stdio command
    args_stdio = parser.parse_args(["stdio"])
    assert args_stdio.command == "stdio"

    # Init command
    args_init = parser.parse_args([
        "init",
        "--file", "room.md",
        "--participants", "@Alice", "@Bob",
        "--topic", "Discussion Topic",
    ])
    assert args_init.command == "init"
    assert args_init.file == "room.md"
    assert args_init.participants == ["@Alice", "@Bob"]
    assert args_init.topic == "Discussion Topic"

    # Join command
    args_join = parser.parse_args([
        "join",
        "--handle", "@Alice",
        "--name", "Alice W.",
    ])
    assert args_join.command == "join"
    assert args_join.handle == "@Alice"
    assert args_join.name == "Alice W."

    # Send command (public)
    args_send = parser.parse_args([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Hello",
    ])
    assert args_send.command == "send"
    assert args_send.sender == "@Alice"
    assert args_send.content == "@Bob Hello"
    assert args_send.private is None

    # Send command (private with recipients)
    args_send_priv = parser.parse_args([
        "send",
        "--sender", "@Alice",
        "--content", "Secret",
        "--private", "@Bob", "@Charlie",
    ])
    assert args_send_priv.private == ["@Bob", "@Charlie"]

    # Send command (private flag)
    args_send_flag = parser.parse_args([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Secret",
        "--private",
    ])
    assert args_send_flag.private == []

    # Wait command
    args_wait = parser.parse_args(["wait", "--handle", "@Bob"])
    assert args_wait.command == "wait"
    assert args_wait.handle == "@Bob"

    # List command
    args_list = parser.parse_args(["list"])
    assert args_list.command == "list"


def test_cli_no_command_shows_help(capsys):
    """Test that running with no args shows help and exits with 0."""
    exit_code = main([])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "usage: multiagent-mcp" in captured.out


def test_cli_serve_invokes_mcp_run():
    """Test that serve command invokes mcp.run with sse transport and host/port."""
    with patch("multiagent_mcp.cli.mcp.run") as mock_run:
        exit_code = main(["serve", "--host", "127.0.0.1", "--port", "8000"])
        assert exit_code == 0
        mock_run.assert_called_once_with(
            transport="sse", host="127.0.0.1", port=8000
        )


def test_cli_stdio_invokes_mcp_run():
    """Test that stdio command invokes mcp.run with stdio transport."""
    with patch("multiagent_mcp.cli.mcp.run") as mock_run:
        exit_code = main(["stdio"])
        assert exit_code == 0
        mock_run.assert_called_once_with(transport="stdio")


def test_cli_init_command(tmp_path: Path, capsys):
    """Test init subcommand creates transcript and returns JSON status."""
    transcript = tmp_path / "chat.md"
    exit_code = main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
        "--topic", "Strategy Meeting",
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "initialized"
    assert data["filepath"] == str(transcript)
    assert data["topic"] == "Strategy Meeting"
    assert "@Alice" in data["participants"]
    assert "@Bob" in data["participants"]
    assert transcript.exists()


def test_cli_list_command(tmp_path: Path, capsys):
    """Test list subcommand returns JSON with participants and status."""
    transcript = tmp_path / "chat_list.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
        "--topic", "Review",
    ])
    capsys.readouterr()  # clear buffer

    exit_code = main(["list"])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "participants" in data
    assert "active_participants" in data
    assert data["topic"] == "Review"
    assert len(data["participants"]) == 2


def test_cli_join_and_wait_commands(tmp_path: Path, capsys):
    """Test join and wait subcommands return TurnResult JSON."""
    import threading
    import time
    transcript = tmp_path / "chat_join.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
    ])
    capsys.readouterr()

    # Alice joins (first participant, blocks until Bob joins)
    def join_alice():
        main(["join", "--handle", "@Alice", "--name", "Alice Smith"])

    t = threading.Thread(target=join_alice)
    t.start()
    time.sleep(0.05)

    # Bob joins (second participant, unblocks Alice)
    exit_code = main(["join", "--handle", "@Bob", "--name", "Bob Jones"])
    assert exit_code == 0
    t.join(timeout=2.0)
    assert not t.is_alive()

    # Alice sends a message to Bob in a thread
    def send_alice():
        main(["send", "--sender", "@Alice", "--content", "Hello @Bob"])

    t_send = threading.Thread(target=send_alice)
    t_send.start()
    time.sleep(0.05)

    # Bob calls wait -> unblocks with Alice's message
    capsys.readouterr()
    exit_code = main(["wait", "--handle", "@Bob"])
    assert exit_code == 0
    captured = capsys.readouterr()
    wait_res = json.loads(captured.out)
    assert wait_res["status"] == "your_turn"

    # Bob replies to Alice to unblock Alice's send
    main(["send", "--sender", "@Bob", "--content", "Hello @Alice"])
    t_send.join(timeout=2.0)
    assert not t_send.is_alive()


def test_cli_send_public_command(tmp_path: Path, capsys):
    """Test send subcommand with public message and @mentions."""
    import threading
    import time
    transcript = tmp_path / "chat_send.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
    ])
    # Register Alice and Bob
    t_join = threading.Thread(target=lambda: main(["join", "--handle", "@Alice"]))
    t_join.start()
    time.sleep(0.05)
    main(["join", "--handle", "@Bob"])
    t_join.join(timeout=2.0)
    capsys.readouterr()

    # Alice sends public message to Bob
    t_send = threading.Thread(
        target=lambda: main([
            "send",
            "--sender", "@Alice",
            "--content", "@Bob please check this out",
        ])
    )
    t_send.start()
    time.sleep(0.05)

    # Bob waits and gets the message, then replies
    main(["wait", "--handle", "@Bob"])
    main(["send", "--sender", "@Bob", "--content", "Got it @Alice"])
    t_send.join(timeout=2.0)
    assert not t_send.is_alive()


def test_cli_send_private_recipients_list(tmp_path: Path, capsys):
    """Test send subcommand with explicit private recipients list."""
    import threading
    import time
    transcript = tmp_path / "chat_priv_list.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob", "@Charlie",
    ])
    t_join = threading.Thread(target=lambda: main(["join", "--handle", "@Alice"]))
    t_join.start()
    time.sleep(0.05)
    main(["join", "--handle", "@Bob"])
    t_join.join(timeout=2.0)
    main(["join", "--handle", "@Charlie"])
    capsys.readouterr()

    # Send private message with --private @Bob
    t_send = threading.Thread(
        target=lambda: main([
            "send",
            "--sender", "@Alice",
            "--content", "Confidential info",
            "--private", "@Bob",
        ])
    )
    t_send.start()
    time.sleep(0.05)

    # Bob waits and checks
    main(["wait", "--handle", "@Bob"])
    assert room.last_posted_message is not None
    assert room.last_posted_message.is_private is True
    assert room.last_posted_message.recipients == ["@Bob"]

    main(["send", "--sender", "@Bob", "--content", "Understood @Alice"])
    t_send.join(timeout=2.0)
    assert not t_send.is_alive()


def test_cli_send_private_flag_and_boolean_strings(tmp_path: Path, capsys):
    """Test send subcommand with --private flag and boolean strings."""
    import threading
    import time
    transcript = tmp_path / "chat_priv_flag.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
    ])
    t_join = threading.Thread(target=lambda: main(["join", "--handle", "@Alice"]))
    t_join.start()
    time.sleep(0.05)
    main(["join", "--handle", "@Bob"])
    t_join.join(timeout=2.0)
    capsys.readouterr()

    # Send with --private flag alone
    t1 = threading.Thread(target=lambda: main([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Secret flag message",
        "--private",
    ]))
    t1.start()
    time.sleep(0.05)
    assert room.last_posted_message.is_private is True
    main(["send", "--sender", "@Bob", "--content", "OK @Alice"])
    t1.join(timeout=2.0)

    # Send with --private true
    t2 = threading.Thread(target=lambda: main([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Secret true string",
        "--private", "true",
    ]))
    t2.start()
    time.sleep(0.05)
    assert room.last_posted_message.is_private is True
    main(["send", "--sender", "@Bob", "--content", "OK 2 @Alice"])
    t2.join(timeout=2.0)

    # Send with --private false
    t3 = threading.Thread(target=lambda: main([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Public false string",
        "--private", "false",
    ]))
    t3.start()
    time.sleep(0.05)
    assert room.last_posted_message.is_private is False
    main(["send", "--sender", "@Bob", "--content", "OK 3 @Alice"])
    t3.join(timeout=2.0)


def test_cli_file_backed_persistence_across_commands(tmp_path: Path, capsys):
    """Test full multiagent lifecycle via separate CLI invocations with file persistence."""
    import threading
    import time
    transcript = tmp_path / "cli_persistent_chat.md"
    state_file = tmp_path / "cli_persistent_chat.md.state.json"

    # 1. init
    exit_code = main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
        "--topic", "CLI Persistence",
    ])
    assert exit_code == 0
    assert state_file.exists()

    # 2. join Alice (in thread) & Bob
    t_join = threading.Thread(target=lambda: main(["join", "--handle", "@Alice"]))
    t_join.start()
    time.sleep(0.05)

    exit_code = main(["join", "--handle", "@Bob"])
    assert exit_code == 0
    t_join.join(timeout=2.0)

    # 3. send Alice -> Bob
    t_send = threading.Thread(
        target=lambda: main([
            "send",
            "--sender", "@Alice",
            "--content", "@Bob Hello from separate CLI process",
        ])
    )
    t_send.start()
    time.sleep(0.05)

    # 4. wait Bob
    capsys.readouterr()
    exit_code = main(["wait", "--handle", "@Bob"])
    assert exit_code == 0
    captured = capsys.readouterr()
    wait_data = json.loads(captured.out)
    assert wait_data["status"] == "your_turn"
    assert len(wait_data["new_messages"]) >= 1
    assert "Hello from separate CLI process" in wait_data["new_messages"][-1]["content"]

    # Bob replies to unblock Alice
    main(["send", "--sender", "@Bob", "--content", "Ack @Alice"])
    t_send.join(timeout=2.0)

    # 5. list
    capsys.readouterr()
    exit_code = main(["list"])
    assert exit_code == 0
    captured = capsys.readouterr()
    list_data = json.loads(captured.out)
    assert list_data["active_turn"] == "@Alice"
    assert len(list_data["active_participants"]) == 2


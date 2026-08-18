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
        "--timeout", "10.5",
    ])
    assert args_join.command == "join"
    assert args_join.handle == "@Alice"
    assert args_join.name == "Alice W."
    assert args_join.timeout == 10.5

    # Send command (public)
    args_send = parser.parse_args([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Hello",
        "--timeout", "5.0",
    ])
    assert args_send.command == "send"
    assert args_send.sender == "@Alice"
    assert args_send.content == "@Bob Hello"
    assert args_send.private is None
    assert args_send.timeout == 5.0

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
    args_wait = parser.parse_args(["wait", "--handle", "@Bob", "--timeout", "15.0"])
    assert args_wait.command == "wait"
    assert args_wait.handle == "@Bob"
    assert args_wait.timeout == 15.0

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
    transcript = tmp_path / "chat_join.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
    ])
    capsys.readouterr()

    # Alice joins (first participant, short timeout so it doesn't block long in test)
    exit_code = main(["join", "--handle", "@Alice", "--name", "Alice Smith", "--timeout", "0.05"])
    assert exit_code == 0
    captured = capsys.readouterr()
    alice_res = json.loads(captured.out)
    assert alice_res["status"] in ("timeout", "joined")

    # Bob joins (second participant, unblocks and catches up)
    exit_code = main(["join", "--handle", "@Bob", "--name", "Bob Jones", "--timeout", "0.05"])
    assert exit_code == 0
    captured = capsys.readouterr()
    bob_res = json.loads(captured.out)
    assert "status" in bob_res
    assert "@Alice" in bob_res["active_participants"]
    assert "@Bob" in bob_res["active_participants"]

    # Test wait command for Alice
    exit_code = main(["wait", "--handle", "@Alice", "--timeout", "0.05"])
    assert exit_code == 0
    captured = capsys.readouterr()
    wait_res = json.loads(captured.out)
    assert "status" in wait_res


def test_cli_send_public_command(tmp_path: Path, capsys):
    """Test send subcommand with public message and @mentions."""
    transcript = tmp_path / "chat_send.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
    ])
    # Register both
    main(["join", "--handle", "@Alice", "--timeout", "0.05"])
    main(["join", "--handle", "@Bob", "--timeout", "0.05"])
    capsys.readouterr()

    # Alice sends public message to Bob
    exit_code = main([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob please check this out",
        "--timeout", "0.05",
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    turn_res = json.loads(captured.out)
    assert "status" in turn_res
    assert room.active_turn == "@Bob"


def test_cli_send_private_recipients_list(tmp_path: Path, capsys):
    """Test send subcommand with explicit private recipients list."""
    transcript = tmp_path / "chat_priv_list.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob", "@Charlie",
    ])
    main(["join", "--handle", "@Alice", "--timeout", "0.05"])
    main(["join", "--handle", "@Bob", "--timeout", "0.05"])
    capsys.readouterr()

    # Send private message with --private @Bob
    exit_code = main([
        "send",
        "--sender", "@Alice",
        "--content", "Confidential info",
        "--private", "@Bob",
        "--timeout", "0.05",
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    turn_res = json.loads(captured.out)
    assert "status" in turn_res
    assert room.last_posted_message is not None
    assert room.last_posted_message.is_private is True
    assert room.last_posted_message.recipients == ["@Bob"]


def test_cli_send_private_flag_and_boolean_strings(tmp_path: Path, capsys):
    """Test send subcommand with --private flag and boolean strings."""
    transcript = tmp_path / "chat_priv_flag.md"
    main([
        "init",
        "--file", str(transcript),
        "--participants", "@Alice", "@Bob",
    ])
    main(["join", "--handle", "@Alice", "--timeout", "0.05"])
    main(["join", "--handle", "@Bob", "--timeout", "0.05"])
    capsys.readouterr()

    # Send with --private flag alone
    exit_code = main([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Secret flag message",
        "--private",
        "--timeout", "0.05",
    ])
    assert exit_code == 0
    assert room.last_posted_message.is_private is True

    # Send with --private true
    exit_code = main([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Secret true string",
        "--private", "true",
        "--timeout", "0.05",
    ])
    assert exit_code == 0
    assert room.last_posted_message.is_private is True

    # Send with --private false
    exit_code = main([
        "send",
        "--sender", "@Alice",
        "--content", "@Bob Public false string",
        "--private", "false",
        "--timeout", "0.05",
    ])
    assert exit_code == 0
    assert room.last_posted_message.is_private is False

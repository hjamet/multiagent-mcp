"""Unit tests for multiagent_mcp.fast_cli zero-dependency client."""

import json
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

from multiagent_mcp import fast_cli


class FakeSocket:
    """Mock socket implementing binary framing."""

    def __init__(self, responses: list[bytes]):
        self.responses = list(responses)
        self.sent = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def settimeout(self, timeout):
        pass

    def sendall(self, data: bytes):
        self.sent.extend(data)

    def recv(self, num_bytes: int) -> bytes:
        if not self.responses:
            return b""
        chunk = self.responses.pop(0)
        return chunk


def test_fast_cli_zero_external_dependencies():
    """Verify in a clean Python subprocess that fast_cli imports 0 external dependencies."""
    cmd = [
        sys.executable,
        "-c",
        "import sys; "
        "import multiagent_mcp.fast_cli; "
        "forbidden = ['pydantic', 'mcp', 'starlette', 'uvicorn', 'rich', 'fastmcp']; "
        "loaded = [m for m in forbidden if m in sys.modules]; "
        "assert not loaded, f'Forbidden modules loaded: {loaded}'; "
        "print('ZERO_DEPENDENCIES_OK')",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "ZERO_DEPENDENCIES_OK" in res.stdout


def test_normalize_handle():
    """Test handle normalization to @Canonical format."""
    assert fast_cli.normalize_handle("Alice") == "@Alice"
    assert fast_cli.normalize_handle("@Alice") == "@Alice"
    assert fast_cli.normalize_handle("  @Bob  ") == "@Bob"
    assert fast_cli.normalize_handle(" Charlie ") == "@Charlie"


def test_fast_cli_parser():
    """Test fast_cli argument parser for all commands."""
    parser = fast_cli.build_parser()

    # init
    args_init = parser.parse_args(["init", "-f", "room.md", "-p", "Alice", "@Bob", "-t", "Topic"])
    assert args_init.command == "init"
    assert args_init.file == "room.md"
    assert args_init.participants == ["Alice", "@Bob"]
    assert args_init.topic == "Topic"

    # join
    args_join = parser.parse_args(["join", "-H", "Alice", "-n", "Alice W."])
    assert args_join.command == "join"
    assert args_join.handle == "Alice"
    assert args_join.name == "Alice W."

    # send
    args_send = parser.parse_args(["send", "-s", "@Alice", "-c", "Hello", "--private", "@Bob"])
    assert args_send.command == "send"
    assert args_send.sender == "@Alice"
    assert args_send.content == "Hello"
    assert args_send.private == ["@Bob"]

    # list
    args_list = parser.parse_args(["list"])
    assert args_list.command == "list"

    # status
    args_status = parser.parse_args(["status"])
    assert args_status.command == "status"

    # stop-daemon
    args_stop = parser.parse_args(["stop-daemon"])
    assert args_stop.command == "stop-daemon"


def test_get_daemon_port_from_port_file(tmp_path: Path):
    """Test reading daemon port from daemon.port file."""
    port_file = tmp_path / "daemon.port"
    port_file.write_text("12345\n", encoding="utf-8")

    with patch.dict("os.environ", {"MULTIAGENT_CONFIG_DIR": str(tmp_path)}):
        port = fast_cli.get_daemon_port()
        assert port == 12345


def test_get_daemon_port_from_json_file(tmp_path: Path):
    """Test reading daemon port from daemon.json file."""
    json_file = tmp_path / "daemon.json"
    json_file.write_text(json.dumps({"port": 54321}), encoding="utf-8")

    with patch.dict("os.environ", {"MULTIAGENT_CONFIG_DIR": str(tmp_path)}):
        port = fast_cli.get_daemon_port()
        assert port == 54321


def test_get_daemon_port_from_env():
    """Test reading daemon port from MULTIAGENT_DAEMON_PORT env variable."""
    with patch.dict("os.environ", {"MULTIAGENT_DAEMON_PORT": "9999"}):
        port = fast_cli.get_daemon_port()
        assert port == 9999


def test_send_request_framing():
    """Test IPC socket communication framing (4-byte length prefix)."""
    resp_data = json.dumps({"status": "ok", "value": 42}).encode("utf-8")
    resp_len = struct.pack(">I", len(resp_data))

    fake_sock = FakeSocket([resp_len, resp_data])

    with patch("socket.create_connection", return_value=fake_sock):
        result = fast_cli.send_request("test_action", {"key": "value"}, port=8888)
        assert result == {"status": "ok", "value": 42}

        # Verify sent data framing
        header = fake_sock.sent[:4]
        body = fake_sock.sent[4:]
        (length,) = struct.unpack(">I", header)
        assert length == len(body)
        sent_json = json.loads(body.decode("utf-8"))
        assert sent_json["action"] == "test_action"
        assert sent_json["key"] == "value"


def test_fast_cli_status_stopped(capsys):
    """Test status command when daemon is not running."""
    with patch("multiagent_mcp.fast_cli.get_daemon_port", return_value=None):
        code = fast_cli.main(["status"])
        assert code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "stopped"
        assert data["daemon_running"] is False


def test_fast_cli_stop_daemon_stopped(capsys):
    """Test stop-daemon command when daemon is not running."""
    with patch("multiagent_mcp.fast_cli.get_daemon_port", return_value=None):
        code = fast_cli.main(["stop-daemon"])
        assert code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "not_running"


def test_fast_cli_init_command(capsys):
    """Test init command routing to send_request."""
    with patch("multiagent_mcp.fast_cli.send_request") as mock_send:
        mock_send.return_value = {"status": "initialized", "room": "room.md"}
        code = fast_cli.main(["init", "-f", "room.md", "-p", "Alice", "Bob", "-t", "Topic"])
        assert code == 0
        mock_send.assert_called_once_with(
            "init",
            {"file": "room.md", "participants": ["@Alice", "@Bob"], "topic": "Topic"},
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "initialized"


def test_fast_cli_join_command(capsys):
    """Test join command routing to send_request."""
    with patch("multiagent_mcp.fast_cli.send_request") as mock_send:
        mock_send.return_value = {"status": "your_turn", "active_turn": "@Alice"}
        code = fast_cli.main(["join", "-H", "Alice", "-n", "Alice W."])
        assert code == 0
        mock_send.assert_called_once_with(
            "join",
            {"handle": "@Alice", "name": "Alice W."},
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "your_turn"


def test_fast_cli_send_command_public(capsys):
    """Test send command with public message."""
    with patch("multiagent_mcp.fast_cli.send_request") as mock_send:
        mock_send.return_value = {"status": "waiting", "active_turn": "@Bob"}
        code = fast_cli.main(["send", "-s", "Alice", "-c", "@Bob hi"])
        assert code == 0
        mock_send.assert_called_once_with(
            "send",
            {"sender": "@Alice", "content": "@Bob hi", "private": False},
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "waiting"


def test_fast_cli_send_command_private(capsys):
    """Test send command with private recipients."""
    with patch("multiagent_mcp.fast_cli.send_request") as mock_send:
        mock_send.return_value = {"status": "waiting", "active_turn": "@Bob"}
        code = fast_cli.main(["send", "-s", "Alice", "-c", "secret", "--private", "Bob", "@Charlie"])
        assert code == 0
        mock_send.assert_called_once_with(
            "send",
            {"sender": "@Alice", "content": "secret", "private": ["@Bob", "@Charlie"]},
        )


def test_fast_cli_list_command(capsys):
    """Test list command routing to send_request."""
    with patch("multiagent_mcp.fast_cli.send_request") as mock_send:
        mock_send.return_value = {"participants": ["@Alice", "@Bob"], "active_turn": "@Alice"}
        code = fast_cli.main(["list"])
        assert code == 0
        mock_send.assert_called_once_with("list", {})
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data["participants"]) == 2

"""Fast zero-dependency CLI client for multiagent-mcp (<20ms startup)."""

import argparse
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import time
from typing import Any, Optional, Union

# Immediate Windows UTF-8 reconfiguration
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_HOST = "127.0.0.1"


def get_config_dir() -> pathlib.Path:
    """Return the configuration directory path."""
    env_dir = os.environ.get("MULTIAGENT_CONFIG_DIR")
    if env_dir:
        return pathlib.Path(env_dir)
    return pathlib.Path.home() / ".config" / "multiagent-mcp"


def get_daemon_port() -> Optional[int]:
    """Read the daemon port from config files or environment variable."""
    env_port = os.environ.get("MULTIAGENT_DAEMON_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass

    config_dir = get_config_dir()
    port_file = config_dir / "daemon.port"
    if port_file.exists():
        try:
            return int(port_file.read_text(encoding="utf-8").strip())
        except Exception:
            pass

    json_file = config_dir / "daemon.json"
    if json_file.exists():
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "port" in data:
                return int(data["port"])
        except Exception:
            pass

    return None


def is_port_reachable(port: int, host: str = DEFAULT_HOST, timeout: float = 0.1) -> bool:
    """Check if the daemon port is reachable via socket connection."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError, socket.timeout):
        return False


def get_python_exe() -> str:
    """Find a valid python executable for running background modules."""
    exe = sys.executable
    exe_path = pathlib.Path(exe)
    if exe_path.stem.lower() not in ("python", "pythonw"):
        candidate = exe_path.parent.parent / ("python.exe" if sys.platform == "win32" else "python")
        if candidate.exists():
            return str(candidate)
        candidate2 = exe_path.parent / ("python.exe" if sys.platform == "win32" else "python")
        if candidate2.exists():
            return str(candidate2)
        import shutil
        which_py = shutil.which("python") or shutil.which("python3")
        if which_py:
            return which_py
    return exe


def spawn_daemon() -> None:
    """Start the daemon in a detached background process."""
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    python_bin = get_python_exe()

    if sys.platform == "win32":
        # 0x08000000 = CREATE_NO_WINDOW
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
        subprocess.Popen(
            [python_bin, "-m", "multiagent_mcp.daemon"],
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            [python_bin, "-m", "multiagent_mcp.daemon"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )


def ensure_daemon(max_wait_seconds: float = 5.0, host: str = DEFAULT_HOST) -> int:
    """Ensure the daemon is running and return its port number."""
    port = get_daemon_port()
    if port and is_port_reachable(port, host=host):
        return port

    # Spawn daemon if not running or unreachable
    spawn_daemon()

    start = time.time()
    while time.time() - start < max_wait_seconds:
        port = get_daemon_port()
        if port and is_port_reachable(port, host=host):
            return port
        time.sleep(0.05)

    # Final attempt
    port = get_daemon_port()
    if port and is_port_reachable(port, host=host):
        return port

    raise RuntimeError(
        f"Could not connect to multiagent-mcp daemon on {host} after {max_wait_seconds}s. "
        "Make sure multiagent_mcp.daemon can start."
    )


def _recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Read exactly num_bytes from a socket."""
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = sock.recv(num_bytes - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed prematurely while reading data")
        buf.extend(chunk)
    return bytes(buf)


def send_request(
    action: str,
    payload: Optional[dict[str, Any]] = None,
    port: Optional[int] = None,
    host: str = DEFAULT_HOST,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Send a framed JSON IPC request to the daemon and return the JSON response."""
    if port is None:
        port = ensure_daemon(host=host)

    if payload is None:
        payload = {}

    req = {"action": action, "payload": payload, **payload}
    req_bytes = json.dumps(req, ensure_ascii=False).encode("utf-8")
    header = struct.pack(">I", len(req_bytes))

    with socket.create_connection((host, port), timeout=timeout) as sock:
        if timeout is not None:
            sock.settimeout(timeout)
        else:
            sock.settimeout(None)

        sock.sendall(header + req_bytes)

        resp_header = _recv_exact(sock, 4)
        (resp_len,) = struct.unpack(">I", resp_header)
        resp_bytes = _recv_exact(sock, resp_len)
        resp_data = json.loads(resp_bytes.decode("utf-8"))
        if isinstance(resp_data, dict):
            if resp_data.get("status") == "error":
                return resp_data
            if "result" in resp_data:
                return resp_data["result"]
        return resp_data


def normalize_handle(handle: str) -> str:
    """Normalize a handle to canonical '@Handle' format."""
    clean = handle.strip()
    if not clean.startswith("@"):
        clean = f"@{clean}"
    return clean


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for fast CLI commands."""
    parser = argparse.ArgumentParser(
        prog="multiagent-mcp",
        description="Multi-Agent MCP Server for LLM turn coordination and hub management",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Command: init
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a conversation room with transcript file and participants",
    )
    init_parser.add_argument(
        "--file",
        "-f",
        type=str,
        required=True,
        help="Path to markdown transcript file",
    )
    init_parser.add_argument(
        "--participants",
        "-p",
        type=str,
        nargs="+",
        required=True,
        help="List of participant handles (e.g. @Alice @Bob @User)",
    )
    init_parser.add_argument(
        "--topic",
        "-t",
        type=str,
        default="",
        help="Initial conversation topic",
    )

    # Command: join
    join_parser = subparsers.add_parser(
        "join",
        help="Join the conversation room and wait or catch up",
    )
    join_parser.add_argument(
        "--handle",
        "-H",
        type=str,
        required=True,
        help="Participant handle (e.g. @Alice)",
    )
    join_parser.add_argument(
        "--name",
        "-n",
        type=str,
        default="",
        help="Display name",
    )

    # Command: send
    send_parser = subparsers.add_parser(
        "send",
        help="Send a message to the room and wait for next turn",
    )
    send_parser.add_argument(
        "--sender",
        "-s",
        type=str,
        required=True,
        help="Sender handle (e.g. @Alice)",
    )
    send_parser.add_argument(
        "--content",
        "-c",
        type=str,
        required=True,
        help="Message content with @mentions",
    )
    send_parser.add_argument(
        "--private",
        nargs="*",
        default=None,
        help="Private message recipients list (e.g. @Bob @Charlie) or flag",
    )

    # Command: list
    subparsers.add_parser(
        "list",
        help="List active participants, turn queue, and room status",
    )

    # Command: status
    subparsers.add_parser(
        "status",
        help="Show status of the daemon and active room",
    )

    # Command: stop-daemon
    subparsers.add_parser(
        "stop-daemon",
        aliases=["stop_daemon"],
        help="Stop the background multiagent-mcp daemon",
    )

    # Command: serve (placeholder in fast_cli parser for compatibility)
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start FastMCP server over Server-Sent Events (SSE)",
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port number to listen on (default: 8000)",
    )

    # Command: stdio (placeholder in fast_cli parser for compatibility)
    subparsers.add_parser(
        "stdio",
        help="Run FastMCP server over Standard I/O (stdio)",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Main fast CLI entrypoint."""
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "init":
        payload = {
            "file": args.file,
            "participants": [normalize_handle(p) for p in args.participants],
            "topic": args.topic,
        }
        res = send_request("init", payload)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    elif args.command == "join":
        payload = {
            "handle": normalize_handle(args.handle),
            "name": args.name,
        }
        res = send_request("join", payload)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    elif args.command == "send":
        private_val: Union[list[str], bool] = False
        if args.private is not None:
            if len(args.private) == 0:
                private_val = True
            elif len(args.private) == 1 and args.private[0].lower() in ("true", "1", "yes"):
                private_val = True
            elif len(args.private) == 1 and args.private[0].lower() in ("false", "0", "no"):
                private_val = False
            else:
                private_val = [normalize_handle(p) for p in args.private]

        payload = {
            "sender": normalize_handle(args.sender),
            "content": args.content,
            "private": private_val,
        }
        res = send_request("send", payload)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    elif args.command == "list":
        res = send_request("list", {})
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    elif args.command == "status":
        port = get_daemon_port()
        if not port or not is_port_reachable(port):
            print(json.dumps({"status": "stopped", "message": "Daemon is not running", "daemon_running": False}, indent=2, ensure_ascii=False))
            return 0
        res = send_request("status", {}, port=port)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    elif args.command in ("stop-daemon", "stop_daemon"):
        port = get_daemon_port()
        if not port or not is_port_reachable(port):
            print(json.dumps({"status": "not_running", "message": "Daemon is not running"}, indent=2, ensure_ascii=False))
            return 0
        res = send_request("stop_daemon", {}, port=port)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    elif args.command in ("serve", "stdio"):
        # For serve and stdio, if fast_cli is called directly, lazily delegate to cli
        from multiagent_mcp import cli
        return cli.main(argv)

    return 0


if __name__ == "__main__":
    sys.exit(main())

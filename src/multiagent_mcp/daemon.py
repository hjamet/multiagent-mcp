"""High-performance in-memory IPC Daemon server for MultiAgentHub room coordination.

Framing: 4-byte Big-Endian length-prefixed UTF-8 JSON payloads (< 0.2ms latency).
Zero disk polling: turn wakeups are triggered via in-memory asyncio Futures.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import struct
import sys
from typing import Any, Optional, Union

from multiagent_mcp.models import Message, Participant, TurnResult, normalize_handle
from multiagent_mcp.room import RoomManager, get_config_dir

logger = logging.getLogger("multiagent_mcp.daemon")


async def read_msg(reader: asyncio.StreamReader) -> Optional[dict]:
    """Read a length-prefixed JSON message from an asyncio StreamReader.

    Format: 4 bytes Big-Endian unsigned integer (payload length) + UTF-8 JSON bytes.
    Returns None if connection closed (EOF).
    """
    try:
        header = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionResetError, EOFError, OSError):
        return None

    length = struct.unpack(">I", header)[0]
    try:
        payload = await reader.readexactly(length)
    except (asyncio.IncompleteReadError, ConnectionResetError, EOFError, OSError):
        return None

    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as e:
        logger.error(f"Error decoding JSON payload: {e}")
        return None


async def write_msg(writer: asyncio.StreamWriter, data: Any) -> None:
    """Write a length-prefixed JSON message to an asyncio StreamWriter.

    Format: 4 bytes Big-Endian unsigned integer (payload length) + UTF-8 JSON bytes.
    """
    if hasattr(data, "model_dump"):
        raw_data = data.model_dump(mode="json")
    else:
        raw_data = data

    payload = json.dumps(raw_data, ensure_ascii=False).encode("utf-8")
    header = struct.pack(">I", len(payload))
    writer.write(header + payload)
    await writer.drain()


def get_discovery_files(config_dir: Optional[Path] = None) -> tuple[Path, Path]:
    """Get the paths for daemon discovery files (daemon.json, daemon.port)."""
    cfg = config_dir or get_config_dir()
    return cfg / "daemon.json", cfg / "daemon.port"


def get_daemon_info(config_dir: Optional[Path] = None) -> Optional[dict]:
    """Read daemon discovery info from daemon.json if present."""
    json_path, _ = get_discovery_files(config_dir)
    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def get_daemon_port(config_dir: Optional[Path] = None) -> Optional[int]:
    """Read daemon port from discovery files."""
    info = get_daemon_info(config_dir)
    if info and "port" in info:
        return int(info["port"])
    _, port_path = get_discovery_files(config_dir)
    if port_path.exists():
        try:
            return int(port_path.read_text(encoding="utf-8").strip())
        except Exception:
            return None
    return None


class DaemonServer:
    """High-speed in-memory IPC daemon coordinating turns without disk polling."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        config_dir: Optional[Path] = None,
        state_file: Optional[Union[str, Path]] = None,
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.config_dir: Path = config_dir or get_config_dir()
        self.room: RoomManager = RoomManager(state_file=state_file)
        self.waiting_clients: dict[str, list[asyncio.Future]] = {}
        self.server: Optional[asyncio.Server] = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._serving: bool = False
        self._active_connections: set[asyncio.StreamWriter] = set()

    def _save_discovery_files(self) -> None:
        """Write daemon.json and daemon.port discovery files."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        json_path, port_path = get_discovery_files(self.config_dir)
        info = {
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        json_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        port_path.write_text(str(self.port), encoding="utf-8")
        logger.info(f"Discovery files written to {json_path} and {port_path}")

    def _cleanup_discovery_files(self) -> None:
        """Remove daemon discovery files upon shutdown."""
        json_path, port_path = get_discovery_files(self.config_dir)
        for p in (json_path, port_path):
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                logger.warning(f"Failed to remove discovery file {p}: {e}")

    def _get_unread_messages(self, canonical: str) -> list[Message]:
        """Compute unread messages for participant from in-memory room state."""
        participant = self.room.participants.get(canonical)
        if not participant:
            return []
        return [
            m
            for m in self.room.messages
            if m.seq_id > participant.last_read_seq_id
            and m.sender != canonical
            and (not m.is_private or canonical in m.recipients)
        ]

    def _build_turn_result(self, canonical: str, unread: list[Message]) -> TurnResult:
        """Build TurnResult model from current in-memory state."""
        status = "your_turn" if self.room.active_turn == canonical else "message_received"
        active_list = [p.handle for p in self.room.participants.values() if p.status == "active"]
        notice = (
            f"Transcript: '{self.room.filepath}'. Interdiction formelle de consulter ce fichier sur disque."
            if self.room.filepath
            else None
        )
        return TurnResult(
            status=status,
            active_turn=self.room.active_turn,
            new_messages=unread,
            current_queue=list(self.room.turn_queue),
            active_participants=active_list,
            system_notice=notice,
        )

    def _wake_handles(self, handles: set[str]) -> None:
        """Wake all waiting client Futures registered for given handles."""
        for h in handles:
            canonical = normalize_handle(h)
            futures = self.waiting_clients.get(canonical, [])
            for fut in list(futures):
                if not fut.done():
                    fut.set_result(True)

    def _wake_all(self) -> None:
        """Wake all waiting client Futures."""
        for handles in list(self.waiting_clients.keys()):
            futures = self.waiting_clients.get(handles, [])
            for fut in list(futures):
                if not fut.done():
                    fut.set_result(True)

    async def handle_init(
        self,
        filepath: str,
        participants: Optional[list[str]] = None,
        topic: str = "",
        first_speaker: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        """Initialize room state and transcript file."""
        self.room.init_room(
            filepath=filepath,
            participants=participants,
            topic=topic,
            first_speaker=first_speaker,
            force=force,
        )
        self._wake_all()
        return {
            "status": "initialized",
            "filepath": str(self.room.filepath) if self.room.filepath else filepath,
            "topic": self.room.topic,
            "participants": [p.handle for p in self.room.participants.values()],
            "first_speaker": self.room.first_speaker,
            "message": f"Room initialized with {len(self.room.participants)} participants.",
        }

    async def handle_join(self, handle: str, name: str = "") -> dict:
        """Register participant, wait for all_joined barrier, and return turn status."""
        canonical = normalize_handle(handle)
        participant = await self.room.join_room(handle=canonical, name=name)
        self.room._load_state()

        all_joined = (
            len(self.room.participants) > 0
            and all(p.status == "active" for p in self.room.participants.values())
        )

        if not all_joined:
            # Wait until all declared participants join
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self.waiting_clients.setdefault(canonical, []).append(fut)
            try:
                await fut
            finally:
                if canonical in self.waiting_clients:
                    if fut in self.waiting_clients[canonical]:
                        self.waiting_clients[canonical].remove(fut)
                    if not self.waiting_clients[canonical]:
                        del self.waiting_clients[canonical]
        else:
            if self.room.active_turn is None and self.room.first_speaker:
                self.room.active_turn = self.room.first_speaker
                self.room._save_state()
            self._wake_all()

        # Reload state after waking or barrier lifting
        self.room._load_state()
        participant = self.room.participants.get(canonical, participant)
        unread = self._get_unread_messages(canonical)
        participant.last_read_seq_id = self.room.seq_counter
        self.room._save_state()

        status = "your_turn" if self.room.active_turn == canonical else "joined"
        active_count = sum(1 for p in self.room.participants.values() if p.status == "active")
        active_list = [p.handle for p in self.room.participants.values() if p.status == "active"]
        notice = (
            f"Transcript: '{self.room.filepath}'. Interdiction formelle de consulter ce fichier sur disque."
            if self.room.filepath
            else None
        )
        return {
            "status": status,
            "participant": participant.model_dump(mode="json"),
            "active_turn": self.room.active_turn,
            "new_messages": [m.model_dump(mode="json") for m in unread],
            "active_participants": active_list,
            "current_queue": list(self.room.turn_queue),
            "system_notice": notice or f"Joined room. Active participants: {active_count}",
        }

    async def handle_send(
        self,
        sender: str,
        content: str,
        private: Optional[Union[list[str], bool]] = False,
    ) -> dict:
        """Post a message, wake other participants, and wait for sender's next turn."""
        self.room._load_state()
        canonical_sender = normalize_handle(sender)
        if canonical_sender not in self.room.participants:
            await self.room.join_room(canonical_sender)
            self.room._load_state()

        participant = self.room.participants[canonical_sender]

        msg = await self.room.post_message(
            sender=canonical_sender,
            content=content,
            private=private,
        )
        participant.last_read_seq_id = msg.seq_id

        # Wake up relevant participants in memory
        if not msg.is_private:
            self._wake_all()
        else:
            to_wake = set(msg.recipients)
            if self.room.active_turn:
                to_wake.add(self.room.active_turn)
            self._wake_handles(to_wake)

        # Wait in loop until unread messages arrive or it becomes sender's turn
        while True:
            unread = [
                m
                for m in self.room.messages
                if m.seq_id > msg.seq_id
                and m.sender != canonical_sender
                and (not m.is_private or canonical_sender in m.recipients)
            ]

            if self.room.active_turn == canonical_sender or len(unread) > 0:
                participant.last_read_seq_id = self.room.seq_counter
                self.room._save_state()
                turn_res = self._build_turn_result(canonical_sender, unread)
                return turn_res.model_dump(mode="json")

            # Await in-memory future with zero disk polling
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self.waiting_clients.setdefault(canonical_sender, []).append(fut)

            try:
                await fut
            finally:
                if canonical_sender in self.waiting_clients:
                    if fut in self.waiting_clients[canonical_sender]:
                        self.waiting_clients[canonical_sender].remove(fut)
                    if not self.waiting_clients[canonical_sender]:
                        del self.waiting_clients[canonical_sender]

    def handle_list(self) -> dict:
        """Return full room and participant status."""
        return self.room.list_participants()

    async def handle_stop(self) -> dict:
        """Save state, signal stop event, and cleanly shut down daemon."""
        self.room._save_state()
        self._wake_all()
        self._stop_event.set()
        return {"status": "stopped", "message": "Daemon shutting down cleanly."}

    async def dispatch_request(self, req: dict) -> dict:
        """Route and execute an incoming JSON IPC request."""
        action = req.get("action") or req.get("command") or req.get("method")
        if not action:
            return {"status": "error", "error": "Missing 'action' field in request"}

        req_id = req.get("id")

        try:
            if action in ("init", "init_room", "init_conversation"):
                res = await self.handle_init(
                    filepath=req.get("filepath") or req.get("file") or "",
                    participants=req.get("participants"),
                    topic=req.get("topic", ""),
                    first_speaker=req.get("first_speaker") or req.get("firstSpeaker"),
                    force=bool(req.get("force", False)),
                )
            elif action in ("join", "join_room", "join_conversation"):
                res = await self.handle_join(
                    handle=req.get("handle") or req.get("sender") or "",
                    name=req.get("name", ""),
                )
            elif action in ("send", "post_message", "send_message"):
                res = await self.handle_send(
                    sender=req.get("sender") or req.get("handle") or "",
                    content=req.get("content", ""),
                    private=req.get("private", False),
                )
            elif action in ("get_messages", "history", "read_messages"):
                self.room._load_state()
                canonical = normalize_handle(req.get("handle") or req.get("sender") or "")
                since_seq = req.get("since_seq_id", 0)
                msgs = [
                    m.model_dump(mode="json")
                    for m in self.room.messages
                    if m.seq_id > since_seq
                    and (not m.is_private or canonical in m.recipients or canonical == m.sender)
                ]
                res = {"status": "ok", "messages": msgs}
            elif action in ("list", "list_participants"):
                res = self.handle_list()
            elif action in ("stop", "shutdown", "stop_daemon", "stop-daemon"):
                res = await self.handle_stop()
            elif action in ("ping", "status"):
                res = {
                    "status": "ok",
                    "pid": os.getpid(),
                    "active_turn": self.room.active_turn,
                    "participants_count": len(self.room.participants),
                    "message_count": len(self.room.messages),
                }
            else:
                return {
                    "status": "error",
                    "error": f"Unknown action: '{action}'",
                    "id": req_id,
                }

            response: dict[str, Any] = {"status": "ok", "result": res}
            if req_id is not None:
                response["id"] = req_id
            return response

        except Exception as e:
            logger.exception(f"Error handling action '{action}': {e}")
            err_resp: dict[str, Any] = {"status": "error", "error": str(e)}
            if req_id is not None:
                err_resp["id"] = req_id
            return err_resp

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle individual TCP client connection over length-prefixed framing."""
        self._active_connections.add(writer)
        try:
            while self._serving:
                req = await read_msg(reader)
                if req is None:
                    break
                response = await self.dispatch_request(req)
                await write_msg(writer, response)

                if req.get("action") in ("stop", "shutdown"):
                    break
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._active_connections.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self) -> None:
        """Start TCP server, bind port, and write discovery files."""
        self._serving = True
        self.server = await asyncio.start_server(
            self.handle_client,
            host=self.host,
            port=self.port,
        )

        # Retrieve actual bound port
        sockets = self.server.sockets
        if sockets:
            self.port = sockets[0].getsockname()[1]

        self._save_discovery_files()
        logger.info(f"DaemonServer listening on {self.host}:{self.port} (PID: {os.getpid()})")

    async def stop(self) -> None:
        """Stop TCP server, close connections, and clean up discovery files."""
        self._serving = False
        self._stop_event.set()

        # Wake all waiting futures to unblock pending callers
        self._wake_all()

        # Close all active client connections
        for writer in list(self._active_connections):
            try:
                writer.close()
            except Exception:
                pass

        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass

        self._cleanup_discovery_files()
        logger.info("DaemonServer stopped and discovery files cleaned up.")

    async def serve_forever(self) -> None:
        """Run daemon until stop event or interrupt."""
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()


class DaemonClient:
    """Client for high-speed TCP IPC communication with DaemonServer."""

    def __init__(self, host: str = "127.0.0.1", port: Optional[int] = None) -> None:
        self.host: str = host
        self._explicit_port: Optional[int] = port

    def _resolve_port(self) -> int:
        if self._explicit_port is not None:
            return self._explicit_port
        port = get_daemon_port()
        if port is None:
            raise ConnectionError(
                "Daemon is not running (daemon.port/daemon.json not found). "
                "Start daemon with 'python -m multiagent_mcp.daemon' first."
            )
        return port

    async def request(self, action: str, **params: Any) -> dict:
        """Send action request and return response result."""
        port = self._resolve_port()
        reader, writer = await asyncio.open_connection(self.host, port)
        try:
            req = {"action": action, **params}
            await write_msg(writer, req)
            resp = await read_msg(reader)
            if resp is None:
                raise ConnectionError("Connection closed before response received.")
            if resp.get("status") == "error":
                raise RuntimeError(resp.get("error", "Unknown daemon error"))
            return resp.get("result", resp)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def init(
        self,
        filepath: str,
        participants: list[str],
        topic: str = "",
        first_speaker: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        """Initialize room via daemon."""
        return await self.request(
            "init",
            filepath=filepath,
            participants=participants,
            topic=topic,
            first_speaker=first_speaker,
            force=force,
        )

    async def join(self, handle: str, name: str = "") -> dict:
        """Join room via daemon."""
        return await self.request("join", handle=handle, name=name)

    async def send(
        self, sender: str, content: str, private: Optional[Union[list[str], bool]] = False
    ) -> dict:
        """Post message and wait for next turn via daemon."""
        return await self.request("send", sender=sender, content=content, private=private)

    async def list_participants(self) -> dict:
        """List room state via daemon."""
        return await self.request("list")

    async def ping(self) -> dict:
        """Ping daemon."""
        return await self.request("ping")

    async def stop(self) -> dict:
        """Request daemon shutdown."""
        return await self.request("stop")


def setup_signals(server: DaemonServer, loop: asyncio.AbstractEventLoop) -> None:
    """Setup cross-platform SIGINT / SIGTERM signal handlers."""
    def _trigger_stop():
        asyncio.create_task(server.stop())

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _trigger_stop)
            except (NotImplementedError, RuntimeError):
                pass
    else:
        def _win_handler(signum: int, frame: Any) -> None:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(server.stop()))

        try:
            signal.signal(signal.SIGINT, _win_handler)
            signal.signal(signal.SIGTERM, _win_handler)
        except Exception:
            pass


async def run_daemon(host: str = "127.0.0.1", port: int = 0) -> None:
    """Run daemon server event loop."""
    server = DaemonServer(host=host, port=port)
    loop = asyncio.get_running_loop()
    setup_signals(server, loop)
    await server.serve_forever()


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint for running daemon."""
    parser = argparse.ArgumentParser(
        prog="multiagent-daemon",
        description="High-performance in-memory IPC daemon for MultiAgentHub",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=0,
        help="Port number to listen on (default: 0 for dynamic assignment)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity level",
    )

    args = parser.parse_args(argv)

    cfg_dir = get_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    log_file = cfg_dir / "daemon.log"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        handlers=handlers,
    )

    print(f"Starting MultiAgentHub IPC Daemon on {args.host}:{args.port}...")
    try:
        asyncio.run(run_daemon(host=args.host, port=args.port))
    except KeyboardInterrupt:
        print("\nDaemon interrupted by user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

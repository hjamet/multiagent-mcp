"""Command Line Interface for multiagent-mcp."""

import argparse
import asyncio
import json
import sys
from typing import Union

from multiagent_mcp.room import normalize_handle
from multiagent_mcp.server import (
    init_conversation,
    join_conversation,
    list_participants,
    mcp,
    room,
    send_message,
)


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for CLI commands."""
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
    join_parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="Timeout in seconds when waiting (default: 45.0)",
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
    send_parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="Timeout in seconds when waiting for next turn (default: 45.0)",
    )

    # Command: wait
    wait_parser = subparsers.add_parser(
        "wait",
        help="Wait for your turn or incoming messages",
    )
    wait_parser.add_argument(
        "--handle",
        "-H",
        type=str,
        required=True,
        help="Participant handle (e.g. @Alice)",
    )
    wait_parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="Timeout in seconds (default: 45.0)",
    )

    # Command: list
    subparsers.add_parser(
        "list",
        help="List active participants, turn queue, and room status",
    )

    # Command: serve (SSE transport)
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

    # Command: stdio (Standard I/O transport)
    subparsers.add_parser(
        "stdio",
        help="Run FastMCP server over Standard I/O (stdio)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "serve":
        print(f"Starting MultiAgentHub SSE server on {args.host}:{args.port}...")
        while True:
            try:
                mcp.run(transport="sse", host=args.host, port=args.port)
            except KeyboardInterrupt:
                print("\nShutting down server.")
                break
            except Exception as e:
                print(f"\n[Warning] Server restarted after exception: {e}")
                import time
                time.sleep(0.5)
        return 0

    elif args.command == "stdio":
        try:
            mcp.run(transport="stdio")
        except KeyboardInterrupt:
            pass
        return 0

    elif args.command == "init":
        res = init_conversation(
            filepath=args.file,
            participants=args.participants,
            topic=args.topic,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    elif args.command == "join":
        res = asyncio.run(
            join_conversation(
                handle=args.handle,
                name=args.name,
                timeout_seconds=args.timeout,
            )
        )
        print(res.model_dump_json(indent=2))
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
                private_val = args.private

        res = asyncio.run(
            send_message(
                sender=args.sender,
                content=args.content,
                private=private_val,
                timeout_seconds=args.timeout,
            )
        )
        print(res.model_dump_json(indent=2))
        return 0

    elif args.command == "wait":
        canonical = normalize_handle(args.handle)
        res = asyncio.run(
            room.wait_for_turn(
                agent_id=canonical,
                timeout_seconds=args.timeout,
            )
        )
        print(res.model_dump_json(indent=2))
        return 0

    elif args.command == "list":
        res = list_participants()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

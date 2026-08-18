"""Command Line Interface for multiagent-mcp."""

import sys
from typing import Any

from multiagent_mcp import fast_cli
from multiagent_mcp.fast_cli import build_parser


def __getattr__(name: str) -> Any:
    """Lazy-load server/mcp attributes when accessed directly on the module."""
    if name in (
        "mcp",
        "room",
        "init_conversation",
        "join_conversation",
        "list_participants",
        "send_message",
        "broadcast_message",
        "whisper_message",
        "wait_for_message",
    ):
        import multiagent_mcp.server as srv

        return getattr(srv, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint."""
    if argv is None:
        argv = sys.argv[1:]

    # Handle serve / stdio via lazy import of FastMCP / server
    if len(argv) > 0 and argv[0] in ("serve", "stdio"):
        parser = build_parser()
        args = parser.parse_args(argv)

        from multiagent_mcp.server import mcp

        if args.command == "serve":
            print(f"Starting MultiAgentHub SSE server on {args.host}:{args.port}...")
            while True:
                try:
                    mcp.run(transport="sse", host=args.host, port=args.port)
                    break
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

    # All other subcommands (init, join, send, list, status, stop-daemon) routed to fast_cli
    return fast_cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())

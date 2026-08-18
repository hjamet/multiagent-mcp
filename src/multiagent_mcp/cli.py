"""Command Line Interface for multiagent-mcp."""

import argparse
import sys
from multiagent_mcp.server import mcp


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for CLI commands."""
    parser = argparse.ArgumentParser(
        prog="multiagent-mcp",
        description="Multi-Agent MCP Server for LLM turn coordination and hub management",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

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
        try:
            mcp.run(transport="sse", host=args.host, port=args.port)
        except KeyboardInterrupt:
            print("\nShutting down server.")
        return 0

    elif args.command == "stdio":
        try:
            mcp.run(transport="stdio")
        except KeyboardInterrupt:
            pass
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

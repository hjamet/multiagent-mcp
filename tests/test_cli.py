"""Tests for multiagent-mcp CLI interface."""

from unittest.mock import patch
import pytest

from multiagent_mcp.cli import build_parser, main


def test_cli_parser_defaults():
    """Test default values for serve and stdio subcommands."""
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

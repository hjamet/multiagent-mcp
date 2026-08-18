"""Pytest fixtures for multiagent-mcp tests."""

from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def isolate_config_dir(tmp_path: Path, monkeypatch):
    """Isolate configuration directory and active room pointer for every test run."""
    cfg_dir = tmp_path / "mcp_isolated_config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MULTIAGENT_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("MULTIAGENT_STATE_FILE", str(cfg_dir / "default.state.json"))

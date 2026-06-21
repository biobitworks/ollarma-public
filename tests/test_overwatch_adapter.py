"""Tests for OverwatchAdapter (Phase 37 SAFE-02)."""
import os
import pytest
from unittest.mock import patch
from ollarma.overwatch_adapter import (
    OverwatchAdapter,
    OVERWATCH_ATTACHED,
    OVERWATCH_ATTACH_FAILED,
    OVERWATCH_NOT_CONFIGURED,
)


def test_not_configured_when_no_env():
    """OverwatchAdapter returns NOT_CONFIGURED when env var not set."""
    env_backup = os.environ.pop("OVERWATCH_MCP_PATH", None)
    try:
        adapter = OverwatchAdapter()
        state = adapter.attach()
        assert state == OVERWATCH_NOT_CONFIGURED
    finally:
        if env_backup is not None:
            os.environ["OVERWATCH_MCP_PATH"] = env_backup


def test_not_configured_explicit_none():
    """OverwatchAdapter with mcp_path=None returns NOT_CONFIGURED."""
    adapter = OverwatchAdapter(mcp_path=None)
    assert adapter.attach() == OVERWATCH_NOT_CONFIGURED


def test_attach_failed_when_binary_missing(tmp_path):
    """OverwatchAdapter returns ATTACH_FAILED when MCP binary path not found."""
    adapter = OverwatchAdapter(mcp_path=str(tmp_path / "nonexistent_mcp"))
    state = adapter.attach()
    assert state == OVERWATCH_ATTACH_FAILED


def test_attached_when_binary_exists(tmp_path):
    """OverwatchAdapter returns ATTACHED when MCP binary file exists."""
    mcp_bin = tmp_path / "overwatch-mcp"
    mcp_bin.write_text("#!/bin/sh\n")
    adapter = OverwatchAdapter(mcp_path=str(mcp_bin))
    state = adapter.attach()
    assert state == OVERWATCH_ATTACHED


def test_governance_check_not_configured():
    """governance_check returns state=NOT_CONFIGURED dict when not attached."""
    adapter = OverwatchAdapter(mcp_path=None)
    adapter.attach()
    result = adapter.governance_check({"run_id": "test"})
    assert result["state"] == OVERWATCH_NOT_CONFIGURED
    assert result["checked"] is False
    assert isinstance(result["refs"], list)


def test_governance_check_attached_returns_digest(tmp_path):
    """governance_check when ATTACHED returns a content-addressed digest."""
    mcp_bin = tmp_path / "overwatch-mcp"
    mcp_bin.write_text("#!/bin/sh\n")
    adapter = OverwatchAdapter(mcp_path=str(mcp_bin))
    adapter.attach()
    result = adapter.governance_check({"run_id": "test-123", "status": "ok"})
    assert result["state"] == OVERWATCH_ATTACHED
    assert result["checked"] is True
    assert len(result["refs"]) == 1
    assert isinstance(result["refs"][0], str)


def test_state_property_before_attach():
    """state property returns NOT_CONFIGURED before attach() is called."""
    adapter = OverwatchAdapter(mcp_path=None)
    assert adapter.state == OVERWATCH_NOT_CONFIGURED

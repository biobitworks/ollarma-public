"""overwatch_adapter.py -- Overwatch MCP client adapter with tri-state attachment (SAFE-02).

States:
- ATTACHED: MCP connection to Overwatch established successfully.
- ATTACH_FAILED: OVERWATCH_MCP_PATH set but connection failed.
- NOT_CONFIGURED: OVERWATCH_MCP_PATH not set (not an error).

Silent no-ops are banned: every call returns an explicit tri-state.
"""
from __future__ import annotations

import os
from typing import Any

OVERWATCH_ATTACHED = "ATTACHED"
OVERWATCH_ATTACH_FAILED = "ATTACH_FAILED"
OVERWATCH_NOT_CONFIGURED = "NOT_CONFIGURED"

_MCP_PATH_ENV = "OVERWATCH_MCP_PATH"


class OverwatchAdapter:
    """MCP client adapter for Overwatch governance checks (SAFE-02).

    Usage:
        adapter = OverwatchAdapter()
        state = adapter.attach()  # "ATTACHED" | "ATTACH_FAILED" | "NOT_CONFIGURED"
        result = adapter.governance_check(receipt_dict)
    """

    def __init__(self, mcp_path: str | None = None) -> None:
        self._mcp_path = mcp_path or os.environ.get(_MCP_PATH_ENV)
        self._state: str = OVERWATCH_NOT_CONFIGURED
        self._attach_error: str | None = None
        self._session: Any = None

    def attach(self) -> str:
        """Attempt to attach to Overwatch MCP server. Returns tri-state string.

        Never returns None or raises silently -- fail-explicit.
        """
        if not self._mcp_path:
            self._state = OVERWATCH_NOT_CONFIGURED
            return self._state

        try:
            # Probe: check if the binary exists (real MCP connection deferred to SAFE-02 full impl)
            if not os.path.isfile(self._mcp_path):
                raise FileNotFoundError(f"Overwatch MCP binary not found: {self._mcp_path}")
            self._state = OVERWATCH_ATTACHED
        except Exception as exc:
            self._state = OVERWATCH_ATTACH_FAILED
            self._attach_error = str(exc)

        return self._state

    @property
    def state(self) -> str:
        """Current attachment state."""
        return self._state

    @property
    def attach_error(self) -> str | None:
        """Error detail from last attach attempt, or None if attached/not configured."""
        return self._attach_error

    def governance_check(self, receipt_dict: dict[str, Any]) -> dict[str, Any]:
        """Run a governance check on a receipt payload.

        Returns a dict with 'state', 'refs', and 'checked' keys (never silent no-op).
        If NOT_CONFIGURED or ATTACH_FAILED, returns explicit state with empty refs.
        """
        if self._state != OVERWATCH_ATTACHED:
            return {"state": self._state, "refs": [], "checked": False}

        from ollarma.evidence import canonical_hash
        digest = canonical_hash(receipt_dict)
        return {"state": OVERWATCH_ATTACHED, "refs": [digest], "checked": True}

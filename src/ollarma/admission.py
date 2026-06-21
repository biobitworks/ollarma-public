"""admission.py -- Fail-closed recovery admission control (v4.3, Phase 44).

Before any high-value execution path runs, this module checks whether the repo
has stranded work (classified by ``ollarma.recovery.scan``). If so, the
entrypoint raises ``RecoveryRequiredError`` with the blocker code and exact
``next_fix_commands[]`` instead of proceeding silently.

Admission decision:
- ``clean``                      -> admit
- ``resume_context_available``   -> admit (informational only)
- ``stranded_worktree_detected`` -> block
- ``possible_work_loss``         -> block
- ``recovery_sweep_required``    -> block

Performance:
- A 30-second TTL cache (module-level) avoids re-running the scanner on every
  admitted request. Scan cost is ~subprocess calls to git; 30s is tight enough
  to catch a freshly stranded worktree on the next request but loose enough to
  not dominate the hot path.
- ``force=True`` or ``OLLARMA_RECOVERY_FORCE_SCAN=1`` bypass the cache.

Opt-out:
- ``OLLARMA_RECOVERY_ADMISSION=off`` bypasses the check entirely (intended for
  dry-run / test paths only; default is fail-closed).

Receipts:
- Every blocked request appends a ``RecoveryBlockReceipt`` to
  ``.ollarma/recovery_block_receipts.jsonl`` so governance can observe admission
  events.
"""
from __future__ import annotations

import datetime
import logging
import os
import pathlib
import threading
import time
from typing import Optional

import orjson
from pydantic import BaseModel, Field

from ollarma import recovery

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMISSION_ENV_VAR = "OLLARMA_RECOVERY_ADMISSION"  # "off" disables admission
FORCE_SCAN_ENV_VAR = "OLLARMA_RECOVERY_FORCE_SCAN"  # "1" bypasses cache
CACHE_TTL_SECONDS = 30.0
RECEIPTS_FILENAME = "recovery_block_receipts.jsonl"


# ---------------------------------------------------------------------------
# Exceptions + receipt model
# ---------------------------------------------------------------------------

class RecoveryRequiredError(Exception):
    """Raised when admission is blocked by a non-clean recovery state.

    Carries the structured blocker code and fix commands so callers (HTTP,
    MCP, CLI) can surface them without re-scanning.
    """

    def __init__(
        self,
        *,
        blocker_code: str,
        next_fix_commands: list[str],
        packet: dict,
        entrypoint: str,
    ) -> None:
        self.blocker_code = blocker_code
        self.next_fix_commands = list(next_fix_commands)
        self.packet = packet
        self.entrypoint = entrypoint
        super().__init__(
            f"RECOVERY_REQUIRED: {blocker_code} blocks {entrypoint!r}. "
            f"Run these to resolve: {next_fix_commands!r}"
        )

    def to_error_payload(self) -> dict:
        """Return a JSON-serializable error payload for transports."""
        return {
            "error": "RECOVERY_REQUIRED",
            "blocker_code": self.blocker_code,
            "entrypoint": self.entrypoint,
            "next_fix_commands": self.next_fix_commands,
            "state": self.packet.get("state"),
            "project_id": self.packet.get("project_id"),
            "timestamp": self.packet.get("timestamp"),
        }


class RecoveryBlockReceipt(BaseModel):
    """Append-only record of a blocked admission."""

    schema_version: int = 1
    timestamp: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    entrypoint: str
    blocker_code: str
    project_id: str
    repo_root: str
    state: str
    next_fix_commands: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, recovery.RecoveryState]] = {}


def _cache_get(repo_root: str) -> Optional[recovery.RecoveryState]:
    with _CACHE_LOCK:
        entry = _CACHE.get(repo_root)
        if entry is None:
            return None
        expires_at, state = entry
        if time.monotonic() > expires_at:
            # Stale — drop it.
            _CACHE.pop(repo_root, None)
            return None
        return state


def _cache_put(repo_root: str, state: recovery.RecoveryState) -> None:
    with _CACHE_LOCK:
        _CACHE[repo_root] = (time.monotonic() + CACHE_TTL_SECONDS, state)


def _cache_clear() -> None:
    """Clear admission cache. Exposed for tests."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Receipt persistence
# ---------------------------------------------------------------------------

def _receipts_path(repo_root: pathlib.Path) -> pathlib.Path:
    """Append-only JSONL at ``<repo_root>/.ollarma/recovery_block_receipts.jsonl``."""
    d = repo_root / ".ollarma"
    d.mkdir(parents=True, exist_ok=True)
    return d / RECEIPTS_FILENAME


def _append_block_receipt(
    repo_root: pathlib.Path, receipt: RecoveryBlockReceipt,
) -> None:
    path = _receipts_path(repo_root)
    raw = orjson.dumps(receipt.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    with open(path, "ab") as f:
        f.write(raw + b"\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def admission_enabled() -> bool:
    """Return True unless OLLARMA_RECOVERY_ADMISSION=off."""
    return os.environ.get(ADMISSION_ENV_VAR, "").lower() != "off"


def _force_scan() -> bool:
    return os.environ.get(FORCE_SCAN_ENV_VAR, "").strip() == "1"


def check_recovery(
    entrypoint: str,
    project_root: Optional[str | pathlib.Path] = None,
    *,
    force: bool = False,
    base_branch: str = "main",
) -> recovery.RecoveryState:
    """Check recovery state; raise RecoveryRequiredError if admission is blocked.

    Returns the scanned ``RecoveryState`` on admit (so callers can attach the
    current state to their receipts if useful).
    """
    if not admission_enabled():
        logger.debug("admission: OLLARMA_RECOVERY_ADMISSION=off -- bypass")
        # Still return *something* sensible; construct a synthetic clean state
        # rather than running the scanner.
        root = pathlib.Path(project_root) if project_root else pathlib.Path.cwd()
        return recovery.RecoveryState(
            project_id=root.resolve().name,
            repo_root=str(root.resolve()),
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            source="admission-disabled",
            state="clean",
            blocker_code="OK",
            resume_present=False,
            session_log_present=False,
        )

    root = pathlib.Path(project_root) if project_root else pathlib.Path.cwd()
    cache_key = str(root.resolve())

    state: Optional[recovery.RecoveryState] = None
    if not force and not _force_scan():
        state = _cache_get(cache_key)

    if state is None:
        state = recovery.scan(
            root, source=f"admission:{entrypoint}", base_branch=base_branch,
        )
        _cache_put(cache_key, state)

    if recovery.is_admission_blocking(state):
        receipt = RecoveryBlockReceipt(
            entrypoint=entrypoint,
            blocker_code=state.blocker_code,
            project_id=state.project_id,
            repo_root=state.repo_root,
            state=state.state,
            next_fix_commands=state.next_fix_commands,
        )
        try:
            _append_block_receipt(pathlib.Path(state.repo_root), receipt)
        except OSError as exc:
            # Receipt persistence failure must NOT mask the original block.
            logger.warning("admission: block-receipt write failed: %s", exc)

        raise RecoveryRequiredError(
            blocker_code=state.blocker_code,
            next_fix_commands=state.next_fix_commands,
            packet=state.model_dump(mode="json"),
            entrypoint=entrypoint,
        )

    return state

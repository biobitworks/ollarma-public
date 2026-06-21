"""sibling_reader.py — Fail-closed allowlisted sibling-project file reader."""
from __future__ import annotations
import datetime as dt
import pathlib
from typing import Any
from pydantic import BaseModel

SIBLING_NOT_ALLOWLISTED = "SIBLING_NOT_ALLOWLISTED"
SIBLING_CONTENT_BLOCKED = "SIBLING_CONTENT_BLOCKED"


class SiblingReadReceipt(BaseModel):
    """Per-read receipt for an allowlisted sibling-project file read."""
    model_config = {"frozen": True}

    path: str
    content: str
    namespace: str
    project: str | None = None
    read_at: str


def read_sibling_path(
    path: str | pathlib.Path,
    *,
    namespace: str,
    allowlisted_roots: list[pathlib.Path],
    project: str | None = None,
    gate: "Any | None" = None,
) -> SiblingReadReceipt:
    """Read a sibling-project file if it is under an allowlisted root.

    Raises ValueError(SIBLING_NOT_ALLOWLISTED) if path is outside the allowlist.
    Raises ValueError with OS error on missing/unreadable file.
    Never returns None — always fails closed.
    """
    if not allowlisted_roots:
        raise ValueError(SIBLING_NOT_ALLOWLISTED)

    resolved = pathlib.Path(path).resolve()
    resolved_roots = [r.resolve() for r in allowlisted_roots]
    allowed = any(
        _is_under_root(resolved, root) for root in resolved_roots
    )
    if not allowed:
        raise ValueError(SIBLING_NOT_ALLOWLISTED)

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"SIBLING_READ_ERROR: {exc}") from exc

    # Layer 1: Antigence gate validation of returned content (SAFE-03)
    if gate is not None:
        tristate, _ = gate.validate(content)
        if tristate == "block":
            raise ValueError(SIBLING_CONTENT_BLOCKED)

    return SiblingReadReceipt(
        path=str(resolved),
        content=content,
        namespace=namespace,
        project=project,
        read_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _is_under_root(resolved: pathlib.Path, root: pathlib.Path) -> bool:
    """Return True if resolved path is under root (safe symlink-aware check)."""
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False

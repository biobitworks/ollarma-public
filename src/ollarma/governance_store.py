"""governance_store.py -- Content-addressed governance payload store (SAFE-04).

Payloads stored at .ollarma/governance/<digest>/payload.json.
64KB cap enforced on each payload.
"""
from __future__ import annotations

import pathlib
import re
from typing import Any

import orjson

from ollarma.evidence import canonical_hash

MAX_PAYLOAD_BYTES = 64 * 1024  # 64KB cap (SAFE-04)
GOVERNANCE_STORE_DIR = ".ollarma/governance"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_digest(digest: str) -> str:
    """Validate that digest is a 64-char lowercase hex string (SHA-256).

    Prevents path traversal via crafted digest values like '../../etc/passwd'.
    """
    if not _DIGEST_RE.match(digest):
        raise ValueError(f"Invalid governance digest: must be 64-char hex, got {digest!r}")
    return digest


class GovernancePayloadTooLargeError(ValueError):
    """Raised when a governance payload exceeds the 64KB cap."""


class GovernanceStore:
    """Content-addressed store for governance payloads (SAFE-04).

    Each payload is stored at <base_path>/<digest>/payload.json where
    digest = canonical_hash(payload). Receipts reference digests, not payloads.
    """

    def __init__(self, base_path: str | pathlib.Path | None = None) -> None:
        self._base = pathlib.Path(base_path or GOVERNANCE_STORE_DIR)

    def store(self, payload: dict[str, Any]) -> str:
        """Content-address and store a governance payload.

        Returns the digest string. Raises GovernancePayloadTooLargeError if >64KB.
        Idempotent: storing the same payload twice returns the same digest.
        """
        raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise GovernancePayloadTooLargeError(
                f"Governance payload is {len(raw)} bytes, exceeds {MAX_PAYLOAD_BYTES}B cap"
            )
        digest = canonical_hash(payload)
        dest = self._base / digest
        dest.mkdir(parents=True, exist_ok=True)
        payload_path = dest / "payload.json"
        if not payload_path.exists():
            payload_path.write_bytes(raw)
        return digest

    def load(self, digest: str) -> dict[str, Any]:
        """Load a stored governance payload by digest. Raises FileNotFoundError if missing."""
        digest = _validate_digest(digest)
        payload_path = self._base / digest / "payload.json"
        if not payload_path.exists():
            raise FileNotFoundError(f"Governance payload not found: {digest}")
        return orjson.loads(payload_path.read_bytes())

    def exists(self, digest: str) -> bool:
        """Return True if a payload with this digest exists in the store."""
        digest = _validate_digest(digest)
        return (self._base / digest / "payload.json").exists()

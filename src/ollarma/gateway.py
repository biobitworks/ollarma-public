"""gateway.py -- Frontier-gateway primitive layer (Phase 57).

Phase 57 scope: schema + hash-chained split-stream receipt store.

Exports
-------
- ``FrontierReceipt``         -- Pydantic, frozen, schema_version=1. Emitted for
  every gateway call (including disabled + dry-run paths). Extends the
  ``EscalationReceipt`` pattern (see ``ollarma.escalation``).
- ``GatewayAdmissionEntry``   -- Audit-trail record for every admission decision
  (accept/reject/disabled/dry_run). Persisted to ``admissions.jsonl``.
- ``GatewayReasonCode``       -- Gateway-specific str-Enum extension of
  ``escalation.ReasonCode`` (gateway codes live here to keep escalation.py
  stable per invariant I-03).
- ``GatewayReceiptStore``     -- Hash-chained JSONL store under
  ``.ollarma/gateway/`` with two independent streams: ``admissions.jsonl`` and
  ``receipts.jsonl``.
- ``GatewayStoreError``       -- Raised on chain corruption or required-field
  violations. Never bare-caught.

Design bindings to ``.planning/phases/57-gateway-core-foundations/57-CONTEXT.md``
- D-01..D-05 receipt store layout
- D-06..D-09 FrontierReceipt v1 field set
- D-13..D-15 dry-run semantics (this module stores the ``dry_run`` flag; the
  dry-run logic itself lands in 57-02)

Hash-chain primitive
--------------------
``ollarma.evidence.canonical_hash`` is the SINGLE hashing entry point (Pitfall
6). This module does not introduce a second hashing function.

Atomic-append contract
----------------------
Appends use ``os.O_APPEND | os.O_WRONLY | os.O_CREAT`` with a single
``os.write``. Atomicity is guaranteed IN-PROCESS by ``self._lock`` —
concurrent threads will never interleave. Cross-process atomicity is
NOT guaranteed on macOS because PIPE_BUF is 512 bytes and records can
exceed 600 bytes. On Linux, PIPE_BUF is 4096 bytes and records fit,
but the operator contract for v5.0 is single-process (per 57-CONTEXT.md
deferred block: "Multi-tenant gateway ... out of scope for all of v5.0").
Do not share a ``.ollarma/gateway/`` directory between processes until
a future phase adds fcntl/flock serialization.

Failure taxonomy
----------------
No ``except Exception`` in this module (DEBT-10, v4.5 retro-review). Narrow
types only: ``OSError``, ``pydantic.ValidationError``, ``orjson.JSONDecodeError``.
Any other exception propagates.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import threading
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

import orjson
import pydantic
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from ollarma.escalation import ReasonCode
from ollarma.evidence import GENESIS_PARENT_HASH, canonical_hash


__all__ = [
    "FrontierReceipt",
    "GatewayAdmissionEntry",
    "GatewayReasonCode",
    "GatewayReceiptStore",
    "GatewayStoreError",
]


# ---------------------------------------------------------------------------
# Gateway-specific ReasonCode extension (D-09)
# ---------------------------------------------------------------------------

class GatewayReasonCode(str, Enum):
    """Gateway-specific reason codes.

    Kept SEPARATE from ``escalation.ReasonCode`` so that escalation.py stays
    byte-identical across the v5.0 milestone (invariant I-03). A
    ``FrontierReceipt.reason_code`` field accepts either enum's values.
    """

    GATEWAY_DISABLED = "GATEWAY_DISABLED"
    DRY_RUN = "DRY_RUN"
    REJECT_INVALID_RECEIPT = "REJECT_INVALID_RECEIPT"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_CONTEXT_EXCEEDED = "PROVIDER_CONTEXT_EXCEEDED"
    PROVIDER_NETWORK_TIMEOUT = "PROVIDER_NETWORK_TIMEOUT"
    # Phase 58 admission-stage codes (D-58-04). Rate-cap codes live here in
    # 58-01 so the enum stays stable when 58-02 lands; both 58-01 and 58-02
    # import these from the same module.
    PROJECT_NOT_ALLOWED = "PROJECT_NOT_ALLOWED"
    VIRTUAL_KEY_UNKNOWN = "VIRTUAL_KEY_UNKNOWN"
    VIRTUAL_KEY_KEYCHAIN_MISS = "VIRTUAL_KEY_KEYCHAIN_MISS"
    RATE_CAP_REQ_PER_MIN_EXCEEDED = "RATE_CAP_REQ_PER_MIN_EXCEEDED"
    RATE_CAP_TOKENS_PER_DAY_EXCEEDED = "RATE_CAP_TOKENS_PER_DAY_EXCEEDED"
    GATEWAY_NOT_CONFIGURED = "GATEWAY_NOT_CONFIGURED"


_VALID_REASON_CODES: frozenset[str] = frozenset(
    {e.value for e in ReasonCode} | {e.value for e in GatewayReasonCode}
)


def _utc_now_iso() -> str:
    """Match EscalationReceipt timestamp format exactly (scribe parser parity)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# FrontierReceipt (D-06..D-09, D-15)
# ---------------------------------------------------------------------------

class FrontierReceipt(BaseModel):
    """Structured receipt emitted for every gateway call.

    One receipt per call in ALL postures: succeeded, failed, dry_run,
    disabled. The hash-chain linkage (``parent_hash``, ``receipt_hash``) is
    populated by ``GatewayReceiptStore.append_receipt`` on write — constructed
    receipts carry GENESIS_PARENT_HASH and an empty ``receipt_hash`` until
    they enter the chain.
    """

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    # Provenance link -- D-06
    escalation_receipt_id: str
    # Cross-stream chain linkage (Phase 63 trace CLI invariant):
    # equals the ``receipt_hash`` of the ``GatewayAdmissionEntry`` that admitted
    # this call. NOT ``canonical_hash(escalation_receipt.model_dump())`` — that
    # content hash is recorded on the admission entry itself
    # (``GatewayAdmissionEntry.escalation_receipt_content_hash``). Walking
    # admissions.jsonl -> receipts.jsonl uses
    # ``admission.receipt_hash == FrontierReceipt.admission_receipt_hash``.
    admission_receipt_hash: str

    # Provider identity -- D-07
    provider: str
    model_id: str
    provider_request_id: str | None = None

    # Usage + cost -- D-08
    prompt_tokens: int = 0
    response_tokens: int = 0
    cost_usd: Decimal = Field(default=Decimal("0"))
    latency_ms: int = 0

    # Outcome -- D-09
    status: Literal["succeeded", "failed", "dry_run", "disabled"]
    reason_code: str | None = None

    # Truncation trio -- D-09
    context_truncated: bool = False
    original_tokens: int | None = None
    truncated_tokens: int | None = None

    # Housekeeping
    schema_version: int = 1
    created_at: str = Field(default_factory=_utc_now_iso)

    # Chain linkage (populated by GatewayReceiptStore on append)
    parent_hash: str = GENESIS_PARENT_HASH
    receipt_hash: str = ""

    # Dry-run flag per D-15 (recorded on receipt itself so a dry-run against a
    # disabled gateway still carries the explicit operator intent)
    dry_run: bool = False

    # -- Validators -----------------------------------------------------------

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _coerce_cost_usd(cls, value: Any) -> Decimal:
        """Accept Decimal, str, int, or float-as-string. Never accept raw float."""
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, str):
            try:
                return Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"cost_usd must be a decimal string: {value!r}") from exc
        # Deliberately reject raw float to prevent Decimal(0.1) -> 0.1000000000...
        raise ValueError(
            f"cost_usd must be Decimal | str | int, got {type(value).__name__}"
        )

    @field_serializer("cost_usd")
    def _serialize_cost_usd(self, value: Decimal) -> str:
        # Stable string serialization so canonical_hash bytes are deterministic.
        return str(value)

    @field_validator("reason_code")
    @classmethod
    def _validate_reason_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in _VALID_REASON_CODES:
            raise ValueError(
                f"reason_code {value!r} is not a member of ReasonCode or "
                f"GatewayReasonCode"
            )
        return value

    @model_validator(mode="after")
    def _validate_truncation_trio(self) -> "FrontierReceipt":
        if self.context_truncated:
            if self.original_tokens is None or self.truncated_tokens is None:
                raise ValueError(
                    "context_truncated=True requires both original_tokens and "
                    "truncated_tokens to be set"
                )
        else:
            if self.original_tokens is not None or self.truncated_tokens is not None:
                raise ValueError(
                    "context_truncated=False requires original_tokens and "
                    "truncated_tokens to be None"
                )
        return self


# ---------------------------------------------------------------------------
# GatewayAdmissionEntry (audit stream)
# ---------------------------------------------------------------------------

class GatewayAdmissionEntry(BaseModel):
    """Audit-trail entry for every admission decision.

    Written to ``.ollarma/gateway/admissions.jsonl``. Independent hash chain
    from the receipts stream (D-01, D-02). Linked to a ``FrontierReceipt`` via
    ``escalation_receipt_id`` so the Phase 63 trace CLI can reconstruct the
    full path.
    """

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    admission_id: str
    escalation_receipt_id: str
    # Content hash of the input EscalationReceipt payload
    # (``canonical_hash(escalation_receipt.model_dump())``). NOT the admission's
    # own chain hash (that is ``receipt_hash`` below). Used for tamper-evident
    # binding of this admission to the exact ER bytes the caller submitted.
    escalation_receipt_content_hash: str
    outcome: Literal["accept", "reject", "disabled", "dry_run"]
    reason_code: str | None = None
    reason_detail: str = ""
    project: str
    schema_version: int = 1
    created_at: str = Field(default_factory=_utc_now_iso)
    parent_hash: str = GENESIS_PARENT_HASH
    receipt_hash: str = ""

    @field_validator("reason_code")
    @classmethod
    def _validate_reason_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in _VALID_REASON_CODES:
            raise ValueError(
                f"reason_code {value!r} is not a member of ReasonCode or "
                f"GatewayReasonCode"
            )
        return value


# ---------------------------------------------------------------------------
# GatewayReceiptStore (D-01..D-05)
# ---------------------------------------------------------------------------

class GatewayStoreError(Exception):
    """Raised when the gateway receipt store detects chain corruption or
    schema violations. Never bare-caught."""


_GATEWAY_DIR = ".ollarma/gateway"
_ADMISSIONS_FILENAME = "admissions.jsonl"
_RECEIPTS_FILENAME = "receipts.jsonl"
_StreamName = Literal["admissions", "receipts"]


def _hash_payload(payload: dict[str, Any]) -> str:
    """Canonical hash of a receipt payload with receipt_hash field stripped."""
    body = {k: v for k, v in payload.items() if k != "receipt_hash"}
    return canonical_hash(body)


class GatewayReceiptStore:
    """Split-stream, hash-chained JSONL store.

    Two independent streams:
    - ``.ollarma/gateway/admissions.jsonl``
    - ``.ollarma/gateway/receipts.jsonl``

    Each stream's first line has ``parent_hash=GENESIS_PARENT_HASH``; every
    subsequent line's ``parent_hash`` equals the previous line's
    ``receipt_hash``. ``verify_chain(stream)`` walks the file and recomputes
    every hash; any on-disk mutation breaks verification (tamper evident).

    Append-only forever (D-04). ``rotate`` / ``compact`` are explicit non-goals
    for v5.0 (deferred block in 57-CONTEXT.md).
    """

    def __init__(self, repo_root: pathlib.Path) -> None:
        self._repo_root = pathlib.Path(repo_root).resolve()
        self._gateway_dir = self._repo_root / _GATEWAY_DIR
        # Serialize in-process writes per store instance. This prevents two
        # threads from reading the same tail hash and writing siblings with
        # the same parent_hash (DEBT-14: module-level state without a lock).
        self._lock = threading.Lock()

    # -- Path helpers ---------------------------------------------------------

    def _stream_path(self, stream: _StreamName) -> pathlib.Path:
        if stream == "admissions":
            name = _ADMISSIONS_FILENAME
        elif stream == "receipts":
            name = _RECEIPTS_FILENAME
        else:
            raise GatewayStoreError(f"unknown stream: {stream!r}")
        return self._gateway_dir / name

    def _ensure_dir(self) -> None:
        self._gateway_dir.mkdir(parents=True, exist_ok=True)

    # -- Tail hash read (chain continuation) ---------------------------------

    def _current_tail_hash(self, path: pathlib.Path) -> str:
        """Return the last-line receipt_hash, or GENESIS for an empty/missing file."""
        if not path.exists():
            return GENESIS_PARENT_HASH
        last_line: bytes | None = None
        try:
            with open(path, "rb") as f:
                for raw in f:
                    stripped = raw.rstrip(b"\n")
                    if stripped:
                        last_line = stripped
        except OSError as exc:
            raise GatewayStoreError(f"failed to read {path}: {exc}") from exc

        if last_line is None:
            return GENESIS_PARENT_HASH

        try:
            record = orjson.loads(last_line)
        except orjson.JSONDecodeError as exc:
            raise GatewayStoreError(
                f"tail line is not valid JSON in {path.name}: {exc}"
            ) from exc

        tail = record.get("receipt_hash")
        if not isinstance(tail, str) or not tail:
            raise GatewayStoreError(
                f"tail line missing populated receipt_hash in {path.name}"
            )
        return tail

    # -- Atomic append --------------------------------------------------------

    def _atomic_append_line(self, path: pathlib.Path, line_bytes: bytes) -> None:
        """Single-syscall append of ``line_bytes + b"\\n"`` to ``path``.

        Uses ``O_APPEND | O_WRONLY | O_CREAT`` so the kernel handles the
        position atomically (no seek race). Writes below PIPE_BUF are atomic
        on POSIX under O_APPEND, which is our invariant: "either the full line
        lands or none of it lands". The caller's threading.Lock serializes
        tail-hash reads with writes so chain continuity is preserved in-process.
        """
        if b"\n" in line_bytes:
            raise GatewayStoreError(
                "serialized record contains an embedded newline; canonical JSON "
                "should never include one"
            )
        payload = line_bytes + b"\n"
        flags = os.O_APPEND | os.O_WRONLY | os.O_CREAT
        # mode 0o644 -- readable by operator, writable only by owner
        fd = os.open(path, flags, 0o644)
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise GatewayStoreError(
                    f"short write to {path.name}: {written} of {len(payload)} bytes"
                )
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- Admissions stream ----------------------------------------------------

    def append_admission(
        self, entry: GatewayAdmissionEntry,
    ) -> GatewayAdmissionEntry:
        """Append one admission entry; return the materialized entry with
        parent_hash + receipt_hash populated."""
        return self._append(entry, stream="admissions")

    def append_receipt(self, receipt: FrontierReceipt) -> FrontierReceipt:
        """Append one FrontierReceipt; return the materialized receipt with
        parent_hash + receipt_hash populated."""
        return self._append(receipt, stream="receipts")

    def _append(
        self,
        entry: FrontierReceipt | GatewayAdmissionEntry,
        *,
        stream: _StreamName,
    ) -> Any:
        self._ensure_dir()
        path = self._stream_path(stream)
        with self._lock:
            parent = self._current_tail_hash(path)
            # Rebuild with parent_hash set, then compute receipt_hash over the
            # payload excluding receipt_hash itself (run_ledger pattern).
            payload = entry.model_dump(mode="json")
            payload["parent_hash"] = parent
            payload["receipt_hash"] = ""
            computed = _hash_payload(payload)
            payload["receipt_hash"] = computed

            # Re-validate through the model so the returned object carries
            # the exact serialization the file holds.
            model_cls = type(entry)
            try:
                materialized = model_cls.model_validate(payload)
            except pydantic.ValidationError as exc:
                raise GatewayStoreError(
                    f"materialized {stream} entry failed re-validation: {exc}"
                ) from exc

            line_bytes = orjson.dumps(
                materialized.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS,
            )
            try:
                self._atomic_append_line(path, line_bytes)
            except OSError as exc:
                raise GatewayStoreError(
                    f"failed to append to {path}: {exc}"
                ) from exc

            return materialized

    # -- Load + verify --------------------------------------------------------

    def _iter_lines(self, path: pathlib.Path) -> list[bytes]:
        if not path.exists():
            return []
        try:
            with open(path, "rb") as f:
                return [line.rstrip(b"\n") for line in f if line.rstrip(b"\n")]
        except OSError as exc:
            raise GatewayStoreError(f"failed to read {path}: {exc}") from exc

    def load_admissions(self) -> list[GatewayAdmissionEntry]:
        return self._load(stream="admissions", model_cls=GatewayAdmissionEntry)

    def load_receipts(self) -> list[FrontierReceipt]:
        return self._load(stream="receipts", model_cls=FrontierReceipt)

    def _load(self, *, stream: _StreamName, model_cls: type) -> list:
        path = self._stream_path(stream)
        lines = self._iter_lines(path)
        out: list = []
        for i, raw in enumerate(lines, start=1):
            try:
                record = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                snippet = raw[:200].decode("utf-8", errors="replace")
                raise GatewayStoreError(
                    f"{path.name} line {i} is not valid JSON: {exc}; "
                    f"first 200 chars: {snippet!r}"
                ) from exc
            try:
                out.append(model_cls.model_validate(record))
            except pydantic.ValidationError as exc:
                snippet = raw[:200].decode("utf-8", errors="replace")
                raise GatewayStoreError(
                    f"{path.name} line {i} failed schema validation: {exc}; "
                    f"first 200 chars: {snippet!r}"
                ) from exc
        return out

    def verify_chain(self, stream: _StreamName) -> str:
        """Walk the stream and recompute every hash.

        Returns the tail hash on success (or GENESIS_PARENT_HASH for an empty
        stream). Raises GatewayStoreError on any break.
        """
        path = self._stream_path(stream)
        lines = self._iter_lines(path)
        if not lines:
            return GENESIS_PARENT_HASH

        parent = GENESIS_PARENT_HASH
        for i, raw in enumerate(lines, start=1):
            try:
                record = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                raise GatewayStoreError(
                    f"{path.name} line {i} is not valid JSON: {exc}"
                ) from exc

            if record.get("parent_hash") != parent:
                raise GatewayStoreError(
                    f"{path.name} line {i}: parent_hash mismatch "
                    f"(expected {parent[:16]}..., got "
                    f"{str(record.get('parent_hash'))[:16]}...)"
                )

            stored_hash = record.get("receipt_hash")
            if not isinstance(stored_hash, str) or not stored_hash:
                raise GatewayStoreError(
                    f"{path.name} line {i}: receipt_hash missing"
                )

            expected = _hash_payload(record)
            if expected != stored_hash:
                raise GatewayStoreError(
                    f"{path.name} line {i}: receipt_hash mismatch "
                    f"(expected {expected[:16]}..., got {stored_hash[:16]}...)"
                )
            parent = stored_hash

        return parent

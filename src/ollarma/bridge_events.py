"""Durable localhost bridge events for real-time backbone consumers.

RTB-01 adds a small append-only event spine that local consumers can replay
without scraping process logs or model output. It is deliberately narrow:
typed events, redacted payloads, stable hashes, JSONL storage, and bounded
replay. It does not implement token-level streaming or long-lived live tailing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION: Literal[1] = 1
REDACTED = "<redacted>"


class BridgeEventType(str, Enum):
    CHAT_STARTED = "chat_started"
    CHAT_DONE = "chat_done"
    ROUTE_STARTED = "route_started"
    ROUTE_DONE = "route_done"
    KB_HIT = "kb_hit"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    CHAT_DELTA = "chat_delta"
    LANE_TRANSITION = "lane_transition"
    ANTIGENCE_REVIEW_STARTED = "antigence_review_started"
    ANTIGENCE_VERDICT = "antigence_verdict"
    DISTILLATION_DONE = "distillation_done"


BridgeSource = Literal[
    "chat",
    "route",
    "kb",
    "workflow",
    "swarm",
    "antigence",
    "distillation",
]


class BridgeEvent(BaseModel):
    """Typed replay event for the localhost bridge spine."""

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: str
    run_id: str
    parent_event_id: str | None = None
    event_type: BridgeEventType
    created_at: str
    source: BridgeSource
    payload_hash: str
    payload: dict[str, Any]
    receipt_refs: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class BridgeEventList(BaseModel):
    """Bounded event replay result."""

    schema_version: Literal[1] = SCHEMA_VERSION
    events: tuple[BridgeEvent, ...] = ()
    after_event_id: str | None = None
    after_event_id_found: bool = True
    limit: int = 100

    model_config = ConfigDict(frozen=True)


class BridgeEventStoreError(RuntimeError):
    """Raised when append-only bridge event storage is inconsistent."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _key_needs_redaction(key: str, value: Any) -> bool:
    lowered = key.lower()
    if lowered in {"authorization", "api_key", "token", "password", "secret"}:
        return True
    if lowered.endswith("_token") or lowered.endswith("_password") or lowered.endswith("_secret"):
        return True
    if "api_key" in lowered:
        return True
    if lowered in {"raw_request", "raw_response", "provider_raw_request", "provider_raw_response"}:
        return True
    if lowered == "keychain_service":
        text = str(value).lower()
        return any(marker in text for marker in ("token", "key", "secret", "password", "credential", "auth"))
    return False


def redact_payload(value: Any, *, parent_key: str | None = None) -> Any:
    """Recursively redact secret-like values while preserving payload shape."""

    if parent_key and _key_needs_redaction(parent_key, value):
        return REDACTED
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            redacted[key_text] = REDACTED if _key_needs_redaction(key_text, child) else redact_payload(child, parent_key=key_text)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_payload(child, parent_key=parent_key) for child in value]
    return value


def create_bridge_event(
    *,
    event_type: BridgeEventType | str,
    source: BridgeSource,
    payload: dict[str, Any] | None = None,
    run_id: str | None = None,
    parent_event_id: str | None = None,
    receipt_refs: tuple[str, ...] = (),
    created_at: str | None = None,
) -> BridgeEvent:
    """Create a redacted bridge event with stable payload and event hashes."""

    redacted_payload = redact_payload(payload or {})
    payload_hash = stable_hash(redacted_payload)
    body = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or str(uuid.uuid4()),
        "parent_event_id": parent_event_id,
        "event_type": BridgeEventType(event_type).value,
        "created_at": created_at or _utc_now(),
        "source": source,
        "payload_hash": payload_hash,
        "payload": redacted_payload,
        "receipt_refs": tuple(receipt_refs),
    }
    event_id = stable_hash(body)
    return BridgeEvent(event_id=event_id, **body)


class BridgeEventStore:
    """Append-only JSONL store for bridge events."""

    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / ".ollarma" / "bridge" / "events.jsonl"

    def append(self, event: BridgeEvent) -> BridgeEvent:
        """Append *event*, rejecting event-id collisions with different content."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json(event.model_dump(mode="json"))
        if "\n" in line:
            raise BridgeEventStoreError("bridge event serialization contains newline")
        if self.path.exists():
            for existing in self._iter_events():
                if existing.event_id == event.event_id:
                    if existing.model_dump(mode="json") == event.model_dump(mode="json"):
                        return existing
                    raise BridgeEventStoreError(f"event_id collision: {event.event_id}")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return event

    def list_events(self, *, after_event_id: str | None = None, limit: int = 100) -> BridgeEventList:
        """Return events after *after_event_id* in stable file order."""

        bounded_limit = max(0, min(int(limit), 500))
        if not self.path.exists():
            return BridgeEventList(
                events=(),
                after_event_id=after_event_id,
                after_event_id_found=after_event_id is None,
                limit=bounded_limit,
            )

        events = list(self._iter_events())
        if after_event_id is None:
            return BridgeEventList(
                events=tuple(events[-bounded_limit:] if bounded_limit else ()),
                after_event_id=after_event_id,
                after_event_id_found=True,
                limit=bounded_limit,
            )
        start = 0
        found = False
        if after_event_id is not None:
            for idx, event in enumerate(events):
                if event.event_id == after_event_id:
                    start = idx + 1
                    found = True
                    break
            if not found:
                return BridgeEventList(
                    events=(),
                    after_event_id=after_event_id,
                    after_event_id_found=False,
                    limit=bounded_limit,
                )
        return BridgeEventList(
            events=tuple(events[start : start + bounded_limit]),
            after_event_id=after_event_id,
            after_event_id_found=found,
            limit=bounded_limit,
        )

    def _iter_events(self) -> tuple[BridgeEvent, ...]:
        rows: list[BridgeEvent] = []
        if not self.path.exists():
            return ()
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                    rows.append(BridgeEvent.model_validate(raw))
                except Exception as exc:  # noqa: BLE001
                    raise BridgeEventStoreError(f"{self.path}:{line_number}: invalid bridge event: {exc}") from exc
        return tuple(rows)

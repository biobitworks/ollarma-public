"""Tests for the RTB-01 bridge event schema and append-only store."""
from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from ollarma.bridge_events import (
    REDACTED,
    BridgeEvent,
    BridgeEventStore,
    BridgeEventStoreError,
    BridgeEventType,
    create_bridge_event,
    redact_payload,
)


def test_bridge_event_accepts_valid_event() -> None:
    event = create_bridge_event(
        event_type=BridgeEventType.CHAT_STARTED,
        source="chat",
        payload={"message_preview": "hello"},
        run_id="run-1",
        created_at="2026-05-31T15:20:00Z",
    )

    assert event.schema_version == 1
    assert event.event_id
    assert event.payload_hash
    assert event.event_type == BridgeEventType.CHAT_STARTED


def test_bridge_event_rejects_unknown_event_type() -> None:
    event = create_bridge_event(event_type="chat_started", source="chat", payload={})
    data = event.model_dump(mode="json") | {"event_type": "unknown"}

    with pytest.raises(ValidationError):
        BridgeEvent.model_validate(data)


def test_redaction_recurses_and_preserves_shape() -> None:
    payload = {
        "Authorization": "Bearer abc",
        "nested": {
            "api_key": "secret",
            "items": [{"token": "tok"}, {"safe": "value"}],
            "keychain_service": "semantic-scholar-token",
        },
        "safe": "keep",
    }

    redacted = redact_payload(payload)

    assert redacted["Authorization"] == REDACTED
    assert redacted["nested"]["api_key"] == REDACTED
    assert redacted["nested"]["items"][0]["token"] == REDACTED
    assert redacted["nested"]["items"][1]["safe"] == "value"
    assert redacted["nested"]["keychain_service"] == REDACTED
    assert redacted["safe"] == "keep"


def test_payload_hash_changes_when_redacted_payload_changes() -> None:
    first = create_bridge_event(event_type="chat_started", source="chat", payload={"safe": "a"})
    second = create_bridge_event(event_type="chat_started", source="chat", payload={"safe": "b"})

    assert first.payload_hash != second.payload_hash


def test_store_append_and_replay_order(tmp_path: pathlib.Path) -> None:
    store = BridgeEventStore(tmp_path)
    first = create_bridge_event(event_type="chat_started", source="chat", payload={"n": 1}, run_id="r")
    second = create_bridge_event(event_type="chat_done", source="chat", payload={"n": 2}, run_id="r")

    store.append(first)
    store.append(second)

    assert store.path.read_text(encoding="utf-8").count("\n") == 2
    replay = store.list_events()
    assert [event.event_id for event in replay.events] == [first.event_id, second.event_id]
    after_first = store.list_events(after_event_id=first.event_id)
    assert after_first.after_event_id_found is True
    assert [event.event_id for event in after_first.events] == [second.event_id]


def test_store_unknown_after_id_returns_empty_without_replay(tmp_path: pathlib.Path) -> None:
    store = BridgeEventStore(tmp_path)
    store.append(create_bridge_event(event_type="chat_started", source="chat", payload={"n": 1}))

    replay = store.list_events(after_event_id="missing")

    assert replay.after_event_id_found is False
    assert replay.events == ()


def test_store_rejects_event_id_collision_with_different_content(tmp_path: pathlib.Path) -> None:
    store = BridgeEventStore(tmp_path)
    first = create_bridge_event(event_type="chat_started", source="chat", payload={"n": 1})
    second = create_bridge_event(event_type="chat_started", source="chat", payload={"n": 2})
    colliding = second.model_copy(update={"event_id": first.event_id})

    store.append(first)
    with pytest.raises(BridgeEventStoreError, match="event_id collision"):
        store.append(colliding)

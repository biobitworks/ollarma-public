"""HTTP tests for RTB-01 bridge event replay and compatibility hooks."""
from __future__ import annotations

import json

from starlette.testclient import TestClient

from ollarma.http_api import app


def test_bridge_events_empty_on_clean_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLARMA_BRIDGE_EVENTS_ROOT", str(tmp_path))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/bridge/events")

    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == 1
    assert data["events"] == []
    assert data["after_event_id_found"] is True
    assert data["limit"] == 100


def test_bridge_events_stream_returns_sse_and_done(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLARMA_BRIDGE_EVENTS_ROOT", str(tmp_path))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/bridge/events/stream")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert "event: done" in response.text


def test_bridge_events_default_replay_returns_latest_window(tmp_path, monkeypatch) -> None:
    from ollarma.bridge_events import BridgeEventStore, create_bridge_event

    monkeypatch.setenv("OLLARMA_BRIDGE_EVENTS_ROOT", str(tmp_path))
    store = BridgeEventStore(tmp_path)
    first = store.append(
        create_bridge_event(event_type="chat_started", source="chat", payload={"idx": 1})
    )
    second = store.append(
        create_bridge_event(event_type="chat_done", source="chat", payload={"idx": 2})
    )
    third = store.append(
        create_bridge_event(event_type="route_done", source="route", payload={"idx": 3})
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/bridge/events?limit=2")

    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["event_id"] for event in events] == [second.event_id, third.event_id]

    incremental = client.get(f"/bridge/events?after_event_id={first.event_id}&limit=2")
    assert [event["event_id"] for event in incremental.json()["events"]] == [
        second.event_id,
        third.event_id,
    ]


def test_chat_response_unchanged_and_events_emitted(tmp_path, monkeypatch) -> None:
    import ollarma.http_api as http_mod
    from ollarma.service import ChatResult

    monkeypatch.setenv("OLLARMA_BRIDGE_EVENTS_ROOT", str(tmp_path))
    monkeypatch.setattr(
        http_mod.service,
        "chat_with_model",
        lambda **kwargs: ChatResult(response="hello", model="qwen3.5:2b"),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/chat", json={"message": "hi", "token": "secret-token"})

    assert response.status_code == 200
    assert response.json() == {
        "response": "hello",
        "model": "qwen3.5:2b",
        "status": "answered",
        "reason_code": None,
        "detail": None,
        "recovery_commands": [],
    }
    events = client.get("/bridge/events").json()["events"]
    assert [event["event_type"] for event in events] == ["chat_started", "chat_done"]
    bridge_payload = json.dumps(events[0]["payload"])
    assert "request_body" not in events[0]["payload"]
    assert "secret-token" not in bridge_payload
    assert "hi" not in bridge_payload
    conversations = tmp_path / ".ollarma" / "conversations"
    assert (conversations / "events.jsonl").exists()
    turn = json.loads((conversations / "events.jsonl").read_text().splitlines()[0])
    assert turn["surface"] == "http_chat"
    assert turn["model"] == "qwen3.5:2b"
    assert turn["bridge_event_refs"] == [events[0]["event_id"], events[1]["event_id"]]


def test_chat_sse_wrapper_unchanged_and_events_emitted(tmp_path, monkeypatch) -> None:
    import ollarma.http_api as http_mod
    from ollarma.service import ChatResult

    monkeypatch.setenv("OLLARMA_BRIDGE_EVENTS_ROOT", str(tmp_path))
    monkeypatch.setattr(
        http_mod.service,
        "chat_with_model",
        lambda **kwargs: ChatResult(response="hello", model="qwen3.5:2b"),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/chat?stream=true",
        json={"message": "hi"},
        headers={"Accept": "text/event-stream"},
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert "data: " in response.text
    assert "[DONE]" in response.text
    assert [event["event_type"] for event in client.get("/bridge/events").json()["events"]] == [
        "chat_started",
        "chat_done",
    ]


def test_route_response_unchanged_and_events_emitted(tmp_path, monkeypatch) -> None:
    import ollarma.http_api as http_mod
    from ollarma.service import RouteResult

    monkeypatch.setenv("OLLARMA_BRIDGE_EVENTS_ROOT", str(tmp_path))
    monkeypatch.setattr(
        http_mod.service,
        "route_prompt",
        lambda **kwargs: RouteResult(
            final_response="done",
            tool_calls_count=0,
            model="qwen3.5:2b",
            project="overwatch",
            lane="kb_direct",
            reason_code="KB_DIRECT_ANSWER",
            query_class="file_lookup",
            kb_status="ready",
            evidence_refs=({"doc_id": "d1", "score": 0.98, "text": "long raw text should not appear"},),
        ),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/route", json={"prompt": "summarize drift", "project": "overwatch"})

    assert response.status_code == 200
    data = response.json()
    assert data["final_response"] == "done"
    assert data["project"] == "overwatch"
    events = client.get("/bridge/events").json()["events"]
    assert [event["event_type"] for event in events] == ["route_started", "route_done", "kb_hit"]
    assert events[-1]["payload"]["hits"][0] == {"doc_id": "d1", "score": 0.98}
    bridge_payload = json.dumps(events[0]["payload"])
    assert "request_body" not in events[0]["payload"]
    assert "summarize drift" not in bridge_payload
    conversations = tmp_path / ".ollarma" / "conversations"
    turn = json.loads((conversations / "events.jsonl").read_text().splitlines()[0])
    assert turn["surface"] == "http_route"
    assert turn["project"] == "overwatch"
    assert turn["lane"] == "kb_direct"
    assert turn["kb_evidence_refs"] == ["d1"]

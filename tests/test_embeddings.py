"""Tests for the RTB-REQ-25 embed-pin + /embed surface.

Covers: model selection, keep_alive=-1 request shape, endpoint request/response
schema, and LOUD degraded behavior (bridge down / model missing / evicted /
empty) — embeddings must never silently return a zero vector.
"""
from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from ollarma import embeddings
from ollarma.embeddings import (
    EMBED_BRIDGE_DOWN,
    EMBED_EMPTY,
    EMBED_EMPTY_INPUT,
    EMBED_KEEP_ALIVE,
    EMBED_MODEL,
    EMBED_MODEL_UNAVAILABLE,
    EmbedResult,
    embed_status,
    embed_text,
    pin_embed_model,
)
import ollarma.http_api as http_mod
from ollarma.http_api import app


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeController:
    """Minimal stand-in for PipelineController (pin refcount + bookkeeping)."""

    def __init__(self, benchmark_active: bool = False):
        from ollarma.pipeline_control import ModelPinState

        self._pin_state: dict = {}
        self._benchmark_active = benchmark_active
        self._ModelPinState = ModelPinState

    def pin(self, model: str):
        if self._benchmark_active:
            raise ValueError("BENCHMARK_ACTIVE")
        prev = self._pin_state.get(model)
        count = (prev.pin_count if prev else 0) + 1
        self._pin_state[model] = self._ModelPinState(pin_count=count)
        return None


def _patch_post(monkeypatch, fn):
    monkeypatch.setattr(httpx, "post", fn)


# ---------------------------------------------------------------------------
# Model selection + request shape
# ---------------------------------------------------------------------------

def test_embed_uses_canonical_model_and_keep_alive(monkeypatch):
    """The embed call must target nomic-embed-text with keep_alive=-1."""
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, {"embedding": [0.1, 0.2, 0.3]})

    _patch_post(monkeypatch, fake_post)

    result = embed_text("hello world")

    assert EMBED_MODEL == "nomic-embed-text"
    assert captured["json"]["model"] == EMBED_MODEL
    assert captured["json"]["keep_alive"] == EMBED_KEEP_ALIVE == -1
    assert captured["json"]["prompt"] == "hello world"
    assert "embeddings" in captured["url"]
    assert result.status == "ok"
    assert result.dim == 3
    assert result.embedding == (0.1, 0.2, 0.3)
    assert result.keep_alive == -1


def test_embed_result_is_serializable(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _FakeResponse(200, {"embedding": [1.0]}))
    payload = embed_text("x").model_dump()
    assert payload["model"] == EMBED_MODEL
    assert payload["status"] == "ok"
    assert payload["dim"] == 1


# ---------------------------------------------------------------------------
# LOUD degraded behavior — never a silent zero vector
# ---------------------------------------------------------------------------

def test_embed_empty_input_is_degraded():
    result = embed_text("   ")
    assert result.status == "degraded"
    assert result.reason_code == EMBED_EMPTY_INPUT
    assert result.embedding == ()


def test_embed_bridge_down_is_degraded(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    _patch_post(monkeypatch, boom)
    result = embed_text("hello")
    assert result.status == "degraded"
    assert result.reason_code == EMBED_BRIDGE_DOWN
    assert result.embedding == ()
    assert result.dim == 0


def test_embed_model_unavailable_is_degraded(monkeypatch):
    _patch_post(
        monkeypatch,
        lambda *a, **k: _FakeResponse(404, text='{"error":"model \'nomic-embed-text\' not found"}'),
    )
    result = embed_text("hello")
    assert result.status == "degraded"
    assert result.reason_code == EMBED_MODEL_UNAVAILABLE


def test_embed_empty_vector_is_degraded(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _FakeResponse(200, {"embedding": []}))
    result = embed_text("hello")
    assert result.status == "degraded"
    assert result.reason_code == EMBED_EMPTY


# ---------------------------------------------------------------------------
# pin_embed_model + embed_status
# ---------------------------------------------------------------------------

def test_pin_embed_model_pins_and_loads(monkeypatch):
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        return _FakeResponse(200, {"embedding": [0.0, 1.0]})

    _patch_post(monkeypatch, fake_post)
    ctrl = _FakeController()

    status = pin_embed_model(ctrl)

    assert status.status == "ok"
    assert status.pinned is True
    assert status.resident is True
    assert status.available is True
    # refcount pin recorded for the embed model
    assert ctrl._pin_state[EMBED_MODEL].pin_count == 1
    # load probe issued keep_alive=-1
    assert captured["json"]["keep_alive"] == -1


def test_pin_embed_model_idempotent_refcount(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _FakeResponse(200, {"embedding": [0.0]}))
    ctrl = _FakeController()
    pin_embed_model(ctrl)
    pin_embed_model(ctrl)
    # second call sees existing refcount and does not double-pin
    assert ctrl._pin_state[EMBED_MODEL].pin_count == 1


def test_pin_embed_model_degraded_during_benchmark(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _FakeResponse(200, {"embedding": [0.0]}))
    ctrl = _FakeController(benchmark_active=True)
    status = pin_embed_model(ctrl)
    assert status.status == "degraded"
    assert status.reason_code == "BENCHMARK_ACTIVE"
    assert status.pinned is False
    assert EMBED_MODEL not in ctrl._pin_state


def test_pin_embed_model_does_not_refcount_when_probe_fails(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _FakeResponse(404, text="model not found"))
    ctrl = _FakeController()
    status = pin_embed_model(ctrl)
    assert status.status == "degraded"
    assert status.reason_code == EMBED_MODEL_UNAVAILABLE
    assert status.pinned is False
    assert EMBED_MODEL not in ctrl._pin_state


def test_embed_status_is_read_only_ps_probe(monkeypatch):
    calls: dict[str, int] = {"post": 0, "get": 0}

    def fake_post(*a, **k):
        calls["post"] += 1
        raise AssertionError("embed_status must not POST embeddings")

    def fake_get(*a, **k):
        calls["get"] += 1
        return _FakeResponse(200, {"models": [{"model": f"{EMBED_MODEL}:latest"}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    status = embed_status(_FakeController())
    assert status.status == "ok"
    assert status.resident is True
    assert calls == {"post": 0, "get": 1}


def test_embed_status_degraded_when_bridge_down(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    status = embed_status(_FakeController())
    assert status.status == "degraded"
    assert status.available is False
    assert status.reason_code == EMBED_BRIDGE_DOWN


def test_embed_status_degraded_when_not_resident(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(200, {"models": []}))
    status = embed_status(_FakeController())
    assert status.status == "degraded"
    assert status.resident is False
    assert status.reason_code == EMBED_MODEL_UNAVAILABLE


# ---------------------------------------------------------------------------
# HTTP endpoint schema
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_embed_endpoint_ok(client, monkeypatch):
    monkeypatch.setattr(
        http_mod.service,
        "embed_text",
        lambda text: EmbedResult(
            model=EMBED_MODEL, status="ok", embedding=(0.1, 0.2), dim=2
        ),
    )
    resp = client.post("/embed", json={"text": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == EMBED_MODEL
    assert body["dim"] == 2
    assert body["embedding"] == [0.1, 0.2]


def test_embed_endpoint_requires_text(client):
    resp = client.post("/embed", json={})
    assert resp.status_code == 400
    assert "text is required" in resp.json()["error"]


def test_embed_endpoint_degraded_returns_503(client, monkeypatch):
    monkeypatch.setattr(
        http_mod.service,
        "embed_text",
        lambda text: EmbedResult(
            model=EMBED_MODEL, status="degraded", reason_code=EMBED_BRIDGE_DOWN,
            detail="bridge down",
        ),
    )
    resp = client.post("/embed", json={"text": "hello"})
    assert resp.status_code == 503
    assert resp.json()["reason_code"] == EMBED_BRIDGE_DOWN


def test_embed_status_endpoint(client, monkeypatch):
    from ollarma.embeddings import EmbedStatus

    monkeypatch.setattr(
        http_mod.service,
        "embed_status",
        lambda: EmbedStatus(
            model=EMBED_MODEL, available=True, resident=True, pinned=True, status="ok"
        ),
    )
    resp = client.get("/embed/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == EMBED_MODEL
    assert body["pinned"] is True

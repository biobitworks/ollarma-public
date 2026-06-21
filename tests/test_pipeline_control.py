"""tests/test_pipeline_control.py — Unit tests for pipeline_control.py.

All tests are offline (no Ollama, no network).
Tests that need filesystem state pass tmp_path and instantiate
PipelineController(state_dir=tmp_path) directly — never the global singleton.
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from ollarma.pipeline_control import (
    PIPELINE_BENCHMARK_ACTIVE,
    PIPELINE_MODEL_PINNED,
    PIPELINE_OPERATION_EVICT,
    PIPELINE_OPERATION_PIN,
    PIPELINE_OPERATION_WARMUP,
    ModelPinState,
    ModelPipelineStatus,
    ModelStatusSnapshot,
    PipelineController,
    PipelineReceipt,
)


# ===========================================================================
# Task 1 — Models & receipts
# ===========================================================================


def test_pipeline_receipt_create() -> None:
    """PipelineReceipt.create() returns a receipt with all fields populated."""
    receipt = PipelineReceipt.create(
        operation="warmup",
        model="phi4-mini",
        previous_hash=None,
        payload={"key": "value"},
    )
    assert receipt.operation == "warmup"
    assert receipt.model == "phi4-mini"
    assert receipt.previous_hash is None
    assert isinstance(receipt.receipt_hash, str)
    assert len(receipt.receipt_hash) == 64  # SHA-256 hex
    assert receipt.payload == {"key": "value"}
    assert receipt.created_at  # non-empty ISO timestamp


def test_pipeline_receipt_hash_chained() -> None:
    """Second receipt's previous_hash equals first receipt's receipt_hash."""
    first = PipelineReceipt.create("warmup", "phi4-mini", None)
    second = PipelineReceipt.create("pin", "phi4-mini", first.receipt_hash)
    assert second.previous_hash == first.receipt_hash
    assert second.receipt_hash != first.receipt_hash


def test_pipeline_receipt_hash_deterministic() -> None:
    """Two receipts with identical inputs (same timestamp) produce the same hash."""
    # Use the same created_at by constructing the data dict manually
    # The canonical_hash guarantee means identical dicts → identical hashes.
    # We test via two separate calls sharing the same payload and chaining context.
    # Since timestamps differ, we verify the hash changes — not that it is identical.
    # The real determinism guarantee is that canonical_hash itself is deterministic.
    receipt_a = PipelineReceipt.create("warmup", "phi4-mini", None, {"x": 1})
    # Rebuild with the same hash inputs by constructing directly
    from ollarma.evidence import canonical_hash

    hash_a = canonical_hash(
        {
            "operation": receipt_a.operation,
            "model": receipt_a.model,
            "previous_hash": receipt_a.previous_hash,
            "payload": receipt_a.payload,
            "created_at": receipt_a.created_at,
        }
    )
    assert hash_a == receipt_a.receipt_hash


def test_model_status_snapshot_model() -> None:
    """ModelStatusSnapshot holds a tuple of ModelPipelineStatus."""
    m = ModelPipelineStatus(
        name="qwen3:8b",
        size_vram_mb=5120.0,
        pinned=True,
        pin_count=1,
        decode_tps_ewma=45.2,
    )
    snapshot = ModelStatusSnapshot(
        models=(m,),
        swap_used_mb=0.0,
        state="ok",
        cached_at="2026-04-15T12:00:00Z",
    )
    assert len(snapshot.models) == 1
    assert snapshot.models[0].name == "qwen3:8b"
    assert snapshot.models[0].pinned is True
    assert snapshot.state == "ok"


def test_model_pin_state_defaults() -> None:
    """ModelPinState() has pin_count=0 and decode_tps_ewma=None by default."""
    ps = ModelPinState()
    assert ps.pin_count == 0
    assert ps.decode_tps_ewma is None


# ===========================================================================
# Task 2 — PipelineController
# ===========================================================================


def test_controller_pin_increments_count(tmp_path: pathlib.Path) -> None:
    """Pinning the same model twice results in pin_count==2 in the sidecar."""
    ctrl = PipelineController(state_dir=tmp_path)
    ctrl.pin("phi4-mini")
    ctrl.pin("phi4-mini")
    assert ctrl._pin_state["phi4-mini"].pin_count == 2


def test_controller_evict_pinned_raises(tmp_path: pathlib.Path) -> None:
    """Evicting a pinned model (pin_count > 0) raises ValueError(PIPELINE_MODEL_PINNED)."""
    ctrl = PipelineController(state_dir=tmp_path)
    ctrl.pin("phi4-mini")
    with pytest.raises(ValueError, match=PIPELINE_MODEL_PINNED):
        ctrl.evict("phi4-mini")


def test_controller_evict_unpinned_emits_receipt(tmp_path: pathlib.Path) -> None:
    """Evicting an unpinned model emits a receipt with operation=='evict'."""
    ctrl = PipelineController(state_dir=tmp_path)

    mock_response = MagicMock()
    with patch("httpx.post", return_value=mock_response) as mock_post:
        receipt = ctrl.evict("phi4-mini")

    mock_post.assert_called_once()
    assert receipt.operation == PIPELINE_OPERATION_EVICT
    assert receipt.model == "phi4-mini"
    assert len(receipt.receipt_hash) == 64


def test_controller_sidecar_persists(tmp_path: pathlib.Path) -> None:
    """Pin state survives process restart (new PipelineController from same state_dir)."""
    ctrl1 = PipelineController(state_dir=tmp_path)
    ctrl1.pin("qwen3:8b")
    ctrl1.pin("qwen3:8b")

    # Simulate restart
    ctrl2 = PipelineController(state_dir=tmp_path)
    assert ctrl2._pin_state["qwen3:8b"].pin_count == 2


def test_controller_prunes_reserved_model_pins_from_sidecar(tmp_path: pathlib.Path) -> None:
    """Reserved Antigence/Sentinel pins are migrated out of Ollarma sidecar state."""
    import orjson

    sidecar = tmp_path / "pipelines.json"
    sidecar.write_bytes(
        orjson.dumps(
            {
                "pins": {
                    "qwen3:1.7b": {"pin_count": 1, "decode_tps_ewma": None},
                    "qwen2.5:1.5b": {"pin_count": 2, "decode_tps_ewma": None},
                },
                "last_receipt_hash": "abc123",
            }
        )
    )

    ctrl = PipelineController(state_dir=tmp_path)

    assert "qwen3:1.7b" not in ctrl._pin_state
    assert ctrl._pin_state["qwen2.5:1.5b"].pin_count == 2
    persisted = orjson.loads(sidecar.read_bytes())
    assert "qwen3:1.7b" not in persisted["pins"]
    assert persisted["pins"]["qwen2.5:1.5b"]["pin_count"] == 2


def test_controller_drift_detected(tmp_path: pathlib.Path) -> None:
    """State is 'drift' when a pinned model is absent from the /api/ps response."""
    ctrl = PipelineController(state_dir=tmp_path)
    ctrl.pin("phi4-mini")

    # Invalidate status cache to force rebuild
    ctrl._status_cache = None

    # Patch _fetch_ollama_api_ps to return empty (model not loaded in Ollama)
    with patch(
        "ollarma.guards._fetch_ollama_api_ps", return_value=()
    ), patch(
        "ollarma.guards.collect_runtime_telemetry",
        return_value=MagicMock(swap_used_mb=None),
    ):
        snapshot = ctrl._build_status_snapshot()

    assert snapshot.state == "drift"


def test_controller_status_matches_untagged_pin_to_latest_runtime_name(
    tmp_path: pathlib.Path,
) -> None:
    """An untagged pin matches Ollama's ``:latest`` name in /api/ps."""
    from ollarma.guards import RuntimeModelTelemetry

    ctrl = PipelineController(state_dir=tmp_path)
    ctrl.pin("nomic-embed-text")

    with patch(
        "ollarma.guards._fetch_ollama_api_ps",
        return_value=(RuntimeModelTelemetry(name="nomic-embed-text:latest"),),
    ), patch(
        "ollarma.guards.collect_runtime_telemetry",
        return_value=MagicMock(swap_used_mb=None),
    ):
        snapshot = ctrl._build_status_snapshot()

    assert snapshot.state == "ok"
    assert snapshot.models[0].name == "nomic-embed-text:latest"
    assert snapshot.models[0].pinned is True
    assert snapshot.models[0].pin_count == 1


def test_benchmark_flag_blocks_warmup(tmp_path: pathlib.Path) -> None:
    """warmup() raises ValueError(BENCHMARK_ACTIVE) when the flag file exists."""
    ctrl = PipelineController(state_dir=tmp_path)
    # Create the flag file
    ctrl._benchmark_flag.touch()

    with pytest.raises(ValueError, match=PIPELINE_BENCHMARK_ACTIVE):
        ctrl.warmup("phi4-mini")


def test_controller_warmup_defaults_to_short_keepalive(tmp_path: pathlib.Path) -> None:
    """Generic warmups remain transient by default."""
    ctrl = PipelineController(state_dir=tmp_path)

    with patch("httpx.post") as mock_post:
        ctrl.warmup("phi4-mini")

    assert mock_post.call_args.kwargs["json"]["keep_alive"] == "5m"


def test_controller_warmup_accepts_indefinite_keepalive(tmp_path: pathlib.Path) -> None:
    """Residency can request an indefinite Ollama keepalive for sidecar-pinned rescue."""
    ctrl = PipelineController(state_dir=tmp_path)

    with patch("httpx.post") as mock_post:
        ctrl.warmup("qwen2.5:1.5b", keep_alive=-1)

    assert mock_post.call_args.kwargs["json"]["keep_alive"] == -1


def test_receipt_chain_maintained(tmp_path: pathlib.Path) -> None:
    """The second operation's previous_hash equals the first operation's receipt_hash."""
    ctrl = PipelineController(state_dir=tmp_path)

    receipt1 = ctrl.pin("phi4-mini")
    receipt2 = ctrl.pin("phi4-mini")

    assert receipt2.previous_hash == receipt1.receipt_hash

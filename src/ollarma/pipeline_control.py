"""pipeline_control.py — Model lifecycle control: warmup, pin, evict, status."""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import tempfile
import threading
import time
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict

from ollarma.evidence import canonical_hash
from ollarma.reserved_models import is_reserved_model
from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB

# ---------------------------------------------------------------------------
# Receipt operation constants
# ---------------------------------------------------------------------------

PIPELINE_OPERATION_WARMUP = "warmup"
PIPELINE_OPERATION_PIN = "pin"
PIPELINE_OPERATION_UNPIN = "unpin"
PIPELINE_OPERATION_EVICT = "evict"
PIPELINE_OPERATION_DRAIN_SWAP = "drain_swap"

# ---------------------------------------------------------------------------
# Drain-swap tunable
# ---------------------------------------------------------------------------

DRAIN_POLL_INTERVAL_S = 0.1

# ---------------------------------------------------------------------------
# Error / flag codes
# ---------------------------------------------------------------------------

PIPELINE_SWAP_DEGRADED = "SWAP_DEGRADED"
PIPELINE_MODEL_PINNED = "PIPELINE_MODEL_PINNED"
PIPELINE_BENCHMARK_ACTIVE = "BENCHMARK_ACTIVE"

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

STATUS_CACHE_TTL_S = 2.0   # /models/status cache TTL in seconds
EWMA_ALPHA = 0.3            # Exponential moving-average weight for decode_tps


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PipelineReceipt(BaseModel):
    """Immutable, hash-chained receipt for a single pipeline operation."""

    model_config = ConfigDict(frozen=True)

    operation: str
    model: str
    previous_hash: str | None = None
    receipt_hash: str
    payload: dict[str, Any] = {}
    created_at: str

    @classmethod
    def create(
        cls,
        operation: str,
        model: str,
        previous_hash: str | None,
        payload: dict[str, Any] | None = None,
    ) -> "PipelineReceipt":
        """Build a new receipt, chaining it to *previous_hash*."""
        payload = payload or {}
        created_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        receipt_hash = canonical_hash(
            {
                "operation": operation,
                "model": model,
                "previous_hash": previous_hash,
                "payload": payload,
                "created_at": created_at,
            }
        )
        return cls(
            operation=operation,
            model=model,
            previous_hash=previous_hash,
            receipt_hash=receipt_hash,
            payload=payload,
            created_at=created_at,
        )


class ModelPipelineStatus(BaseModel):
    """Live status of one model as seen by the pipeline controller."""

    model_config = ConfigDict(frozen=True)

    name: str
    size_vram_mb: float | None = None
    pinned: bool = False
    pin_count: int = 0
    decode_tps_ewma: float | None = None


class ModelStatusSnapshot(BaseModel):
    """Cached snapshot of all loaded models plus host swap pressure."""

    model_config = ConfigDict(frozen=True)

    models: tuple[ModelPipelineStatus, ...] = ()
    swap_used_mb: float | None = None
    state: str = "ok"   # "ok" | "drift"
    cached_at: str


class ModelPinState(BaseModel):
    """Persisted per-model pin bookkeeping (stored in pipelines.json sidecar)."""

    pin_count: int = 0
    decode_tps_ewma: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_state_dir() -> pathlib.Path:
    """Return state directory from env var or system tempdir."""
    env_dir = os.environ.get("OLLARMA_SCHEDULER_DIR")
    if env_dir:
        return pathlib.Path(env_dir)
    return pathlib.Path(tempfile.gettempdir()) / "ollarma-scheduler"


def _model_name_aliases(name: str) -> set[str]:
    """Return runtime aliases for an Ollama model name.

    Ollama often reports untagged requests as ``name:latest`` in ``/api/ps``.
    Pipeline pin bookkeeping stores the caller's request string, so status
    reconciliation needs to treat those names as the same model.
    """
    aliases = {name}
    if ":" not in name:
        aliases.add(f"{name}:latest")
    if name.endswith(":latest"):
        aliases.add(name.removesuffix(":latest"))
    return aliases


def _model_names_match(left: str, right: str) -> bool:
    return bool(_model_name_aliases(left).intersection(_model_name_aliases(right)))


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class PipelineController:
    """Thread-safe pipeline controller. Singleton via get_pipeline_controller()."""

    def __init__(self, state_dir: pathlib.Path | None = None) -> None:
        self._lock = threading.Lock()
        self._state_dir: pathlib.Path = state_dir or _default_state_dir()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._sidecar_path = self._state_dir / "pipelines.json"
        self._receipts_path = self._state_dir / "pipeline_receipts.jsonl"
        self._benchmark_flag = self._state_dir / "benchmark_active.flag"
        self._pin_state: dict[str, ModelPinState] = {}
        self._last_receipt_hash: str | None = None
        self._status_cache: ModelStatusSnapshot | None = None
        self._status_cache_at: float = 0.0
        self._load_sidecar()

    # ------------------------------------------------------------------
    # Sidecar I/O
    # ------------------------------------------------------------------

    def _load_sidecar(self) -> None:
        """Load persisted pin state from pipelines.json (silently on corrupt)."""
        if not self._sidecar_path.exists():
            return
        try:
            raw = orjson.loads(self._sidecar_path.read_bytes())
            migrated = False
            for name, state_dict in raw.get("pins", {}).items():
                if is_reserved_model(name):
                    migrated = True
                    continue
                self._pin_state[name] = ModelPinState(**state_dict)
            self._last_receipt_hash = raw.get("last_receipt_hash")
            if migrated:
                self._save_sidecar()
        except Exception:
            pass  # corrupt sidecar — start fresh

    def _save_sidecar(self) -> None:
        """Persist current pin state to pipelines.json (caller holds self._lock)."""
        data = {
            "pins": {name: state.model_dump() for name, state in self._pin_state.items()},
            "last_receipt_hash": self._last_receipt_hash,
        }
        self._sidecar_path.write_bytes(orjson.dumps(data))

    # ------------------------------------------------------------------
    # Benchmark flag
    # ------------------------------------------------------------------

    def _is_benchmark_active(self) -> bool:
        return self._benchmark_flag.exists()

    def start_benchmark(self) -> None:
        """Set the benchmark-active flag.

        Callers must pair this with ``end_benchmark()`` in a ``finally`` block
        to ensure the flag is cleared even on failure. This is the public
        entry point used by ``service.run_benchmark()`` so the internal flag
        file path remains an implementation detail of this controller.
        """
        self._benchmark_flag.parent.mkdir(parents=True, exist_ok=True)
        self._benchmark_flag.touch()

    def end_benchmark(self) -> None:
        """Clear the benchmark-active flag (idempotent — safe if not set)."""
        self._benchmark_flag.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Receipt emission
    # ------------------------------------------------------------------

    def _emit_receipt(
        self,
        operation: str,
        model: str,
        payload: dict[str, Any] | None = None,
    ) -> PipelineReceipt:
        """Create a hash-chained receipt and append it to pipeline_receipts.jsonl.

        Caller must hold self._lock.
        """
        receipt = PipelineReceipt.create(operation, model, self._last_receipt_hash, payload)
        self._last_receipt_hash = receipt.receipt_hash
        with open(self._receipts_path, "ab") as f:
            f.write(orjson.dumps(receipt.model_dump()) + b"\n")
        return receipt

    # ------------------------------------------------------------------
    # Status snapshot (cached)
    # ------------------------------------------------------------------

    def get_status(self) -> ModelStatusSnapshot:
        """Return cached /models/status snapshot (TTL 2 s)."""
        now = time.monotonic()
        if self._status_cache is not None and (now - self._status_cache_at) < STATUS_CACHE_TTL_S:
            return self._status_cache
        with self._lock:
            # Re-check after acquiring lock
            now = time.monotonic()
            if self._status_cache is not None and (now - self._status_cache_at) < STATUS_CACHE_TTL_S:
                return self._status_cache
            snapshot = self._build_status_snapshot()
            self._status_cache = snapshot
            self._status_cache_at = now
            return snapshot

    def _build_status_snapshot(self) -> ModelStatusSnapshot:
        """Build a fresh ModelStatusSnapshot (lazy-imports guards to avoid circular import)."""
        from ollarma.guards import _fetch_ollama_api_ps, collect_runtime_telemetry  # noqa: PLC0415

        try:
            api_models = _fetch_ollama_api_ps()
        except Exception:
            api_models = ()

        try:
            telemetry = collect_runtime_telemetry()
            swap_mb: float | None = telemetry.swap_used_mb
        except Exception:
            swap_mb = None

        # Drift: any pinned model no longer visible in /api/ps
        pinned_names = {n for n, s in self._pin_state.items() if s.pin_count > 0}
        api_names = {m.name for m in api_models}
        drifted = {
            pinned
            for pinned in pinned_names
            if not any(_model_names_match(pinned, api_name) for api_name in api_names)
        }
        state = "drift" if drifted else "ok"

        models: list[ModelPipelineStatus] = []
        for m in api_models:
            ps = self._pin_state.get(m.name)
            if ps is None:
                ps = next(
                    (
                        state
                        for pinned_name, state in self._pin_state.items()
                        if _model_names_match(pinned_name, m.name)
                    ),
                    ModelPinState(),
                )
            models.append(
                ModelPipelineStatus(
                    name=m.name,
                    size_vram_mb=(
                        m.size_vram_bytes / (1024 * 1024) if m.size_vram_bytes else None
                    ),
                    pinned=ps.pin_count > 0,
                    pin_count=ps.pin_count,
                    decode_tps_ewma=ps.decode_tps_ewma,
                )
            )

        return ModelStatusSnapshot(
            models=tuple(models),
            swap_used_mb=swap_mb,
            state=state,
            cached_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    # ------------------------------------------------------------------
    # Lifecycle operations
    # ------------------------------------------------------------------

    def warmup(self, model: str, *, keep_alive: str | int = "5m") -> PipelineReceipt:
        """Warm up a model.

        Raises:
            ValueError(BENCHMARK_ACTIVE) if benchmark flag is set.
            ValueError(SWAP_DEGRADED) if host swap > 512 MB.
            ValueError("WARMUP_FAILED: ...") on Ollama communication failure.
        """
        if self._is_benchmark_active():
            raise ValueError(PIPELINE_BENCHMARK_ACTIVE)

        from ollarma.guards import collect_runtime_telemetry  # noqa: PLC0415

        try:
            telemetry = collect_runtime_telemetry()
            swap_mb: float = telemetry.swap_used_mb or 0.0
        except Exception:
            swap_mb = 0.0

        if swap_mb > SWAP_DEGRADED_THRESHOLD_MB:
            raise ValueError(PIPELINE_SWAP_DEGRADED)

        try:
            import httpx  # noqa: PLC0415

            httpx.post(
                "http://localhost:11434/api/generate",
                json={"model": model, "prompt": "", "keep_alive": keep_alive},
                timeout=30.0,
            )
        except Exception as exc:
            raise ValueError(f"WARMUP_FAILED: {exc}") from exc

        with self._lock:
            receipt = self._emit_receipt(PIPELINE_OPERATION_WARMUP, model)
            self._save_sidecar()

        return receipt

    def pin(self, model: str) -> PipelineReceipt:
        """Increment pin refcount for *model*.

        Raises:
            ValueError(BENCHMARK_ACTIVE) if benchmark flag is set.
        """
        if self._is_benchmark_active():
            raise ValueError(PIPELINE_BENCHMARK_ACTIVE)

        with self._lock:
            if model not in self._pin_state:
                self._pin_state[model] = ModelPinState()
            new_count = self._pin_state[model].pin_count + 1
            self._pin_state[model] = self._pin_state[model].model_copy(
                update={"pin_count": new_count}
            )
            receipt = self._emit_receipt(
                PIPELINE_OPERATION_PIN, model, {"pin_count": new_count}
            )
            self._save_sidecar()

        return receipt

    def evict(self, model: str) -> PipelineReceipt:
        """Evict *model* from Ollama memory (keep_alive=0s).

        Raises:
            ValueError(BENCHMARK_ACTIVE) if benchmark flag is set.
            ValueError(PIPELINE_MODEL_PINNED) if pin_count > 0.
        """
        if self._is_benchmark_active():
            raise ValueError(PIPELINE_BENCHMARK_ACTIVE)

        with self._lock:
            ps = self._pin_state.get(model, ModelPinState())
            if ps.pin_count > 0:
                raise ValueError(PIPELINE_MODEL_PINNED)

            # Best-effort evict via Ollama keep_alive=0s
            try:
                import httpx  # noqa: PLC0415

                httpx.post(
                    "http://localhost:11434/api/generate",
                    json={"model": model, "prompt": "", "keep_alive": "0s"},
                    timeout=10.0,
                )
            except Exception:
                pass  # best-effort

            if model in self._pin_state:
                del self._pin_state[model]

            receipt = self._emit_receipt(PIPELINE_OPERATION_EVICT, model)
            self._save_sidecar()

        return receipt

    def drain_and_swap(
        self,
        old_model: str,
        new_model: str,
        *,
        timeout_s: float = 30.0,
        scheduler=None,
    ) -> "PipelineReceipt":
        """Soft-drain in-flight requests, then swap old_model -> new_model.

        Polls scheduler.snapshot().active_job_id until None (bounded by timeout_s).
        Falls back to hard swap if timeout exceeded. Emits a PipelineReceipt.

        Args:
            scheduler: Injected for testing; defaults to the service-level singleton.

        Raises:
            ValueError(BENCHMARK_ACTIVE) if benchmark flag is set.
            ValueError("SWAP_FAILED: ...") if warmup of new_model fails.
        """
        if self._is_benchmark_active():
            raise ValueError(PIPELINE_BENCHMARK_ACTIVE)

        import time  # noqa: PLC0415

        # Get the scheduler singleton if not injected
        if scheduler is None:
            from ollarma.service import get_scheduler  # noqa: PLC0415
            scheduler = get_scheduler()

        # Drain: wait until no active serialized job
        deadline = time.monotonic() + timeout_s
        drained = False
        while time.monotonic() < deadline:
            snapshot = scheduler.snapshot()
            if snapshot.active_job_id is None:
                drained = True
                break
            time.sleep(DRAIN_POLL_INTERVAL_S)

        fallback = not drained  # True if we timed out

        # Evict old model (best-effort)
        try:
            import httpx  # noqa: PLC0415

            httpx.post(
                "http://localhost:11434/api/generate",
                json={"model": old_model, "prompt": "", "keep_alive": "0s"},
                timeout=10.0,
            )
        except Exception:
            pass  # best-effort

        # Warmup new model
        try:
            import httpx  # noqa: PLC0415

            httpx.post(
                "http://localhost:11434/api/generate",
                json={"model": new_model, "prompt": "", "keep_alive": "5m"},
                timeout=30.0,
            )
        except Exception as exc:
            raise ValueError(f"SWAP_FAILED: {exc}") from exc

        # Update pin state: remove old, preserve pins for new
        with self._lock:
            if old_model in self._pin_state:
                del self._pin_state[old_model]
            receipt = self._emit_receipt(
                PIPELINE_OPERATION_DRAIN_SWAP,
                new_model,
                {
                    "old_model": old_model,
                    "new_model": new_model,
                    "drained": drained,
                    "timeout_s": timeout_s,
                    "fallback": fallback,
                },
            )
            self._save_sidecar()

        return receipt


# ---------------------------------------------------------------------------
# Process-singleton factory
# ---------------------------------------------------------------------------

_CONTROLLER: PipelineController | None = None
_CONTROLLER_LOCK = threading.Lock()


def get_pipeline_controller(state_dir: pathlib.Path | None = None) -> PipelineController:
    """Return (or create) the process-singleton PipelineController."""
    global _CONTROLLER
    if _CONTROLLER is None:
        with _CONTROLLER_LOCK:
            if _CONTROLLER is None:
                _CONTROLLER = PipelineController(state_dir)
    return _CONTROLLER

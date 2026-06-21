"""embeddings.py — Ollarma-owned embed-model pin + bounded embed surface.

RTB-REQ-25. The antigen-bank heads and the (future) calibration harness depend on
a stable local embedding model. This module makes `nomic-embed-text` a first-class,
*pinned* resource of the backbone:

  - ``pin_embed_model()`` issues ``keep_alive=-1`` to Ollama so the embed model is
    held resident across big-model pulls (Ollama itself will not evict a -1 model),
    and bumps the PipelineController pin refcount so our own ``evict()`` refuses.
  - ``embed_text()`` returns a vector or a LOUD degraded result — never a silent
    zero vector. A missing model, an evicted model, or a down bridge each surface
    an explicit ``status="degraded"`` + ``reason_code`` (RTB-02 invariant).
  - ``embed_status()`` reports the current posture for health/degraded surfacing.

All Ollama I/O is sequential and localhost-only; the HTTP surface wraps these calls
behind the existing ``Semaphore(1)`` so embeddings never race big-model rungs.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBED_MODEL: str = "nomic-embed-text"   # canonical local embed model (kept, pinned)
EMBED_KEEP_ALIVE: int = -1              # -1 => hold resident indefinitely in Ollama
OLLAMA_EMBEDDINGS_URL: str = "http://localhost:11434/api/embeddings"
OLLAMA_PS_URL: str = "http://localhost:11434/api/ps"

_EMBED_TIMEOUT_S: float = 30.0
_PIN_PROBE_TEXT: str = "ollarma embed pin probe"

# ---------------------------------------------------------------------------
# Status / reason codes
# ---------------------------------------------------------------------------

EMBED_OK = "ok"
EMBED_DEGRADED = "degraded"

# Degraded reason codes (LOUD — surfaced to callers, never swallowed)
EMBED_BRIDGE_DOWN = "EMBED_BRIDGE_DOWN"            # Ollama unreachable / transport error
EMBED_MODEL_UNAVAILABLE = "EMBED_MODEL_UNAVAILABLE"  # model not pulled / not loadable
EMBED_EMPTY = "EMBED_EMPTY"                        # response carried no usable vector
EMBED_EMPTY_INPUT = "EMBED_EMPTY_INPUT"           # caller passed blank text


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

class EmbedResult(BaseModel):
    """Embedding result with an explicit ok/degraded posture.

    On ``status == "degraded"`` the ``embedding`` is empty and ``reason_code`` /
    ``detail`` explain why — callers MUST treat this as a failure, not a vector.
    """

    model_config = ConfigDict(frozen=True)

    model: str
    status: str                      # EMBED_OK | EMBED_DEGRADED
    embedding: tuple[float, ...] = ()
    dim: int = 0
    keep_alive: int = EMBED_KEEP_ALIVE
    reason_code: str | None = None
    detail: str | None = None


class EmbedStatus(BaseModel):
    """Current posture of the pinned embed model (for health/degraded surfacing)."""

    model_config = ConfigDict(frozen=True)

    model: str
    available: bool
    resident: bool
    pinned: bool
    status: str                      # EMBED_OK | EMBED_DEGRADED
    reason_code: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Internal: single Ollama embeddings call
# ---------------------------------------------------------------------------

def _post_embeddings(text: str) -> tuple[list[float] | None, str | None, str | None]:
    """POST one embed request with keep_alive=-1.

    Returns ``(embedding, reason_code, detail)``. On success ``reason_code`` is
    None. Transport/model failures map to LOUD degraded reason codes.
    """
    try:
        import httpx  # noqa: PLC0415

        resp = httpx.post(
            OLLAMA_EMBEDDINGS_URL,
            json={"model": EMBED_MODEL, "prompt": text, "keep_alive": EMBED_KEEP_ALIVE},
            timeout=_EMBED_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — transport failure => bridge down
        return None, EMBED_BRIDGE_DOWN, f"embed bridge unreachable: {exc}"

    if resp.status_code != 200:
        body = resp.text[:200]
        # Ollama returns 404/500 with a "model ... not found" style message
        # when the model is not pulled or cannot be loaded.
        if "not found" in body.lower() or resp.status_code == 404:
            return None, EMBED_MODEL_UNAVAILABLE, f"{EMBED_MODEL} unavailable: {body}"
        return None, EMBED_BRIDGE_DOWN, f"embed bridge HTTP {resp.status_code}: {body}"

    try:
        data: dict[str, Any] = resp.json()
    except Exception as exc:  # noqa: BLE001
        return None, EMBED_EMPTY, f"embed response not JSON: {exc}"

    vector = data.get("embedding")
    if not vector or not isinstance(vector, list):
        return None, EMBED_EMPTY, "embed response carried no vector"
    return [float(x) for x in vector], None, None


def _embed_model_names() -> set[str]:
    """Return acceptable Ollama runtime names for the canonical embed model."""
    return {EMBED_MODEL, f"{EMBED_MODEL}:latest"}


def _loaded_embed_model_names() -> tuple[str, ...] | None:
    """Read loaded Ollama models without mutating residency.

    Returns ``None`` when the bridge is unreachable; otherwise returns the
    normalized model names currently reported by ``GET /api/ps``.
    """
    try:
        import httpx  # noqa: PLC0415

        resp = httpx.get(OLLAMA_PS_URL, timeout=5.0)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    try:
        data: dict[str, Any] = resp.json()
    except Exception:  # noqa: BLE001
        return None
    models = data.get("models", [])
    if not isinstance(models, list):
        return ()
    names: list[str] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("model") or raw.get("name") or "").strip()
        if name:
            names.append(name)
    return tuple(names)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def embed_text(text: str) -> EmbedResult:
    """Embed *text* via the pinned local embed model.

    Always returns an ``EmbedResult``. Failures are LOUD: ``status="degraded"``
    with a ``reason_code`` and empty embedding — never a silent zero vector.
    """
    if not text or not text.strip():
        return EmbedResult(
            model=EMBED_MODEL,
            status=EMBED_DEGRADED,
            reason_code=EMBED_EMPTY_INPUT,
            detail="embed text is required",
        )

    vector, reason_code, detail = _post_embeddings(text)
    if vector is None:
        return EmbedResult(
            model=EMBED_MODEL,
            status=EMBED_DEGRADED,
            reason_code=reason_code,
            detail=detail,
        )
    return EmbedResult(
        model=EMBED_MODEL,
        status=EMBED_OK,
        embedding=tuple(vector),
        dim=len(vector),
    )


def pin_embed_model(controller: Any | None = None) -> EmbedStatus:
    """Pin the embed model resident (keep_alive=-1) and refcount it.

    The keep_alive=-1 probe makes Ollama hold the model across big-model pulls;
    the PipelineController pin refcount makes our own ``evict()`` refuse to drop
    it. Idempotent and best-effort: a benchmark freeze or a down bridge yields a
    degraded status rather than raising.
    """
    from ollarma.pipeline_control import get_pipeline_controller  # noqa: PLC0415

    ctrl = controller or get_pipeline_controller()

    pinned = _is_refcount_pinned(ctrl)

    # Respect benchmark freeze before issuing any residency-mutating probe.
    try:
        if hasattr(ctrl, "_is_benchmark_active") and ctrl._is_benchmark_active():  # noqa: SLF001
            return EmbedStatus(
                model=EMBED_MODEL,
                available=False,
                resident=False,
                pinned=pinned,
                status=EMBED_DEGRADED,
                reason_code="BENCHMARK_ACTIVE",
                detail="embed pin deferred during active benchmark",
            )
    except Exception:  # noqa: BLE001
        pass

    # Issue the keep_alive=-1 load probe so Ollama holds the model resident.
    vector, reason_code, detail = _post_embeddings(_PIN_PROBE_TEXT)
    if vector is None:
        return EmbedStatus(
            model=EMBED_MODEL,
            available=False,
            resident=False,
            pinned=pinned,
            status=EMBED_DEGRADED,
            reason_code=reason_code,
            detail=detail,
        )

    # Refcount-pin only after Ollama accepted the keep_alive=-1 probe. This keeps
    # our sidecar from claiming a pin for a model that failed to load.
    if not pinned:
        try:
            ctrl.pin(EMBED_MODEL)
            pinned = True
        except ValueError as exc:
            reason = "BENCHMARK_ACTIVE" if "BENCHMARK_ACTIVE" in str(exc) else "EMBED_PIN_FAILED"
            return EmbedStatus(
                model=EMBED_MODEL,
                available=True,
                resident=True,
                pinned=False,
                status=EMBED_DEGRADED,
                reason_code=reason,
                detail=str(exc),
            )
    return EmbedStatus(
        model=EMBED_MODEL,
        available=True,
        resident=True,
        pinned=pinned,
        status=EMBED_OK,
    )


def embed_status(controller: Any | None = None) -> EmbedStatus:
    """Report the embed model's current posture (no mutation, no -1 probe load).

    Reads Ollama's loaded-model list and local pin bookkeeping only. It does not
    issue an embedding request and does not extend residency.
    """
    from ollarma.pipeline_control import get_pipeline_controller  # noqa: PLC0415

    ctrl = controller or get_pipeline_controller()
    pinned = _is_refcount_pinned(ctrl)

    loaded = _loaded_embed_model_names()
    if loaded is None:
        return EmbedStatus(
            model=EMBED_MODEL,
            available=False,
            resident=False,
            pinned=pinned,
            status=EMBED_DEGRADED,
            reason_code=EMBED_BRIDGE_DOWN,
            detail="embed bridge status probe unavailable",
        )
    resident = bool(_embed_model_names().intersection(loaded))
    if not resident:
        return EmbedStatus(
            model=EMBED_MODEL,
            available=False,
            resident=False,
            pinned=pinned,
            status=EMBED_DEGRADED,
            reason_code=EMBED_MODEL_UNAVAILABLE,
            detail=f"{EMBED_MODEL} is not resident in Ollama",
        )
    return EmbedStatus(
        model=EMBED_MODEL,
        available=True,
        resident=True,
        pinned=pinned,
        status=EMBED_OK,
    )


def _is_refcount_pinned(controller: Any) -> bool:
    """Return True if the embed model already carries a positive pin refcount."""
    try:
        state = controller._pin_state.get(EMBED_MODEL)  # noqa: SLF001
        return bool(state and state.pin_count > 0)
    except (AttributeError, RuntimeError):
        return False

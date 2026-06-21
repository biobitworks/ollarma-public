"""DEBT-56.1 — per-tier SWAP_DEGRADED guard.

Proves that the scheduler's workflow-lane admission refusal is asset-type
aware: a `JobRequest` carrying `requires_model=False` bypasses the
SWAP_DEGRADED block, while the conservative default (True) still honors it.

No live model calls. Uses a deterministic `RuntimeTelemetry` provider.
"""
from __future__ import annotations

from ollarma.autopilot import DiscoveredAsset, script_requires_model
from ollarma.guards import RuntimeTelemetry
from ollarma.scheduler import (
    SWAP_DEGRADED,
    SWAP_DEGRADED_THRESHOLD_MB,
    JobRequest,
    Lane,
    Scheduler,
)


def _swap_degraded_telemetry() -> RuntimeTelemetry:
    """Return telemetry that is above the RAM-relative swap threshold."""
    return RuntimeTelemetry(
        degraded_mode=True,
        degraded_reason="swap above threshold",
        swap_used_mb=SWAP_DEGRADED_THRESHOLD_MB + 512.0,
        telemetry_source="test/fake",
    )


def test_autopilot_swap_degraded_blocks_model_required_asset() -> None:
    """Baseline: requires_model=True must still be refused under SWAP_DEGRADED."""
    scheduler = Scheduler(telemetry_provider=_swap_degraded_telemetry)

    preview = scheduler.preview(
        JobRequest(
            project="proj-a",
            lane=Lane.WORKFLOW_EXECUTION_QUEUE,
            model="qwen3-coder:7b",
            requires_model=True,
        )
    )

    assert preview.status == "rejected"
    assert preview.reason_code == SWAP_DEGRADED


def test_autopilot_swap_degraded_allows_model_free_asset() -> None:
    """DEBT-56.1: requires_model=False bypasses the swap-only refusal."""
    scheduler = Scheduler(telemetry_provider=_swap_degraded_telemetry)

    preview = scheduler.preview(
        JobRequest(
            project="proj-a",
            lane=Lane.WORKFLOW_EXECUTION_QUEUE,
            model=None,
            requires_model=False,
        )
    )

    # Admission is not rejected for swap; the preview is either accepted or
    # queued (depending on serialized-lane occupancy from other tests). The
    # contract we care about: no SWAP_DEGRADED refusal for a model-free job.
    assert preview.status in {"accepted", "queued"}
    assert preview.reason_code != SWAP_DEGRADED


def test_autopilot_requires_model_default_true() -> None:
    """Conservative default: new JobRequest + DiscoveredAsset default to True.

    Ensures the bypass is opt-in only — callers must explicitly declare
    `requires_model=False` to escape the guard.
    """
    request = JobRequest(
        project="proj-a",
        lane=Lane.WORKFLOW_EXECUTION_QUEUE,
        model="qwen3:4b",
    )
    assert request.requires_model is True

    asset = DiscoveredAsset(
        path="/tmp/pure_python.py",
        asset_type="script",
        has_entrypoint=True,
    )
    assert asset.requires_model is True

    # Heuristic: scripts with no LLM import return False.
    plain_source = "if __name__ == '__main__':\n    print('hello')\n"
    assert script_requires_model(plain_source) is False

    # Heuristic: scripts with an `import ollama` return True.
    model_source = "import ollama\nif __name__ == '__main__':\n    pass\n"
    assert script_requires_model(model_source) is True

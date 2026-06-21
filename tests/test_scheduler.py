"""Regression tests for the shared workflow scheduler.

These tests focus on cross-surface serialization guarantees for
`local_inference_single` and `workflow_execution_queue`, plus deterministic
`RESOURCE_BUDGET_EXCEEDED` and `QUEUE_TIMEOUT` outcomes.
"""
from __future__ import annotations

import threading
import time

import pytest

from ollarma.guards import RuntimeTelemetry
from ollarma.scheduler import (
    QUEUE_TIMEOUT,
    RESOURCE_BUDGET_EXCEEDED,
    JobRequest,
    Lane,
    Scheduler,
    SchedulerAdmissionError,
)


def _clean_telemetry() -> RuntimeTelemetry:
    return RuntimeTelemetry()


class TestScheduler:
    """Serialization and admission behavior for the shared Scheduler."""

    def test_local_inference_single_queues_second_request(self) -> None:
        """Only one `local_inference_single` lease can be active at a time."""
        scheduler = Scheduler(telemetry_provider=_clean_telemetry)
        first = scheduler.acquire(
            JobRequest(project="proj-a", lane=Lane.LOCAL_INFERENCE_SINGLE, model="qwen3:4b")
        )

        acquired: list[str] = []

        def worker() -> None:
            lease = scheduler.acquire(
                JobRequest(project="proj-b", lane=Lane.LOCAL_INFERENCE_SINGLE, model="qwen3:8b")
            )
            acquired.append(lease.job_id)
            scheduler.release(lease)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.05)

        preview = scheduler.preview(
            JobRequest(project="proj-b", lane=Lane.LOCAL_INFERENCE_SINGLE, model="qwen3:8b")
        )
        assert preview.status == "queued"
        assert preview.queue_depth == 2
        assert acquired == []

        scheduler.release(first)
        thread.join(timeout=1)

        assert len(acquired) == 1

    def test_workflow_execution_queue_remains_single_active(self) -> None:
        """`workflow_execution_queue` keeps one active lease and reports queue depth."""
        scheduler = Scheduler(telemetry_provider=_clean_telemetry)
        first = scheduler.acquire(
            JobRequest(
                project="proj-a",
                lane=Lane.WORKFLOW_EXECUTION_QUEUE,
                model="qwen3-coder:7b",
            )
        )

        acquired: list[str] = []

        def worker() -> None:
            lease = scheduler.acquire(
                JobRequest(
                    project="proj-b",
                    lane=Lane.WORKFLOW_EXECUTION_QUEUE,
                    model="qwen3-coder:7b",
                )
            )
            acquired.append(lease.job_id)
            scheduler.release(lease)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.05)

        snapshot = scheduler.snapshot()
        assert snapshot.active_lane == Lane.WORKFLOW_EXECUTION_QUEUE.value
        assert snapshot.queue_depth == 1

        scheduler.release(first)
        thread.join(timeout=1)

        assert len(acquired) == 1

    def test_workflow_preview_rejects_resource_budget_exceeded(self) -> None:
        """Degraded telemetry with low swap yields RESOURCE_BUDGET_EXCEEDED.

        swap_used_mb=100 is below the 512MB SWAP_DEGRADED threshold so the
        generic RESOURCE_BUDGET_EXCEEDED reason code is emitted (Phase 31-01).
        """
        scheduler = Scheduler(
            telemetry_provider=lambda: RuntimeTelemetry(
                degraded_mode=True,
                degraded_reason="generic degraded mode (low swap)",
                swap_used_mb=100.0,
                telemetry_source="api/ps+ollama ps+sysctl vm.swapusage",
            )
        )

        preview = scheduler.preview(
            JobRequest(
                project="proj-a",
                lane=Lane.WORKFLOW_EXECUTION_QUEUE,
                model="qwen3-coder:7b",
            )
        )

        assert preview.status == "rejected"
        assert preview.reason_code == RESOURCE_BUDGET_EXCEEDED
        assert preview.scheduler.degraded_mode is True

    def test_queue_timeout_emits_queue_timeout_reason(self) -> None:
        """Timed-out queue waits raise SchedulerAdmissionError with QUEUE_TIMEOUT."""
        scheduler = Scheduler(telemetry_provider=_clean_telemetry)
        first = scheduler.acquire(
            JobRequest(project="proj-a", lane=Lane.LOCAL_INFERENCE_SINGLE, model="qwen3:4b")
        )

        try:
            with pytest.raises(SchedulerAdmissionError) as exc_info:
                scheduler.acquire(
                    JobRequest(project="proj-b", lane=Lane.LOCAL_INFERENCE_SINGLE, model="qwen3:8b"),
                    queue_timeout_s=0.01,
                )
        finally:
            scheduler.release(first)

        assert exc_info.value.reason_code == QUEUE_TIMEOUT
        assert "queue wait exceeded" in exc_info.value.detail

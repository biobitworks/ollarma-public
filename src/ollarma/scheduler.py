"""scheduler.py -- Shared scheduler for local workflow lanes.

This module centralizes lane admission and active-lease tracking for service,
HTTP, and MCP surfaces. It does not call Ollama directly; callers submit work
through the scheduler so later phases can add admission control and telemetry
without rewriting every transport surface.
"""
from __future__ import annotations

import errno
import json
import os
import pathlib
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from enum import Enum
from typing import Callable, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ollarma.guards import RuntimeTelemetry, collect_runtime_telemetry

try:
    import fcntl
except ImportError:  # pragma: no cover - Unix hosts are the supported path here.
    fcntl = None


T = TypeVar("T")
QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
RESOURCE_BUDGET_EXCEEDED = "RESOURCE_BUDGET_EXCEEDED"
UNKNOWN_NAMESPACE = "UNKNOWN_NAMESPACE"
SWAP_DEGRADED = "SWAP_DEGRADED"

# --- GPU-08: RAM-relative swap-degraded threshold ---------------------------
# Origin: a fixed 512 MB cap was introduced in 31-01-T1 (commit 412e367,
# 2026-04-15). That floor is far too low for an orchestration host — macOS
# keeps multiple GB in swap as a matter of course, and swap is "sticky" (pages
# stay allocated long after the pressure that created them clears). A fixed
# 512 MB cap therefore pinned the router into rescue-only/degraded mode more or
# less permanently and blocked larger models from using unified GPU+RAM.
#
# The threshold now scales with physical RAM, so progressively larger machines
# may run progressively larger models. Raw swap bytes are demoted to a coarse
# fallback / observability signal; the AUTHORITATIVE hard-stop is the kernel
# memory-pressure gauge (MEMORY_PRESSURE_CRITICAL below), which fires only when
# the system is genuinely thrashing — independent of how much swap is resident.
_SWAP_DEGRADED_FLOOR_MB: float = 512.0
SWAP_DEGRADED_FRACTION_OF_RAM: float = 0.5


def _physical_ram_mb() -> float | None:
    """Total physical RAM in MB via ``sysctl hw.memsize`` (None off-Darwin)."""
    import subprocess  # noqa: PLC0415 -- local to avoid module-load cost
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return int(result.stdout.strip()) / (1024.0 * 1024.0)
    except ValueError:
        return None


def _compute_swap_degraded_threshold_mb() -> float:
    """RAM-relative swap-degraded threshold, with a 512 MB floor.

    Returns ``max(512, physical_RAM_MB * SWAP_DEGRADED_FRACTION_OF_RAM)``.
    Falls back to the 512 MB floor when physical RAM cannot be read (e.g. a
    non-Darwin CI host). The floor keeps tiny/test environments meaningful.
    """
    ram_mb = _physical_ram_mb()
    if ram_mb is None:
        return _SWAP_DEGRADED_FLOOR_MB
    return max(_SWAP_DEGRADED_FLOOR_MB, ram_mb * SWAP_DEGRADED_FRACTION_OF_RAM)


SWAP_DEGRADED_THRESHOLD_MB: float = _compute_swap_degraded_threshold_mb()

# --- GPU-05: macOS kernel memory-pressure signal ---------------------------
# `kern.memorystatus_vm_pressure_level` is the canonical Darwin pressure gauge,
# more reliable than sticky swap counters. Values:
#   1 = normal (no pressure)
#   2 = warn   (active pageouts)
#   4 = critical (reclaim storm; kernel killing background procs)
# Used by the benchmark guard and residency probe to hard-block only when the
# system is genuinely thrashing, while allowing the rescue-model path to run
# under elevated-but-not-critical pressure.
MEMORY_PRESSURE_NORMAL: int = 1
MEMORY_PRESSURE_WARN: int = 2
MEMORY_PRESSURE_CRITICAL: int = 4
MEMORY_PRESSURE_REASON = "MEMORY_PRESSURE_CRITICAL"


def _read_memory_pressure_level() -> int | None:
    """Probe `kern.memorystatus_vm_pressure_level` via sysctl.

    Returns the raw integer level (1/2/4 on Darwin) or ``None`` on non-Darwin
    hosts, subprocess failure, or unparseable output. The probe is cheap
    (sysctl is a direct kernel call) but guarded so a test or Linux CI
    environment does not surface spurious failures.
    """
    import subprocess  # noqa: PLC0415 -- kept local to avoid module-load cost
    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class Lane(str, Enum):
    """Workload lanes defined by the Phase 18 research contract."""

    READ_ONLY_NON_INFERENCE = "read_only_non_inference"
    LOCAL_INFERENCE_SINGLE = "local_inference_single"
    WORKFLOW_EXECUTION_QUEUE = "workflow_execution_queue"


class JobRequest(BaseModel):
    """Scheduler admission request."""

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    project: str | None = None
    lane: Lane
    model: str | None = None
    queue_depth: int = 0
    reason_code: str | None = None
    submitted_at: float = Field(default_factory=time.time)
    requires_model: bool = True
    """DEBT-56.1 per-tier SWAP_DEGRADED guard.

    When True (default, conservative): the workflow-execution SWAP_DEGRADED
    guard refuses admission whenever host swap exceeds the threshold.
    When False: admission still honors RESOURCE_BUDGET_EXCEEDED for any
    non-swap degraded-mode signal, but bypasses the swap-only refusal
    because the caller has declared the work does not load a model.
    """


class JobLease(BaseModel):
    """Granted lease for scheduled work."""

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    job_id: str
    project: str | None = None
    lane: Lane
    model: str | None = None
    queue_depth: int = 0
    reason_code: str | None = None
    submitted_at: float
    acquired_at: float
    released_at: float | None = None


class RuntimeSnapshot(BaseModel):
    """Observable scheduler state for service/transport reporting."""

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    active_job_id: str | None = None
    active_lane: Lane | None = None
    active_model: str | None = None
    active_project: str | None = None
    queue_depth: int = 0
    queue_depth_by_lane: dict[str, int]
    queue_depth_by_namespace: dict[str, int] = {}
    active_read_only: int = 0
    degraded_mode: bool = False
    resource_reason: str | None = None
    swap_used_mb: float | None = None
    loaded_models: tuple[str, ...] = ()
    telemetry_source: str | None = None


class AdmissionDecision(BaseModel):
    """Deterministic admission outcome without requiring execution to start."""

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    status: str
    queue_depth: int = 0
    reason_code: str | None = None
    reason_detail: str | None = None
    scheduler: RuntimeSnapshot


class SchedulerAdmissionError(RuntimeError):
    """Raised when the shared scheduler cannot safely admit a job."""

    def __init__(
        self,
        reason_code: str,
        snapshot: RuntimeSnapshot,
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.snapshot = snapshot
        self.detail = detail


class Scheduler:
    """Scheduler with process-shared serialized lanes for local inference."""

    _SERIALIZED_LANES = {
        Lane.LOCAL_INFERENCE_SINGLE,
        Lane.WORKFLOW_EXECUTION_QUEUE,
    }

    def __init__(
        self,
        telemetry_provider: Callable[[], RuntimeTelemetry] | None = None,
        state_dir: pathlib.Path | None = None,
    ) -> None:
        self._condition = threading.Condition()
        self._queue: dict[str, deque[JobRequest]] = {}   # keyed by namespace/project
        self._rr_namespaces: list[str] = []              # insertion-ordered namespace list
        self._rr_cursor: int = 0                         # next round-robin dispatch position
        self._active_serialized: JobLease | None = None
        self._active_read_only = 0
        self._telemetry_provider = telemetry_provider or collect_runtime_telemetry
        self._state_dir = state_dir or pathlib.Path(
            os.environ.get(
                "OLLARMA_SCHEDULER_DIR",
                pathlib.Path(tempfile.gettempdir()) / "ollarma-scheduler",
            )
        )
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._serialized_lock_path = self._state_dir / "serialized.lock"
        self._serialized_state_path = self._state_dir / "serialized-active.json"
        self._held_lock_files: dict[str, object] = {}

    def _ns_key(self, request: JobRequest) -> str:
        """Map a request to its namespace queue key."""
        return request.project or "__unscoped__"

    def _total_queue_depth(self) -> int:
        """Total queued jobs across all namespace queues."""
        return sum(len(q) for q in self._queue.values())

    def _active_namespaces(self) -> list[str]:
        """Namespaces with at least one queued job, in round-robin order."""
        return [ns for ns in self._rr_namespaces if self._queue.get(ns)]

    def _collect_telemetry(self) -> RuntimeTelemetry:
        """Return a scheduler-ready runtime telemetry snapshot."""
        return self._telemetry_provider()

    def _open_lock_file(self):
        """Open the process-shared serialized-lane lock file."""
        self._serialized_lock_path.touch(exist_ok=True)
        return open(self._serialized_lock_path, "a+b")

    def _serialized_resource_busy(self) -> bool:
        """Return whether another process currently holds the serialized lock."""
        if fcntl is None:
            return False
        probe = self._open_lock_file()
        try:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return True
                raise
            return False
        finally:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            probe.close()

    def _read_external_active_state(self) -> dict[str, str] | None:
        """Read best-effort metadata for a lease held by another process."""
        if not self._serialized_state_path.exists():
            return None
        try:
            data = json.loads(self._serialized_state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return {
            key: value
            for key, value in data.items()
            if isinstance(value, str)
        }

    def _write_external_active_state(self, lease: JobLease) -> None:
        """Publish active serialized-lease metadata for other processes."""
        payload = {
            "job_id": lease.job_id,
            "lane": str(lease.lane),
            "model": lease.model or "",
            "project": lease.project or "",
        }
        tmp_path = self._serialized_state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload))
        tmp_path.replace(self._serialized_state_path)

    def _clear_external_active_state(self, lease: JobLease) -> None:
        """Remove the shared active-state file when releasing the held lease."""
        state = self._read_external_active_state()
        if state and state.get("job_id") != lease.job_id:
            return
        try:
            self._serialized_state_path.unlink()
        except FileNotFoundError:
            pass

    def _try_acquire_process_lock(self, request: JobRequest) -> JobLease | None:
        """Attempt to acquire the process-shared serialized lock without blocking."""
        if fcntl is None:
            lease = JobLease(**request.model_dump(), acquired_at=time.time())
            return lease

        lock_file = self._open_lock_file()
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            raise

        lease = JobLease(**request.model_dump(), acquired_at=time.time())
        self._held_lock_files[lease.job_id] = lock_file
        self._write_external_active_state(lease)
        return lease

    def _build_snapshot_locked(
        self,
        telemetry: RuntimeTelemetry | None = None,
    ) -> RuntimeSnapshot:
        """Build a frozen runtime snapshot from current state and live telemetry."""
        observed = telemetry or self._collect_telemetry()
        active = self._active_serialized
        external_active = None
        if active is None and self._serialized_resource_busy():
            external_active = self._read_external_active_state()
        queue_depth_by_lane = {
            lane.value: sum(
                1 for ns_queue in self._queue.values() for job in ns_queue if job.lane == lane
            )
            for lane in Lane
        }
        queue_depth_by_namespace = {ns: len(q) for ns, q in self._queue.items() if q}
        return RuntimeSnapshot(
            active_job_id=active.job_id if active else external_active.get("job_id") if external_active else None,
            active_lane=(
                active.lane
                if active
                else external_active.get("lane")
                if external_active
                else None
            ),
            active_model=active.model if active else external_active.get("model") if external_active else None,
            active_project=active.project if active else external_active.get("project") if external_active else None,
            queue_depth=self._total_queue_depth(),
            queue_depth_by_lane=queue_depth_by_lane,
            queue_depth_by_namespace=queue_depth_by_namespace,
            active_read_only=self._active_read_only,
            degraded_mode=observed.degraded_mode,
            resource_reason=observed.degraded_reason,
            swap_used_mb=observed.swap_used_mb,
            loaded_models=observed.loaded_models,
            telemetry_source=observed.telemetry_source,
        )

    def _workflow_budget_error(
        self,
        telemetry: RuntimeTelemetry,
        snapshot: RuntimeSnapshot,
        *,
        requires_model: bool = True,
    ) -> SchedulerAdmissionError | None:
        """Return a deterministic workflow admission error when local budget is unsafe.

        DEBT-56.1: when ``requires_model`` is False, bypass the swap-only
        refusal (the caller has declared the work does not load a model,
        so model-swap pressure is not a blocker). Non-swap degraded
        signals still produce ``RESOURCE_BUDGET_EXCEEDED``.
        """
        if not telemetry.degraded_mode:
            return None
        detail = telemetry.degraded_reason or "local runtime telemetry entered degraded mode"
        swap_mb = telemetry.swap_used_mb
        swap_over = swap_mb is not None and swap_mb > SWAP_DEGRADED_THRESHOLD_MB
        if swap_over:
            if not requires_model:
                return None
            return SchedulerAdmissionError(SWAP_DEGRADED, snapshot, detail)
        return SchedulerAdmissionError(
            RESOURCE_BUDGET_EXCEEDED,
            snapshot,
            detail,
        )

    def _remove_queued_job(self, job_id: str) -> None:
        """Remove a pending queued job if it is still present (searches all namespace queues)."""
        for ns_queue in self._queue.values():
            for queued in list(ns_queue):
                if queued.job_id == job_id:
                    ns_queue.remove(queued)
                    return

    def snapshot(self) -> RuntimeSnapshot:
        """Return a frozen snapshot of scheduler state."""
        with self._condition:
            return self._build_snapshot_locked()

    def preview(self, request: JobRequest) -> AdmissionDecision:
        """Inspect the next deterministic admission outcome without blocking."""
        telemetry = self._collect_telemetry()
        with self._condition:
            snapshot = self._build_snapshot_locked(telemetry)
            budget_error = None
            if request.lane == Lane.WORKFLOW_EXECUTION_QUEUE:
                budget_error = self._workflow_budget_error(
                    telemetry, snapshot, requires_model=request.requires_model,
                )
            if budget_error is not None:
                return AdmissionDecision(
                    status="rejected",
                    queue_depth=self._total_queue_depth(),
                    reason_code=budget_error.reason_code,
                    reason_detail=budget_error.detail,
                    scheduler=budget_error.snapshot,
                )

            if request.lane in self._SERIALIZED_LANES and (
                self._active_serialized is not None
                or self._total_queue_depth() > 0
                or self._serialized_resource_busy()
            ):
                return AdmissionDecision(
                    status="queued",
                    queue_depth=self._total_queue_depth() + 1,
                    reason_code=None,
                    reason_detail=None,
                    scheduler=snapshot,
                )

            return AdmissionDecision(
                status="accepted",
                queue_depth=self._total_queue_depth(),
                reason_code=None,
                reason_detail=None,
                scheduler=snapshot,
            )

    def acquire(
        self,
        request: JobRequest,
        *,
        queue_timeout_s: float | None = None,
    ) -> JobLease:
        """Block until the request obtains a lease."""
        telemetry = self._collect_telemetry()
        with self._condition:
            if request.lane not in self._SERIALIZED_LANES:
                self._active_read_only += 1
                return JobLease(
                    **request.model_dump(),
                    acquired_at=time.time(),
                )

            snapshot = self._build_snapshot_locked(telemetry)
            if request.lane == Lane.WORKFLOW_EXECUTION_QUEUE:
                budget_error = self._workflow_budget_error(
                    telemetry, snapshot, requires_model=request.requires_model,
                )
                if budget_error is not None:
                    raise budget_error

            queued_request = request.model_copy(
                update={"queue_depth": self._total_queue_depth() + 1}
            )
            ns_key = self._ns_key(queued_request)
            if ns_key not in self._queue:
                self._queue[ns_key] = deque()
                self._rr_namespaces.append(ns_key)
            self._queue[ns_key].append(queued_request)
            deadline = None
            if queue_timeout_s is not None:
                deadline = time.monotonic() + max(queue_timeout_s, 0.0)

            while True:
                telemetry = self._collect_telemetry()
                snapshot = self._build_snapshot_locked(telemetry)
                if request.lane == Lane.WORKFLOW_EXECUTION_QUEUE:
                    budget_error = self._workflow_budget_error(
                    telemetry, snapshot, requires_model=request.requires_model,
                )
                    if budget_error is not None:
                        self._remove_queued_job(queued_request.job_id)
                        self._condition.notify_all()
                        raise budget_error

                active_ns = self._active_namespaces()
                if active_ns:
                    pick_ns = active_ns[self._rr_cursor % len(active_ns)]
                    is_front = self._queue[pick_ns][0].job_id == queued_request.job_id
                else:
                    is_front = False
                if is_front and self._active_serialized is None:
                    lease = self._try_acquire_process_lock(queued_request)
                    if lease is not None:
                        self._queue[pick_ns].popleft()
                        self._rr_cursor = (self._rr_cursor + 1) % max(1, len(self._rr_namespaces))
                        self._active_serialized = lease
                        return lease

                if deadline is None:
                    self._condition.wait(timeout=0.05)
                    continue

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove_queued_job(queued_request.job_id)
                    timeout_snapshot = self._build_snapshot_locked(telemetry)
                    self._condition.notify_all()
                    raise SchedulerAdmissionError(
                        QUEUE_TIMEOUT,
                        timeout_snapshot,
                        f"queue wait exceeded {queue_timeout_s:.2f}s",
                    )
                self._condition.wait(timeout=min(remaining, 0.05))

    def release(self, lease: JobLease) -> JobLease:
        """Release a previously acquired lease and return the closed record."""
        with self._condition:
            released = lease.model_copy(update={"released_at": time.time()})
            if lease.lane in self._SERIALIZED_LANES:
                if self._active_serialized and self._active_serialized.job_id == lease.job_id:
                    self._active_serialized = None
                lock_file = self._held_lock_files.pop(lease.job_id, None)
                self._clear_external_active_state(lease)
                if lock_file is not None and fcntl is not None:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    finally:
                        lock_file.close()
            else:
                self._active_read_only = max(0, self._active_read_only - 1)

            self._condition.notify_all()
            return released

    @contextmanager
    def lease(
        self,
        request: JobRequest,
        *,
        queue_timeout_s: float | None = None,
    ):
        """Context manager wrapper around acquire/release."""
        lease = self.acquire(request, queue_timeout_s=queue_timeout_s)
        try:
            yield lease
        finally:
            self.release(lease)

    def submit(
        self,
        request: JobRequest,
        fn: Callable[[JobLease], T],
        *,
        queue_timeout_s: float | None = None,
    ) -> T:
        """Run work under a lease and return the callback result."""
        with self.lease(request, queue_timeout_s=queue_timeout_s) as lease:
            return fn(lease)

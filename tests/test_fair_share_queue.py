"""Fair-share round-robin queue tests (NS-05).

Tests cover:
1. Round-robin interleaving across two namespaces
2. Noisy namespace cannot starve a quiet namespace
3. Single namespace behaves as plain FIFO (backward-compat)
4. RuntimeSnapshot exposes queue_depth_by_namespace
5. _remove_queued_job searches all namespace queues
6. Requests with project=None route to __unscoped__ bucket

TDD state:
  RED  — before Task 2: tests expecting queue_depth_by_namespace fail
         because RuntimeSnapshot lacks that field.
  GREEN — after Task 2: all tests pass.
"""
from __future__ import annotations

import pathlib
import tempfile
import threading
import time

from ollarma.guards import RuntimeTelemetry
from ollarma.scheduler import (
    JobRequest,
    Lane,
    Scheduler,
)


def _clean_telemetry() -> RuntimeTelemetry:
    return RuntimeTelemetry()


def _make_scheduler() -> Scheduler:
    """Create a Scheduler with an isolated temp state directory.

    Using a fresh tempdir per test prevents lock-file interference when
    tests are run in parallel or when a prior test did not cleanly release.
    """
    state_dir = pathlib.Path(tempfile.mkdtemp(prefix="ollarma-test-sched-"))
    return Scheduler(telemetry_provider=_clean_telemetry, state_dir=state_dir)


# ---------------------------------------------------------------------------
# Thread helper: acquire and immediately release so no deadlock between tests.
# ---------------------------------------------------------------------------


def _acquire_and_release(scheduler: Scheduler, req: JobRequest, done: threading.Event) -> None:
    """Acquire a lease, then immediately release it, then signal done."""
    lease = scheduler.acquire(req)
    scheduler.release(lease)
    done.set()


def _acquire_only(scheduler: Scheduler, req: JobRequest, acquired: threading.Event) -> None:
    """Acquire a lease and signal — caller must later release or the blocker drains the
    queue so that each enqueued thread exits eventually."""
    lease = scheduler.acquire(req)
    acquired.set()
    # Hold briefly so the test can inspect, then release.
    time.sleep(0.02)
    scheduler.release(lease)


# ---------------------------------------------------------------------------
# Helper: enqueue N jobs for a namespace while blocker holds the lane.
# Returns list of threads (already started, waiting inside acquire()).
# ---------------------------------------------------------------------------


def _enqueue_while_blocked(
    scheduler: Scheduler,
    project: str | None,
    count: int,
) -> list[threading.Thread]:
    """Start `count` background threads, each calling scheduler.acquire(), and
    return them.  The threads will unblock when the calling test releases its
    blocker lease and will self-release immediately after acquiring."""
    threads: list[threading.Thread] = []
    for _ in range(count):
        req = JobRequest(project=project, lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        ev = threading.Event()
        t = threading.Thread(
            target=_acquire_and_release,
            args=(scheduler, req, ev),
            daemon=True,
        )
        threads.append(t)
        t.start()
    return threads


# ---------------------------------------------------------------------------
# Test 1: Round-robin interleaves two namespaces
# ---------------------------------------------------------------------------


class TestRoundRobinDispatch:
    def test_round_robin_interleaves_two_namespaces(self) -> None:
        """After RR conversion, proj-a and proj-b are both dispatched.

        With round-robin, both namespaces must get at least one dispatch slot
        each.  This passes in FIFO too (both get dispatched eventually), so
        this test is a baseline correctness guard — not a RED indicator.
        """
        scheduler = _make_scheduler()
        dispatch_order: list[str] = []
        lock = threading.Lock()

        def _worker(project: str) -> None:
            req = JobRequest(project=project, lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
            lease = scheduler.acquire(req)
            with lock:
                dispatch_order.append(project)
            time.sleep(0.01)
            scheduler.release(lease)

        t_a = threading.Thread(target=_worker, args=("proj-a",), daemon=True)
        t_b = threading.Thread(target=_worker, args=("proj-b",), daemon=True)
        t_a.start()
        t_b.start()
        t_a.join(timeout=3)
        t_b.join(timeout=3)

        assert set(dispatch_order) == {"proj-a", "proj-b"}, (
            f"Expected both proj-a and proj-b dispatched; got {dispatch_order}"
        )

    def test_single_namespace_behaves_as_fifo(self) -> None:
        """Single namespace: 3 sequential jobs are dispatched in submission order."""
        scheduler = _make_scheduler()
        dispatch_order: list[str] = []

        def acquire_release(tag: str) -> None:
            req = JobRequest(project="proj-only", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
            lease = scheduler.acquire(req)
            dispatch_order.append(tag)
            scheduler.release(lease)

        # Run sequentially so order is deterministic.
        for tag in ["job-1", "job-2", "job-3"]:
            acquire_release(tag)

        assert dispatch_order == ["job-1", "job-2", "job-3"], (
            f"Single namespace must preserve FIFO order; got {dispatch_order}"
        )


# ---------------------------------------------------------------------------
# Test 2: Noisy namespace cannot starve quiet namespace
# ---------------------------------------------------------------------------


class TestStarvationPrevention:
    def test_noisy_namespace_cannot_starve_quiet_namespace(self) -> None:
        """proj-a with 10 jobs and proj-b with 1 job — both namespaces
        must be separately tracked so RR can serve proj-b fairly.

        In RED phase: RuntimeSnapshot.queue_depth_by_namespace is missing
        because the queue is still a single deque.  When both proj-a and
        proj-b are enqueued, the snapshot will NOT show separate per-namespace
        depths — that field does not exist.

        In GREEN phase: snapshot shows proj-a:10 and proj-b:1 as separate
        buckets, and total queue_depth == 11.
        """
        scheduler = _make_scheduler()

        # Hold the lane so nothing dispatches while we set up the queue.
        blocker = scheduler.acquire(
            JobRequest(project="setup", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        )

        # Start 10 proj-a threads (all blocked inside acquire() waiting for blocker).
        threads_a = _enqueue_while_blocked(scheduler, "proj-a", 10)
        time.sleep(0.06)  # let them all enqueue

        # Start 1 proj-b thread.
        threads_b = _enqueue_while_blocked(scheduler, "proj-b", 1)
        time.sleep(0.06)  # let it enqueue

        snap = scheduler.snapshot()

        # --- RED assertions (will fail before Phase 30 Task 2) ---
        assert hasattr(snap, "queue_depth_by_namespace"), (
            "RuntimeSnapshot missing queue_depth_by_namespace — Phase 30 not applied"
        )
        ns_depths = snap.queue_depth_by_namespace
        assert "proj-a" in ns_depths, (
            f"proj-a missing from queue_depth_by_namespace: {ns_depths}"
        )
        assert "proj-b" in ns_depths, (
            f"proj-b missing from per-namespace tracking; starvation cannot be prevented: {ns_depths}"
        )
        assert ns_depths["proj-a"] == 10, f"Expected proj-a depth 10; got {ns_depths}"
        assert ns_depths["proj-b"] == 1, f"Expected proj-b depth 1; got {ns_depths}"
        assert snap.queue_depth == 11, f"Expected total queue_depth 11; got {snap.queue_depth}"

        # Release — let threads drain.
        scheduler.release(blocker)
        for t in threads_a + threads_b:
            t.join(timeout=2)


# ---------------------------------------------------------------------------
# Test 3: RuntimeSnapshot exposes queue_depth_by_namespace
# ---------------------------------------------------------------------------


class TestSnapshotObservability:
    def test_snapshot_exposes_queue_depth_by_namespace(self) -> None:
        """snapshot().queue_depth_by_namespace reflects per-namespace counts.

        RED: RuntimeSnapshot has no queue_depth_by_namespace field → AttributeError.
        GREEN: field present and populated correctly.
        """
        scheduler = _make_scheduler()

        # Hold the lane so we can count queue state before any dispatch.
        blocker = scheduler.acquire(
            JobRequest(project="blocker", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        )

        threads_a = _enqueue_while_blocked(scheduler, "proj-a", 3)
        threads_b = _enqueue_while_blocked(scheduler, "proj-b", 2)
        time.sleep(0.1)  # let all 5 enqueue

        snap = scheduler.snapshot()

        # --- RED: this assertion fails because the field doesn't exist yet ---
        assert hasattr(snap, "queue_depth_by_namespace"), (
            "RuntimeSnapshot is missing queue_depth_by_namespace field (Phase 30 Task 2 not applied)"
        )
        assert snap.queue_depth_by_namespace.get("proj-a") == 3, (
            f"Expected proj-a depth 3; got {snap.queue_depth_by_namespace}"
        )
        assert snap.queue_depth_by_namespace.get("proj-b") == 2, (
            f"Expected proj-b depth 2; got {snap.queue_depth_by_namespace}"
        )

        scheduler.release(blocker)
        for t in threads_a + threads_b:
            t.join(timeout=2)

    def test_preview_exposes_queue_depth_by_namespace(self) -> None:
        """preview().scheduler.queue_depth_by_namespace is populated.

        RED: same missing-field failure as snapshot test.
        """
        scheduler = _make_scheduler()

        blocker = scheduler.acquire(
            JobRequest(project="blocker", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        )
        threads_x = _enqueue_while_blocked(scheduler, "ns-x", 2)
        time.sleep(0.05)

        probe = JobRequest(project="ns-x", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        decision = scheduler.preview(probe)
        snap = decision.scheduler

        assert hasattr(snap, "queue_depth_by_namespace"), (
            "RuntimeSnapshot in AdmissionDecision is missing queue_depth_by_namespace"
        )
        assert snap.queue_depth_by_namespace.get("ns-x") == 2, (
            f"Expected ns-x depth 2; got {snap.queue_depth_by_namespace}"
        )

        scheduler.release(blocker)
        for t in threads_x:
            t.join(timeout=2)


# ---------------------------------------------------------------------------
# Test 4: _remove_queued_job searches all namespace queues
# ---------------------------------------------------------------------------


class TestRemoveQueuedJob:
    def test_remove_queued_job_searches_all_namespaces(self) -> None:
        """_remove_queued_job must find and remove a job regardless of which
        namespace queue it lives in.

        This test works in both RED and GREEN phases because it accesses
        scheduler._queue directly.  In RED, _queue is a deque; in GREEN,
        a dict[str, deque].  Both cases are handled by the dual-path check.
        """
        scheduler = _make_scheduler()

        blocker = scheduler.acquire(
            JobRequest(project="blocker", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        )

        req_a = JobRequest(project="alpha", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        req_b = JobRequest(project="beta", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")

        ev_a = threading.Event()
        ev_b = threading.Event()
        t_a = threading.Thread(
            target=_acquire_and_release, args=(scheduler, req_a, ev_a), daemon=True
        )
        t_b = threading.Thread(
            target=_acquire_and_release, args=(scheduler, req_b, ev_b), daemon=True
        )
        t_a.start()
        t_b.start()
        time.sleep(0.05)  # let both enqueue

        # Verify 2 queued.
        with scheduler._condition:
            if isinstance(scheduler._queue, dict):
                total = sum(len(q) for q in scheduler._queue.values())
            else:
                total = len(scheduler._queue)
        assert total == 2, f"Expected 2 queued jobs; found {total}"

        # Remove beta.
        with scheduler._condition:
            scheduler._remove_queued_job(req_b.job_id)

        # Verify only alpha remains.
        with scheduler._condition:
            if isinstance(scheduler._queue, dict):
                total_after = sum(len(q) for q in scheduler._queue.values())
                remaining_ids = [j.job_id for q in scheduler._queue.values() for j in q]
            else:
                total_after = len(scheduler._queue)
                remaining_ids = [j.job_id for j in scheduler._queue]

        assert total_after == 1, f"Expected 1 job after removal; found {total_after}"
        assert req_a.job_id in remaining_ids, "alpha job should still be queued"
        assert req_b.job_id not in remaining_ids, "beta job should have been removed"

        # Let alpha complete.
        scheduler.release(blocker)
        t_a.join(timeout=2)
        t_b.join(timeout=1)  # beta was removed — its thread never gets dispatched


# ---------------------------------------------------------------------------
# Test 5: Unscoped requests route to __unscoped__ bucket
# ---------------------------------------------------------------------------


class TestUnscopedRouting:
    def test_unscoped_requests_route_to_unscoped_bucket(self) -> None:
        """JobRequest with project=None must appear in __unscoped__ namespace.

        RED: queue_depth_by_namespace missing → assertion fails.
        GREEN: snap.queue_depth_by_namespace['__unscoped__'] == 1.
        """
        scheduler = _make_scheduler()

        blocker = scheduler.acquire(
            JobRequest(project="blocker", lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        )

        req = JobRequest(project=None, lane=Lane.LOCAL_INFERENCE_SINGLE, model="m")
        ev = threading.Event()
        t = threading.Thread(
            target=_acquire_and_release, args=(scheduler, req, ev), daemon=True
        )
        t.start()
        time.sleep(0.05)

        snap = scheduler.snapshot()
        assert hasattr(snap, "queue_depth_by_namespace"), (
            "RuntimeSnapshot missing queue_depth_by_namespace"
        )
        assert snap.queue_depth_by_namespace.get("__unscoped__") == 1, (
            f"Expected __unscoped__ depth 1; got {snap.queue_depth_by_namespace}"
        )

        scheduler.release(blocker)
        t.join(timeout=2)

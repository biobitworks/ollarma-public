"""Tests for NS-04 idempotency module.

Covers:
  - Task 1: IdempotencyStore get/put, TTL eviction, compound key, per-namespace isolation,
    concurrent race safety, 100k row cap
  - Task 2: Service-layer wiring in route_prompt() and submit_workflow()
"""
from __future__ import annotations

import datetime
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Task 1 tests — idempotency module unit tests
# ---------------------------------------------------------------------------


def test_first_call_stores_result(tmp_path, monkeypatch):
    """put() stores result_json; get() returns the stored string."""
    import ollarma.idempotency as idem

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    store = idem.IdempotencyStore("test-ns:")
    store.put("key-abc", '{"result": 1}', transport="http")
    got = store.get("key-abc")
    assert got == '{"result": 1}'


def test_second_call_returns_stored_result_without_calling_service(tmp_path, monkeypatch):
    """get() after put() returns the same value a second time."""
    import ollarma.idempotency as idem

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    store = idem.IdempotencyStore("test-ns:")
    store.put("key-xyz", '{"answer": "hello"}', transport="http")
    first = store.get("key-xyz")
    second = store.get("key-xyz")
    assert first == second == '{"answer": "hello"}'


def test_expired_result_is_evicted_on_write(tmp_path, monkeypatch):
    """A record with expires_at in the past is deleted on the next put() call."""
    import ollarma.idempotency as idem

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    store = idem.IdempotencyStore("expire-ns:")
    # Insert an already-expired record directly into SQLite
    store._connect()  # ensure schema created
    past = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    created = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = store._conn
    conn.execute(
        "INSERT INTO idempotency_cache (compound_key, transport, result_json, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("stale-key", "http", '{"old": true}', created, past),
    )
    conn.commit()

    # put() on a different key should trigger eviction of the stale row
    store.put("fresh-key", '{"new": true}', transport="http")

    # The stale key should now be gone
    result = store.get("stale-key")
    assert result is None

    # Fresh key should still be there
    result = store.get("fresh-key")
    assert result == '{"new": true}'


def test_compound_key_includes_namespace_transport_and_content_hash(tmp_path, monkeypatch):
    """Same body with different namespace produces different compound keys."""
    import ollarma.idempotency as idem

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)

    body = {"prompt": "hello world", "project": "demo", "model": None}
    key_a = idem.make_compound_key("ns-a:", "http", body)
    key_b = idem.make_compound_key("other:", "http", body)

    assert key_a != key_b
    # Also verify same inputs produce same key (deterministic)
    key_a2 = idem.make_compound_key("ns-a:", "http", body)
    assert key_a == key_a2


def test_per_namespace_isolation(tmp_path, monkeypatch):
    """Different namespaces use different SQLite files — no cross-namespace hits."""
    import ollarma.idempotency as idem

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    body = {"prompt": "shared prompt", "project": "proj", "model": None}
    key_ns_a = idem.make_compound_key("ns-a:", "http", body)
    key_ns_b = idem.make_compound_key("ns-b:", "http", body)

    # Store a result in namespace A
    store_a = idem.get_store("ns-a:")
    store_a.put(key_ns_a, '{"from": "a"}', transport="http")

    # Namespace B store should have no hit for the key that only ns-a knows
    store_b = idem.get_store("ns-b:")
    # key_ns_b is a different key — ns-b doesn't know key_ns_a either
    assert store_b.get(key_ns_b) is None
    # And even if we look up key_ns_a in store_b's DB, it won't be there
    assert store_b.get(key_ns_a) is None

    # Verify they use separate files
    file_a = tmp_path / "ns-a_.sqlite"
    file_b = tmp_path / "ns-b_.sqlite"
    assert file_a.exists()
    assert file_b.exists()


def test_concurrent_race_does_not_duplicate_store(tmp_path, monkeypatch):
    """Two threads calling put() with same key simultaneously don't raise exceptions."""
    import ollarma.idempotency as idem

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    store = idem.get_store("race-ns:")
    key = "race-key-001"
    barrier = threading.Barrier(2)
    errors = []

    def writer(result_json: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.put(key, result_json, transport="http")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=('{"thread": 1}',))
    t2 = threading.Thread(target=writer, args=('{"thread": 2}',))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # No exceptions from either thread
    assert errors == [], f"Concurrent put raised: {errors}"

    # Row count must be exactly 1 (INSERT OR IGNORE semantics)
    conn = store._connect()
    count = conn.execute(
        "SELECT COUNT(*) FROM idempotency_cache WHERE compound_key = ?", (key,)
    ).fetchone()[0]
    assert count == 1

    # get() returns a valid JSON string (one of the two)
    result = store.get(key)
    assert result is not None
    assert '"thread"' in result


def test_100k_row_cap_evicts_oldest_10k(tmp_path, monkeypatch):
    """When row count hits 100001, put() evicts 10000 oldest rows."""
    import ollarma.idempotency as idem

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    store = idem.get_store("cap-ns:")
    conn = store._connect()

    # Insert 100001 rows directly (no loop through put — too slow)
    future_expiry = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=48)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Use executemany for speed
    rows = []
    for i in range(idem._ROW_CAP + 1):
        # created_at is ordered so oldest = row 0
        created = (
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(seconds=i)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append((f"cap-key-{i:07d}", "http", '{"x":1}', created, future_expiry))

    conn.executemany(
        "INSERT INTO idempotency_cache (compound_key, transport, result_json, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

    initial_count = conn.execute("SELECT COUNT(*) FROM idempotency_cache").fetchone()[0]
    assert initial_count == idem._ROW_CAP + 1

    # put() on a new key should trigger cap eviction
    store.put("trigger-key", '{"trigger": true}', transport="http")

    final_count = conn.execute("SELECT COUNT(*) FROM idempotency_cache").fetchone()[0]
    # After evicting _EVICT_BATCH (10k) and adding 1 new row:
    # 100001 - 10000 + 1 = 90002
    expected = idem._ROW_CAP + 1 - idem._EVICT_BATCH + 1
    assert final_count == expected, f"Expected {expected}, got {final_count}"

    # The trigger key itself should be present
    assert store.get("trigger-key") == '{"trigger": true}'


# ---------------------------------------------------------------------------
# Task 2 tests — service.route_prompt() and service.submit_workflow() wiring
# ---------------------------------------------------------------------------


def test_route_prompt_idempotency_replay_skips_inference(tmp_path, monkeypatch):
    """Second identical route_prompt() call within TTL returns stored result without calling _route_prompt_impl."""
    import ollarma.idempotency as idem
    import ollarma.service as svc

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    # Build a fake RouteResult to return from the mock
    fake_result = svc.RouteResult(
        final_response="cached answer",
        tool_calls_count=0,
        model="phi4-mini",
        project="demo-project",
        lane="grounded_local",
    )

    call_count = {"n": 0}
    original_impl = svc._route_prompt_impl

    def counting_impl(*args, **kwargs):
        call_count["n"] += 1
        return fake_result.model_dump()

    # Patch _route_prompt_impl and NAMESPACE_REGISTRY
    with (
        patch.object(svc, "_route_prompt_impl", side_effect=counting_impl),
        patch.object(svc.NAMESPACE_REGISTRY, "is_registered", return_value=True),
        patch.object(svc, "resolve_project_adapter") as mock_adapter,
        patch.object(svc, "_append_route_receipt_log"),
    ):
        mock_adapter.return_value = MagicMock(
            project_name="demo-project",
            project_root=str(tmp_path),
        )
        result1 = svc.route_prompt(
            prompt="what is the answer",
            project="demo-project",
            model="phi4-mini",
            namespace_prefix="ns-test:",
        )
        result2 = svc.route_prompt(
            prompt="what is the answer",
            project="demo-project",
            model="phi4-mini",
            namespace_prefix="ns-test:",
        )

    assert call_count["n"] == 1, f"_route_prompt_impl called {call_count['n']} times, expected 1"
    assert result1.final_response == result2.final_response == "cached answer"


def test_route_result_is_memoizable_predicate():
    """Durable local answers cache; transient escalations do not."""
    import ollarma.service as svc

    def rr(lane, reason_code=None):
        return svc.RouteResult(
            final_response="x", tool_calls_count=0, project="p",
            lane=lane, reason_code=reason_code,
        )

    # Durable lanes are memoizable.
    assert svc._route_result_is_memoizable(rr("kb_direct"))
    assert svc._route_result_is_memoizable(rr("grounded_local_synthesis"))
    assert svc._route_result_is_memoizable(rr("orchestrator_handoff"))
    # Escalation lane is never memoizable, regardless of reason.
    assert not svc._route_result_is_memoizable(rr("frontier_or_human", "KB_STALE"))
    assert not svc._route_result_is_memoizable(rr("frontier_or_human", None))
    # Transient reason codes are not memoizable even on a non-escalation lane.
    assert not svc._route_result_is_memoizable(rr("kb_direct", "SWAP_DEGRADED"))
    assert not svc._route_result_is_memoizable(rr("kb_direct", "INSUFFICIENT_GROUNDED_EVIDENCE"))


def test_route_prompt_escalation_is_not_memoized(tmp_path, monkeypatch):
    """A frontier_or_human escalation must be re-evaluated on the next identical
    call — not replayed from cache — so a remediated KB/swap/selection condition
    actually takes effect (regression for the stale-KB cloud-redirect trap)."""
    import ollarma.idempotency as idem
    import ollarma.service as svc

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    escalation = svc.RouteResult(
        final_response="KB evidence is stale. Rebuild the KB or escalate.",
        tool_calls_count=0,
        model=None,
        project="demo-project",
        lane="frontier_or_human",
        reason_code="KB_STALE",
        kb_status="stale",
    )

    call_count = {"n": 0}

    def counting_impl(*args, **kwargs):
        call_count["n"] += 1
        return escalation.model_dump()

    with (
        patch.object(svc, "_route_prompt_impl", side_effect=counting_impl),
        patch.object(svc.NAMESPACE_REGISTRY, "is_registered", return_value=True),
        patch.object(svc, "resolve_project_adapter") as mock_adapter,
        patch.object(svc, "_append_route_receipt_log"),
    ):
        mock_adapter.return_value = MagicMock(
            project_name="demo-project",
            project_root=str(tmp_path),
        )
        svc.route_prompt(
            prompt="summarize", project="demo-project", model=None,
            namespace_prefix="ns-test:",
        )
        svc.route_prompt(
            prompt="summarize", project="demo-project", model=None,
            namespace_prefix="ns-test:",
        )

    assert call_count["n"] == 2, (
        f"escalation was replayed from cache (impl called {call_count['n']}x, expected 2)"
    )


def test_route_prompt_idempotency_replay_respects_namespace(tmp_path, monkeypatch):
    """Different namespace_prefix values produce different compound keys — no cross-namespace replay."""
    import ollarma.idempotency as idem
    import ollarma.service as svc

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    fake_result = svc.RouteResult(
        final_response="fresh result",
        tool_calls_count=0,
        model="phi4-mini",
        project="demo-project",
        lane="grounded_local",
    )

    call_count = {"n": 0}

    def counting_impl(*args, **kwargs):
        call_count["n"] += 1
        return fake_result.model_dump()

    with (
        patch.object(svc, "_route_prompt_impl", side_effect=counting_impl),
        patch.object(svc.NAMESPACE_REGISTRY, "is_registered", return_value=True),
        patch.object(svc, "resolve_project_adapter") as mock_adapter,
        patch.object(svc, "_append_route_receipt_log"),
    ):
        mock_adapter.return_value = MagicMock(
            project_name="demo-project",
            project_root=str(tmp_path),
        )
        svc.route_prompt(
            prompt="identical prompt",
            project="demo-project",
            model="phi4-mini",
            namespace_prefix="ns-alpha:",
        )
        svc.route_prompt(
            prompt="identical prompt",
            project="demo-project",
            model="phi4-mini",
            namespace_prefix="ns-beta:",
        )

    # Different namespaces → different keys → both calls go to _route_prompt_impl
    assert call_count["n"] == 2, f"Expected 2 inference calls, got {call_count['n']}"


def test_route_prompt_no_namespace_idempotency_uses_unscoped(tmp_path, monkeypatch):
    """route_prompt called without namespace_prefix uses the '' / __unscoped__ store."""
    import ollarma.idempotency as idem
    import ollarma.service as svc

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    fake_result = svc.RouteResult(
        final_response="unscoped answer",
        tool_calls_count=0,
        model=None,
        project="demo-project",
        lane="grounded_local",
    )

    call_count = {"n": 0}

    def counting_impl(*args, **kwargs):
        call_count["n"] += 1
        return fake_result.model_dump()

    with (
        patch.object(svc, "_route_prompt_impl", side_effect=counting_impl),
        patch.object(svc, "resolve_project_adapter") as mock_adapter,
        patch.object(svc, "_append_route_receipt_log"),
    ):
        mock_adapter.return_value = MagicMock(
            project_name="demo-project",
            project_root=str(tmp_path),
        )
        # First call — no namespace
        svc.route_prompt(
            prompt="no namespace prompt",
            project="demo-project",
        )
        # Second call — same args, still no namespace
        result2 = svc.route_prompt(
            prompt="no namespace prompt",
            project="demo-project",
        )

    # Only one inference call — second was replayed from __unscoped__ store
    assert call_count["n"] == 1, f"Expected 1 inference call (unscoped replay), got {call_count['n']}"
    assert result2.final_response == "unscoped answer"

    # Verify the __unscoped__.sqlite file exists
    assert (tmp_path / "__unscoped__.sqlite").exists()


def test_submit_workflow_idempotency_replay_skips_scheduler(tmp_path, monkeypatch):
    """Second identical submit_workflow() call returns original result without scheduler admission."""
    import ollarma.idempotency as idem
    import ollarma.service as svc

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path)
    monkeypatch.setattr(idem, "_stores", {})

    # Build a fake WorkflowSubmissionResult
    from ollarma.scheduler import RuntimeSnapshot
    fake_snapshot = RuntimeSnapshot(
        queue_depth=0,
        queue_depth_by_lane={"workflow_execution_queue": 0},
    )
    fake_wf_result = svc.WorkflowSubmissionResult(
        project="demo-project",
        manifest_ref={"path": "/tmp/manifest.yaml"},
        manifest_digest="abc123",
        run_id="run-001",
        step_id="step-01",
        task_class="ScriptTask",
        lane="workflow_execution_queue",
        status="accepted",
        model=None,
        scheduler=fake_snapshot,
    )

    call_count = {"n": 0}

    def counting_submit_workflow(*args, **kwargs):
        # This records when the internal workflow submission path is entered
        call_count["n"] += 1
        return fake_wf_result

    with (
        patch.object(svc.NAMESPACE_REGISTRY, "is_registered", return_value=True),
        patch.object(svc, "resolve_project_adapter") as mock_adapter,
        patch.object(svc, "load_workflow_manifest") as mock_manifest,
        patch.object(svc, "_resolve_workflow_step") as mock_step,
        patch.object(svc, "_workflow_class_from_task_class") as mock_wf_class,
        patch.object(svc, "_workflow_dependency_detail", return_value=None),
        patch.object(svc, "resolve_selection") as mock_sel,
        patch.object(svc._scheduler, "preview") as mock_preview,
        patch.object(svc, "_record_workflow_submission") as mock_record,
        patch.object(svc, "_workflow_submission_result", side_effect=counting_submit_workflow),
    ):
        mock_adapter.return_value = MagicMock(
            project_name="demo-project",
            project_root=str(tmp_path),
            namespace_prefix="ns-wf:",
        )
        mock_manifest.return_value = MagicMock(
            run_id="run-001",
            model_dump=lambda mode=None: {"path": "/tmp/manifest.yaml"},
        )
        mock_step.return_value = MagicMock(
            step_id="step-01",
            task_type="ScriptTask",
        )
        mock_wf_class.return_value = "ScriptWorkload"
        mock_sel.return_value = "phi4-mini"
        mock_preview.return_value = MagicMock(
            reason_code=None,
            status="available",
            queue_depth=0,
            scheduler=fake_snapshot,
        )
        mock_record.return_value = ({"path": "ref"}, {"path": "cp"})

        result1 = svc.submit_workflow(
            project="demo-project",
            manifest_ref="/tmp/manifest.yaml",
            step_id="step-01",
            namespace_prefix="ns-wf:",
        )
        result2 = svc.submit_workflow(
            project="demo-project",
            manifest_ref="/tmp/manifest.yaml",
            step_id="step-01",
            namespace_prefix="ns-wf:",
        )

    # _workflow_submission_result (the scheduler admission path) called exactly once
    assert call_count["n"] == 1, f"Expected 1 scheduler call, got {call_count['n']}"
    assert result1.run_id == result2.run_id

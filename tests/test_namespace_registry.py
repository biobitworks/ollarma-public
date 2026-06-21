"""Tests for ollarma.namespace_registry -- NS-01 and NS-02 coverage.

Tests cover:
  NS-01: namespace_prefix carried through registry; is_registered returns correct results
  NS-02: unregistered non-empty namespace fails closed; empty namespace passes through unscoped
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(namespace_prefix: str, project_name: str = "demo-project"):
    """Build a minimal AdapterConfig-like object for monkeypatching."""
    from ollarma.fleet import AdapterConfig

    return AdapterConfig(
        project_name=project_name,
        project_root="/tmp/test-project",
        namespace_prefix=namespace_prefix,
    )


def _make_fleet(*prefixes: str) -> dict:
    """Return a dict keyed by project name with adapters having given prefixes."""
    fleet = {}
    for i, prefix in enumerate(prefixes):
        name = f"project-{i}"
        fleet[name] = _make_adapter(prefix, project_name=name)
    return fleet


# ---------------------------------------------------------------------------
# Test: registered namespace passes validation
# ---------------------------------------------------------------------------


def test_registered_namespace_passes_validation(monkeypatch):
    """NamespaceRegistry.is_registered returns True for a known namespace prefix."""
    from ollarma import service as svc
    from ollarma.namespace_registry import NamespaceRegistry

    fleet = _make_fleet("ollarma-demo:")
    monkeypatch.setattr(svc, "list_projects", lambda **kw: fleet)

    registry = NamespaceRegistry()
    assert registry.is_registered("ollarma-demo:") is True


# ---------------------------------------------------------------------------
# Test: unregistered namespace returns unknown namespace error signal
# ---------------------------------------------------------------------------


def test_unregistered_namespace_returns_unknown_namespace_error(monkeypatch):
    """is_registered returns False for an unregistered prefix."""
    from ollarma import service as svc
    from ollarma.namespace_registry import NamespaceRegistry

    fleet = _make_fleet("ollarma-demo:")
    monkeypatch.setattr(svc, "list_projects", lambda **kw: fleet)

    registry = NamespaceRegistry()
    assert registry.is_registered("bogus:") is False


# ---------------------------------------------------------------------------
# Test: empty namespace_prefix passes through as unscoped (backward-compat)
# ---------------------------------------------------------------------------


def test_empty_namespace_prefix_passes_through_unscoped(monkeypatch):
    """is_registered('') returns True — empty maps to __unscoped__, never fails NS-02."""
    from ollarma import service as svc
    from ollarma.namespace_registry import NamespaceRegistry

    fleet = _make_fleet("ollarma-demo:")
    monkeypatch.setattr(svc, "list_projects", lambda **kw: fleet)

    registry = NamespaceRegistry()
    # Empty string must pass without even loading from fleet
    assert registry.is_registered("") is True


# ---------------------------------------------------------------------------
# Test: UNKNOWN_NAMESPACE reason code is present in the ReasonCode enum
# ---------------------------------------------------------------------------


def test_unknown_namespace_reason_code_in_enum():
    """ReasonCode.UNKNOWN_NAMESPACE exists and has the correct string value."""
    from ollarma.escalation import ReasonCode

    assert ReasonCode.UNKNOWN_NAMESPACE == "UNKNOWN_NAMESPACE"
    assert ReasonCode.UNKNOWN_NAMESPACE.value == "UNKNOWN_NAMESPACE"


# ---------------------------------------------------------------------------
# Test: RunReceipt.namespace field serializes correctly with default ""
# ---------------------------------------------------------------------------


def test_receipt_namespace_field_is_set():
    """RunReceipt accepts namespace field; default is '' and explicit value serializes."""
    from ollarma.run_ledger import RunReceipt

    # Default empty
    receipt_default = RunReceipt(
        run_id="run-1",
        stage="execute",
        step_id="step-1",
        task_or_command="test",
        lane="local_inference_single",
        status="accepted",
        created_at="2026-04-15T00:00:00Z",
    )
    assert receipt_default.namespace == ""
    dumped_default = receipt_default.model_dump()
    assert dumped_default["namespace"] == ""

    # Explicit namespace value
    receipt_ns = RunReceipt(
        run_id="run-2",
        stage="execute",
        step_id="step-2",
        task_or_command="test",
        lane="local_inference_single",
        status="accepted",
        namespace="foo:",
        created_at="2026-04-15T00:00:00Z",
    )
    assert receipt_ns.namespace == "foo:"
    dumped_ns = receipt_ns.model_dump()
    assert dumped_ns["namespace"] == "foo:"


# ---------------------------------------------------------------------------
# Test: registry loads once, not on every is_registered call
# ---------------------------------------------------------------------------


def test_registry_loaded_once_not_per_call(monkeypatch):
    """list_projects is called only once even when is_registered is called 5 times."""
    from ollarma import service as svc
    from ollarma.namespace_registry import NamespaceRegistry

    call_count = 0
    fleet = _make_fleet("ollarma-demo:")

    def counting_list_projects(**kw):
        nonlocal call_count
        call_count += 1
        return fleet

    monkeypatch.setattr(svc, "list_projects", counting_list_projects)

    registry = NamespaceRegistry()
    for _ in range(5):
        registry.is_registered("ollarma-demo:")

    assert call_count == 1, f"Expected list_projects called once, got {call_count}"

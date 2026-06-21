"""test_gateway_schema.py -- FrontierReceipt schema contract tests (Phase 57-01).

Covers:
- Round-trip through model_dump(mode="json") → model_validate with no loss.
- Frozen enforcement (mutation raises).
- schema_version default = 1.
- Decimal cost_usd serializes as string (NOT float).
- status enum restriction.
- context_truncated trio consistency (model_validator).
- GatewayReasonCode extension set + interop with EscalationReceipt.ReasonCode.

Tests exercise the real model — no monkeypatching of internals.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ollarma.escalation import ReasonCode
from ollarma.gateway import FrontierReceipt, GatewayReasonCode
from ollarma.evidence import GENESIS_PARENT_HASH


def _minimum_receipt_kwargs(**overrides):
    base = dict(
        escalation_receipt_id="esc-1",
        admission_receipt_hash="a" * 64,
        provider="anthropic",
        model_id="claude-opus-4",
        status="succeeded",
    )
    base.update(overrides)
    return base


def test_frontier_receipt_round_trip():
    r = FrontierReceipt(**_minimum_receipt_kwargs(
        prompt_tokens=10,
        response_tokens=20,
        cost_usd=Decimal("0.0032"),
        latency_ms=123,
        provider_request_id="req-xyz",
    ))
    dumped = r.model_dump(mode="json")
    # Round-trip through JSON
    roundtripped = FrontierReceipt.model_validate(json.loads(json.dumps(dumped)))
    assert roundtripped.escalation_receipt_id == "esc-1"
    assert roundtripped.admission_receipt_hash == "a" * 64
    assert roundtripped.provider == "anthropic"
    assert roundtripped.model_id == "claude-opus-4"
    assert roundtripped.provider_request_id == "req-xyz"
    assert roundtripped.prompt_tokens == 10
    assert roundtripped.response_tokens == 20
    assert roundtripped.cost_usd == Decimal("0.0032")
    assert roundtripped.latency_ms == 123
    assert roundtripped.status == "succeeded"
    assert roundtripped.schema_version == 1


def test_frontier_receipt_is_frozen():
    r = FrontierReceipt(**_minimum_receipt_kwargs())
    with pytest.raises((ValidationError, TypeError)):
        r.provider = "openai"  # type: ignore[misc]


def test_schema_version_default_and_in_dump():
    r = FrontierReceipt(**_minimum_receipt_kwargs())
    assert r.schema_version == 1
    dumped = r.model_dump(mode="json")
    assert dumped["schema_version"] == 1


def test_cost_usd_serializes_as_string_not_float():
    r = FrontierReceipt(**_minimum_receipt_kwargs(cost_usd=Decimal("0.0032")))
    dumped = r.model_dump(mode="json")
    assert dumped["cost_usd"] == "0.0032"
    assert isinstance(dumped["cost_usd"], str)
    # Also verify int input works
    r_int = FrontierReceipt(**_minimum_receipt_kwargs(cost_usd=0))
    assert r_int.cost_usd == Decimal("0")
    # And string input works
    r_str = FrontierReceipt(**_minimum_receipt_kwargs(cost_usd="1.23"))
    assert r_str.cost_usd == Decimal("1.23")
    assert r_str.model_dump(mode="json")["cost_usd"] == "1.23"


def test_status_enum_rejects_invalid():
    with pytest.raises(ValidationError):
        FrontierReceipt(**_minimum_receipt_kwargs(status="unknown"))


def test_status_accepts_all_four_values():
    for val in ("succeeded", "failed", "dry_run", "disabled"):
        r = FrontierReceipt(**_minimum_receipt_kwargs(status=val))
        assert r.status == val


def test_context_truncated_requires_trio():
    # context_truncated=True but missing original_tokens/truncated_tokens
    with pytest.raises(ValidationError):
        FrontierReceipt(**_minimum_receipt_kwargs(context_truncated=True))
    with pytest.raises(ValidationError):
        FrontierReceipt(
            **_minimum_receipt_kwargs(
                context_truncated=True, original_tokens=100,
            )
        )
    # valid trio
    r = FrontierReceipt(
        **_minimum_receipt_kwargs(
            context_truncated=True,
            original_tokens=1000,
            truncated_tokens=800,
        )
    )
    assert r.context_truncated is True
    assert r.original_tokens == 1000
    assert r.truncated_tokens == 800


def test_context_not_truncated_default_shape():
    r = FrontierReceipt(**_minimum_receipt_kwargs())
    assert r.context_truncated is False
    assert r.original_tokens is None
    assert r.truncated_tokens is None


def test_context_truncated_false_but_tokens_provided_rejected():
    # If context_truncated=False, the token pair must both be None.
    with pytest.raises(ValidationError):
        FrontierReceipt(
            **_minimum_receipt_kwargs(
                context_truncated=False,
                original_tokens=1000,
                truncated_tokens=800,
            )
        )


def test_gateway_reason_code_extension_set():
    required = {
        "GATEWAY_DISABLED",
        "DRY_RUN",
        "REJECT_INVALID_RECEIPT",
        "PROVIDER_AUTH_FAILED",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_CONTEXT_EXCEEDED",
        "PROVIDER_NETWORK_TIMEOUT",
    }
    present = {e.value for e in GatewayReasonCode}
    assert required.issubset(present), f"missing: {required - present}"


def test_reason_code_accepts_escalation_codes():
    r = FrontierReceipt(
        **_minimum_receipt_kwargs(
            status="failed",
            reason_code=ReasonCode.GUARDRAIL_BLOCKED,
        )
    )
    # Stored as enum value str (use_enum_values=True)
    assert r.reason_code == ReasonCode.GUARDRAIL_BLOCKED.value


def test_reason_code_accepts_gateway_codes():
    r = FrontierReceipt(
        **_minimum_receipt_kwargs(
            status="dry_run",
            reason_code=GatewayReasonCode.DRY_RUN,
        )
    )
    assert r.reason_code == GatewayReasonCode.DRY_RUN.value


def test_reason_code_accepts_none_on_success():
    r = FrontierReceipt(**_minimum_receipt_kwargs(reason_code=None))
    assert r.reason_code is None


def test_reason_code_rejects_foreign_string():
    with pytest.raises(ValidationError):
        FrontierReceipt(
            **_minimum_receipt_kwargs(
                status="failed",
                reason_code="NOT_A_REAL_CODE",
            )
        )


def test_chain_fields_default():
    r = FrontierReceipt(**_minimum_receipt_kwargs())
    assert r.parent_hash == GENESIS_PARENT_HASH
    assert r.receipt_hash == ""
    assert r.dry_run is False


def test_created_at_format():
    import re
    r = FrontierReceipt(**_minimum_receipt_kwargs())
    # Match "YYYY-MM-DDTHH:MM:SSZ"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", r.created_at)


def test_required_fields_missing_raises():
    with pytest.raises(ValidationError):
        FrontierReceipt()  # type: ignore[call-arg]


def test_model_fields_include_all_expected():
    expected = {
        "escalation_receipt_id",
        "admission_receipt_hash",
        "provider",
        "model_id",
        "provider_request_id",
        "prompt_tokens",
        "response_tokens",
        "cost_usd",
        "latency_ms",
        "status",
        "reason_code",
        "context_truncated",
        "original_tokens",
        "truncated_tokens",
        "schema_version",
        "created_at",
        "parent_hash",
        "receipt_hash",
        "dry_run",
    }
    assert expected.issubset(set(FrontierReceipt.model_fields.keys()))

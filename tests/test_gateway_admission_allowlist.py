"""test_gateway_admission_allowlist.py -- Phase 58-01 allowlist checks (GATE-06).

Covers three allowlist policy behaviours:

  1. Project IS in allowlist (with a valid vk) -> admission approved.
  2. Project NOT in allowlist -> reject with PROJECT_NOT_ALLOWED.
  3. Empty/missing allowlist -> every project rejects.

Tests call ``AdmissionPolicy.precheck`` directly; HTTP-layer integration is
covered in ``test_gateway_admission_http.py``.
"""
from __future__ import annotations

import pytest

from ollarma import gateway_admission
from ollarma.gateway import GatewayReasonCode
from ollarma.gateway_admission import AdmissionDecision, AdmissionPolicy


@pytest.fixture
def _fake_keychain(monkeypatch):
    """Inject a deterministic Keychain resolver that always returns bytes."""
    def _fake_lookup(service_name: str) -> bytes:
        return b"fake-secret-bytes"
    monkeypatch.setattr(gateway_admission, "_KEYCHAIN_LOOKUP", _fake_lookup)
    return _fake_lookup


def _minimal_config(
    *,
    allowlist: list[str] | None = None,
    virtual_keys: list[dict] | None = None,
) -> dict:
    cfg: dict = {"enabled": True}
    if allowlist is not None:
        cfg["allowlist"] = allowlist
    if virtual_keys is not None:
        cfg["virtual_keys"] = virtual_keys
    return cfg


class TestAllowlistAdmission:
    def test_project_in_allowlist_with_valid_vk_approves(
        self, _fake_keychain,
    ) -> None:
        cfg = _minimal_config(
            allowlist=["overwatch"],
            virtual_keys=[
                {
                    "id": "vk_overwatch",
                    "keychain_service": "ollarma-anthropic-overwatch",
                    "provider": "anthropic",
                },
            ],
        )
        policy = AdmissionPolicy(cfg)
        decision = policy.precheck("overwatch", "vk_overwatch")
        assert isinstance(decision, AdmissionDecision)
        assert decision.approved is True
        assert decision.reason_code is None
        assert decision.reason_detail == "admitted"

    def test_project_not_in_allowlist_rejects(self, _fake_keychain) -> None:
        cfg = _minimal_config(
            allowlist=["overwatch"],
            virtual_keys=[
                {
                    "id": "vk_x",
                    "keychain_service": "ollarma-anthropic",
                    "provider": "anthropic",
                },
            ],
        )
        policy = AdmissionPolicy(cfg)
        decision = policy.precheck("unknown-project", "vk_x")
        assert decision.approved is False
        assert decision.reason_code is GatewayReasonCode.PROJECT_NOT_ALLOWED
        assert "unknown-project" in decision.reason_detail
        assert "allowlist" in decision.reason_detail

    def test_empty_allowlist_rejects_every_project(self, _fake_keychain) -> None:
        cfg = _minimal_config(
            allowlist=[],
            virtual_keys=[
                {
                    "id": "vk_x",
                    "keychain_service": "ollarma-anthropic",
                    "provider": "anthropic",
                },
            ],
        )
        policy = AdmissionPolicy(cfg)
        for project in ["overwatch", "demo", "anything"]:
            decision = policy.precheck(project, "vk_x")
            assert decision.approved is False
            assert decision.reason_code is GatewayReasonCode.PROJECT_NOT_ALLOWED

    def test_missing_allowlist_key_defaults_to_closed(self, _fake_keychain) -> None:
        # No allowlist key at all -> policy treats as empty list (closed).
        cfg: dict = {"enabled": True, "virtual_keys": []}
        policy = AdmissionPolicy(cfg)
        decision = policy.precheck("overwatch", "vk_any")
        assert decision.approved is False
        assert decision.reason_code is GatewayReasonCode.PROJECT_NOT_ALLOWED

    def test_missing_gateway_config_entirely_returns_not_configured(self) -> None:
        # Empty config dict (no features.gateway block) -> GATEWAY_NOT_CONFIGURED
        # precedes the allowlist check.
        policy = AdmissionPolicy({})
        decision = policy.precheck("overwatch", "vk_any")
        assert decision.approved is False
        assert decision.reason_code is GatewayReasonCode.GATEWAY_NOT_CONFIGURED
        assert "no gateway config" in decision.reason_detail

"""test_gateway_admission_virtual_keys.py -- Phase 58-01 vk registry (GATE-03).

Covers:

  1. Known vk + Keychain hit -> admission approved.
  2. Unknown vk (not in registry) -> VIRTUAL_KEY_UNKNOWN.
  3. Known vk but Keychain returns None -> VIRTUAL_KEY_KEYCHAIN_MISS.
  4. ``virtual_key_id=None`` when gateway enabled -> VIRTUAL_KEY_UNKNOWN.
  5. Raw-key isolation: ``b"fake-secret-bytes"`` MUST NOT appear in any
     ``AdmissionDecision`` field, in pytest captured stdout/stderr, or in any
     repr of the policy / decision.
"""
from __future__ import annotations

import pytest

from ollarma import gateway_admission
from ollarma.gateway import GatewayReasonCode
from ollarma.gateway_admission import AdmissionPolicy


_FAKE_SECRET = b"fake-secret-bytes"


@pytest.fixture
def _keychain_hit(monkeypatch):
    def _lookup(service_name: str) -> bytes:
        return _FAKE_SECRET
    monkeypatch.setattr(gateway_admission, "_KEYCHAIN_LOOKUP", _lookup)
    return _lookup


@pytest.fixture
def _keychain_miss(monkeypatch):
    def _lookup(service_name: str) -> None:
        return None
    monkeypatch.setattr(gateway_admission, "_KEYCHAIN_LOOKUP", _lookup)
    return _lookup


def _configured_policy() -> AdmissionPolicy:
    return AdmissionPolicy(
        {
            "enabled": True,
            "allowlist": ["overwatch"],
            "virtual_keys": [
                {
                    "id": "vk_overwatch",
                    "keychain_service": "ollarma-anthropic-overwatch",
                    "provider": "anthropic",
                },
            ],
        }
    )


class TestVirtualKeyAdmission:
    def test_known_vk_keychain_hit_approves(self, _keychain_hit) -> None:
        policy = _configured_policy()
        decision = policy.precheck("overwatch", "vk_overwatch")
        assert decision.approved is True
        assert decision.reason_code is None

    def test_unknown_vk_rejects(self, _keychain_hit) -> None:
        policy = _configured_policy()
        decision = policy.precheck("overwatch", "vk_not_registered")
        assert decision.approved is False
        assert decision.reason_code is GatewayReasonCode.VIRTUAL_KEY_UNKNOWN
        assert "not in registry" in decision.reason_detail

    def test_missing_vk_id_rejects(self, _keychain_hit) -> None:
        policy = _configured_policy()
        decision = policy.precheck("overwatch", None)
        assert decision.approved is False
        assert decision.reason_code is GatewayReasonCode.VIRTUAL_KEY_UNKNOWN
        assert "missing" in decision.reason_detail

    def test_known_vk_keychain_miss_rejects(self, _keychain_miss) -> None:
        policy = _configured_policy()
        decision = policy.precheck("overwatch", "vk_overwatch")
        assert decision.approved is False
        assert decision.reason_code is GatewayReasonCode.VIRTUAL_KEY_KEYCHAIN_MISS
        assert "Keychain has no entry" in decision.reason_detail

    def test_raw_key_never_leaks_into_decision(
        self, _keychain_hit, capsys,
    ) -> None:
        """The mocked key bytes must not appear in AdmissionDecision fields,
        in captured stdout/stderr, or in reprs of policy/decision."""
        policy = _configured_policy()
        decision = policy.precheck("overwatch", "vk_overwatch")

        # decoded form of the sentinel for cross-check
        secret_str = _FAKE_SECRET.decode()

        # reason_detail is a str; must not contain the secret
        assert secret_str not in decision.reason_detail
        # repr forms (useful to catch future __repr__ drift)
        assert secret_str not in repr(decision)
        assert secret_str not in repr(policy)
        # dataclass fields are primitives only -- no bytes leaked
        for name in ("approved", "reason_code", "reason_detail", "retry_after_seconds"):
            value = getattr(decision, name)
            assert value is None or not isinstance(value, bytes)

        # stdout/stderr must be clean
        captured = capsys.readouterr()
        assert secret_str not in captured.out
        assert secret_str not in captured.err

"""test_gateway_admission_rate_caps.py -- Phase 58-02 rate caps (GATE-04).

Covers the per-project rate-cap enforcer + restart-safe state + HTTP 429 +
Retry-After header parity (D-58-05).

Monkeypatches used (deliberate seams):
  - ``time_source`` / ``wall_time_source`` / ``today_source`` on
    ``RateCapEnforcer`` — injects deterministic monotonic, wall-clock, and
    UTC-date clocks. Default impls use ``time.monotonic``, ``time.time``,
    and ``datetime.now(UTC).date().isoformat()`` respectively.
  - ``_KEYCHAIN_LOOKUP`` on ``gateway_admission`` — returns ``b"fake-secret"``
    so the admission pipeline clears the 58-01 vk resolution stage and reaches
    the 58-02 rate-cap stage.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from starlette.testclient import TestClient

from ollarma import gateway_admission
from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.gateway import GatewayReasonCode
from ollarma.gateway_admission import RateCapEnforcer


_FAKE_SECRET = b"fake-secret-bytes"


# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic monotonic clock for the req/min window seam."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _make_enforcer(
    tmp_path: pathlib.Path,
    clock: _Clock | None = None,
    *,
    req_per_min: int = 60,
    tokens_per_day: int = 100_000,
    today: str = "2026-04-19",
) -> RateCapEnforcer:
    state_path = tmp_path / ".ollarma" / "gateway" / "rate_state.json"
    config = {
        "rate_caps": {
            "default": {
                "req_per_min": req_per_min,
                "tokens_per_day": tokens_per_day,
            }
        }
    }
    if clock is None:
        clock = _Clock()
    return RateCapEnforcer(
        config,
        state_path,
        time_source=clock,
        wall_time_source=lambda: 1_700_000_000.0,
        today_source=lambda: today,
    )


def _valid_er_dict(project: str = "overwatch") -> dict:
    er = build_escalation_receipt(
        project=project,
        lane="local",
        task_class="code",
        reason_code=ReasonCode.SELECTION_MISSING,
        reason_detail="selection artifact missing for rate-cap test",
        next_action="frontier_or_human",
    )
    return json.loads(er.model_dump_json())


@pytest.fixture
def _enabled_repo_tight_caps(tmp_path, monkeypatch):
    """Isolated repo with gateway enabled + caps = (3 req/min, 100 tokens/day).

    Tight caps keep the HTTP 429 test deterministic without needing to
    fire hundreds of requests through the TestClient.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".planning").mkdir()
    (repo / ".planning" / "config.json").write_text(
        json.dumps(
            {
                "features": {
                    "gateway": {
                        "enabled": True,
                        "allowlist": ["overwatch"],
                        "virtual_keys": [
                            {
                                "id": "vk_overwatch",
                                "keychain_service": "ollarma-test-overwatch",
                                "provider": "anthropic",
                            }
                        ],
                        "rate_caps": {
                            "default": {
                                "req_per_min": 3,
                                "tokens_per_day": 100_000,
                            }
                        },
                    }
                }
            }
        )
    )
    monkeypatch.chdir(repo)

    def _lookup(_service: str) -> bytes:
        return _FAKE_SECRET

    monkeypatch.setattr(gateway_admission, "_KEYCHAIN_LOOKUP", _lookup)
    return repo


# ---------------------------------------------------------------------------
# Unit tests: RateCapEnforcer
# ---------------------------------------------------------------------------


def test_rate_cap_within_caps_accepted(tmp_path):
    """59 requests in a 60s window all accept; cap is 60 req/min (default)."""
    clock = _Clock()
    enforcer = _make_enforcer(tmp_path, clock, req_per_min=60)
    project = "overwatch"
    for i in range(59):
        decision = enforcer.check(project, estimated_tokens=0)
        assert decision.approved, f"req #{i + 1} unexpectedly rejected"
        enforcer.record(project, actual_tokens=0)
        clock.advance(0.5)  # spread over ~30s; stays in window
    # 60th request still within cap (we've recorded 59; check() sees
    # req_count=59 < 60 cap).
    final = enforcer.check(project, estimated_tokens=0)
    assert final.approved


def test_rate_cap_req_per_min_exceeds_returns_retry_after(tmp_path):
    """61st request in a window -> reject with Retry-After ~60s."""
    clock = _Clock()
    enforcer = _make_enforcer(tmp_path, clock, req_per_min=60)
    project = "overwatch"
    for _ in range(60):
        decision = enforcer.check(project, estimated_tokens=0)
        assert decision.approved
        enforcer.record(project, actual_tokens=0)
        # Zero advance -> all in the same window.
    # 61st should reject.
    reject = enforcer.check(project, estimated_tokens=0)
    assert not reject.approved
    assert reject.reason_code is GatewayReasonCode.RATE_CAP_REQ_PER_MIN_EXCEEDED
    assert reject.retry_after_seconds is not None
    assert 1 <= reject.retry_after_seconds <= 62  # ~60s window, off-by-one OK


def test_rate_cap_tokens_per_day_exceeds_after_record(tmp_path):
    """record(tokens=99999) then request with estimated_tokens=2 -> reject."""
    clock = _Clock()
    enforcer = _make_enforcer(
        tmp_path, clock, req_per_min=1000, tokens_per_day=100_000
    )
    project = "overwatch"
    # One admission bumps tokens to 99_999.
    assert enforcer.check(project, estimated_tokens=0).approved
    enforcer.record(project, actual_tokens=99_999)
    # Next request estimates 2 tokens -> 99_999 + 2 > 100_000 -> reject.
    reject = enforcer.check(project, estimated_tokens=2)
    assert not reject.approved
    assert reject.reason_code is GatewayReasonCode.RATE_CAP_TOKENS_PER_DAY_EXCEEDED
    assert reject.retry_after_seconds is not None
    # Retry-after for tokens/day should be positive seconds-until-UTC-midnight.
    assert reject.retry_after_seconds > 0


def test_rate_cap_state_survives_process_restart(tmp_path):
    """Enforcer A writes state; Enforcer B (fresh instance, same path) reads it."""
    clock_a = _Clock(start=5000.0)
    enforcer_a = _make_enforcer(tmp_path, clock_a, req_per_min=10)
    project = "overwatch"
    for _ in range(5):
        assert enforcer_a.check(project, estimated_tokens=0).approved
        enforcer_a.record(project, actual_tokens=100)
    state_path = tmp_path / ".ollarma" / "gateway" / "rate_state.json"
    assert state_path.exists()
    saved = json.loads(state_path.read_text())
    assert saved["schema_version"] == 1
    assert "overwatch" in saved["per_project"]
    assert saved["per_project"]["overwatch"]["req_count_in_window"] == 5
    assert saved["per_project"]["overwatch"]["tokens_today"] == 500

    # Fresh enforcer with a new monotonic origin but matching wall-clock
    # source -> should resume counters (window not stale).
    clock_b = _Clock(start=99999.0)

    def _matching_wall() -> float:
        return 1_700_000_000.0  # same wall-clock anchor as _make_enforcer

    enforcer_b = RateCapEnforcer(
        {"rate_caps": {"default": {"req_per_min": 10, "tokens_per_day": 100_000}}},
        state_path,
        time_source=clock_b,
        wall_time_source=_matching_wall,
        today_source=lambda: "2026-04-19",
    )
    # req_count preserved: 5 more requests accepted, 6th rejected.
    for _ in range(5):
        assert enforcer_b.check(project, estimated_tokens=0).approved
        enforcer_b.record(project, actual_tokens=100)
    reject = enforcer_b.check(project, estimated_tokens=0)
    assert not reject.approved
    assert reject.reason_code is GatewayReasonCode.RATE_CAP_REQ_PER_MIN_EXCEEDED
    # Tokens preserved across restart: 5*100 from A + 5*100 from B = 1000.
    state_after = json.loads(state_path.read_text())
    assert state_after["per_project"]["overwatch"]["tokens_today"] == 1000


def test_rate_cap_window_resets_after_60s(tmp_path):
    """Advance the monotonic clock past 60s -> req/min counter resets."""
    clock = _Clock()
    enforcer = _make_enforcer(tmp_path, clock, req_per_min=2)
    project = "overwatch"
    assert enforcer.check(project, estimated_tokens=0).approved
    enforcer.record(project, actual_tokens=0)
    assert enforcer.check(project, estimated_tokens=0).approved
    enforcer.record(project, actual_tokens=0)
    # 3rd request in same window -> reject.
    reject = enforcer.check(project, estimated_tokens=0)
    assert not reject.approved
    assert reject.reason_code is GatewayReasonCode.RATE_CAP_REQ_PER_MIN_EXCEEDED
    # Advance monotonic clock past the 60s window.
    clock.advance(61.0)
    # After window reset the next request accepts.
    fresh = enforcer.check(project, estimated_tokens=0)
    assert fresh.approved


# ---------------------------------------------------------------------------
# HTTP integration test: 429 + Retry-After
# ---------------------------------------------------------------------------


def test_rate_cap_http_429_with_retry_after_header(_enabled_repo_tight_caps):
    """Exceed the 3 req/min cap over HTTP; assert 429 + integer Retry-After."""
    from ollarma.http_api import app  # noqa: PLC0415

    body = {
        "escalation_receipt": _valid_er_dict(project="overwatch"),
        "virtual_key_id": "vk_overwatch",
    }

    with TestClient(app) as client:
        # First 3 requests: admission approved, but body omits 'prompt' so
        # Phase 59's dispatch path rejects with GATEWAY_INPUT_INVALID (HTTP
        # 400). The rate-cap counter is bumped AFTER admission accepts, so
        # these three 400s each consume one req-per-min slot.
        for _ in range(3):
            resp = client.post("/gateway/submit", json=body)
            assert resp.status_code == 400, (
                f"expected 400 GATEWAY_INPUT_INVALID pre-cap, got {resp.status_code}"
            )
        # 4th request exceeds the cap -> HTTP 429.
        resp = client.post("/gateway/submit", json=body)
        assert resp.status_code == 429
        payload = resp.json()
        assert payload["status"] == "failed"
        assert payload["reason_code"] == "RATE_CAP_REQ_PER_MIN_EXCEEDED"
        retry_after_hdr = resp.headers.get("Retry-After")
        assert retry_after_hdr is not None
        # RFC 7231: integer seconds.
        retry_after_int = int(retry_after_hdr)
        assert retry_after_int > 0
        assert retry_after_int <= 62  # ~60s window upper bound

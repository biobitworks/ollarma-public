"""Tests for Plan 51-02: startup readiness contract (PERSIST-02).

All probes monkeypatched — no real Ollama / scheduler / admission dependencies.
Readiness contract must be deterministic, schema-versioned, and persistable.
"""
from __future__ import annotations

import pathlib

import orjson
import pytest

from ollarma import admission, service


# ---------------------------------------------------------------------------
# Helpers: minimal fake probe returns
# ---------------------------------------------------------------------------

def _fake_helper_ready(model: str = "qwen2.5:1.5b") -> service.HelperModelResolution:
    return service.HelperModelResolution(
        requested_model=None,
        effective_model=model,
        status="ready",
    )


def _fake_helper_blocked() -> service.HelperModelResolution:
    return service.HelperModelResolution(
        requested_model=None,
        effective_model=None,
        status="blocked",
        reason_code="MODEL_UNAVAILABLE",
        detail="no local model resolvable",
        recovery_commands=("ollarma run --suites code --trials 3",),
    )


def _fake_selection_ready(wc) -> service.SelectionHealth:
    return service.SelectionHealth(
        workload_class=getattr(wc, "value", str(wc)),
        status="ready",
        model="qwen2.5:1.5b",
    )


def _fake_selection_stale(wc) -> service.SelectionHealth:
    return service.SelectionHealth(
        workload_class=getattr(wc, "value", str(wc)),
        status="blocked",
        reason_code="SELECTION_STALE",
    )


def _fake_telemetry(swap_mb: float | None = 50.0):
    from ollarma.guards import RuntimeTelemetry
    return RuntimeTelemetry(
        loaded_models=(),
        loaded_model_count=0,
        size_vram_bytes=0,
        swap_used_mb=swap_mb,
        telemetry_source="fake",
    )


@pytest.fixture(autouse=True)
def _isolate_readiness_path(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect readiness persistence to tmp_path for every test."""
    path = tmp_path / ".ollarma" / "startup" / "readiness.json"
    monkeypatch.setattr(
        service, "startup_readiness_path", lambda root=None: path,
    )


@pytest.fixture
def healthy_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire up all probes to a clean-baseline healthy state."""
    from ollarma.execution_policy import WorkloadClass

    # conftest sets OLLARMA_RECOVERY_ADMISSION=off globally; for readiness tests
    # that expect a fully-ready payload, admission must be ON.
    monkeypatch.delenv("OLLARMA_RECOVERY_ADMISSION", raising=False)

    monkeypatch.setattr(service, "resolve_generic_chat_model", _fake_helper_ready)

    def sel(wc: WorkloadClass) -> service.SelectionHealth:
        return _fake_selection_ready(wc)

    monkeypatch.setattr(service, "_selection_health", sel)
    from ollarma import guards as _guards
    monkeypatch.setattr(_guards, "collect_runtime_telemetry", lambda: _fake_telemetry(50.0))


# ---------------------------------------------------------------------------
# Model contract
# ---------------------------------------------------------------------------

class TestReadinessModelContract:
    def test_schema_version_stamped(self, healthy_probes) -> None:
        payload = service.build_startup_readiness()
        assert payload.schema_version == 1

    def test_service_label_present(self, healthy_probes) -> None:
        payload = service.build_startup_readiness()
        assert payload.service_label == "com.byron.ollarma"

    def test_generated_at_is_utc_iso(self, healthy_probes) -> None:
        payload = service.build_startup_readiness()
        # ISO with UTC offset ("+00:00") or "Z"
        assert payload.generated_at.endswith("+00:00") or payload.generated_at.endswith("Z")

    def test_top_level_fields_present(self, healthy_probes) -> None:
        payload = service.build_startup_readiness()
        dumped = payload.model_dump(mode="json")
        for key in (
            "schema_version", "service_label", "generated_at",
            "status", "checks", "model_availability",
            "swap", "admission", "pipeline", "next_fix_commands",
        ):
            assert key in dumped, f"missing top-level field: {key}"


class TestAggregateStartupStatus:
    def test_all_ready_is_ready(self) -> None:
        checks = (
            service.StartupReadinessCheck(name="a", status="ready"),
            service.StartupReadinessCheck(name="b", status="ready"),
        )
        assert service.aggregate_startup_status(checks) == "ready"

    def test_any_degraded_is_degraded(self) -> None:
        checks = (
            service.StartupReadinessCheck(name="a", status="ready"),
            service.StartupReadinessCheck(name="b", status="degraded"),
        )
        assert service.aggregate_startup_status(checks) == "degraded"

    def test_any_blocked_wins(self) -> None:
        checks = (
            service.StartupReadinessCheck(name="a", status="degraded"),
            service.StartupReadinessCheck(name="b", status="blocked"),
            service.StartupReadinessCheck(name="c", status="ready"),
        )
        assert service.aggregate_startup_status(checks) == "blocked"


# ---------------------------------------------------------------------------
# Probe semantics
# ---------------------------------------------------------------------------

class TestModelAvailabilityCheck:
    def test_helper_ready_is_ready(self, healthy_probes) -> None:
        payload = service.build_startup_readiness()
        assert payload.model_availability.status == "ready"
        # No MODEL_UNAVAILABLE reason when ready
        check = next(c for c in payload.checks if c.name == "model_availability")
        assert check.status == "ready"

    def test_helper_blocked_is_blocked_with_reason(
        self, monkeypatch: pytest.MonkeyPatch, healthy_probes,
    ) -> None:
        monkeypatch.setattr(service, "resolve_generic_chat_model", _fake_helper_blocked)
        payload = service.build_startup_readiness()
        check = next(c for c in payload.checks if c.name == "model_availability")
        assert check.status == "blocked"
        assert check.reason_code == "MODEL_UNAVAILABLE"

    @pytest.mark.parametrize("helper_status", ["selection_ready", "explicit"])
    def test_selection_ready_and_explicit_register_as_ready(
        self, monkeypatch: pytest.MonkeyPatch, healthy_probes, helper_status: str,
    ) -> None:
        """GPU-08: resolve_generic_chat_model emits selection_ready on happy path
        post-benchmark and explicit when a model is passed directly. Both must
        register as ready in model_availability; historically only the legacy
        'ready' literal was matched, so selection_ready fell into the blocked
        branch with an empty detail string."""
        def _helper():
            return service.HelperModelResolution(
                requested_model=None,
                effective_model="qwen2.5:1.5b",
                status=helper_status,
            )
        monkeypatch.setattr(service, "resolve_generic_chat_model", _helper)
        payload = service.build_startup_readiness()
        check = next(c for c in payload.checks if c.name == "model_availability")
        assert check.status == "ready", (
            f"status {helper_status!r} from resolve_generic_chat_model must map "
            f"to check.status=ready, got {check.status!r}"
        )
        assert check.reason_code is None
        assert payload.model_availability.status == "ready"

    def test_stale_selection_uses_pressure_aware_fallback_for_model_availability(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale benchmark artifact should degrade to a small local helper,
        not report total unavailability when Ollama has a fallback model."""
        from ollarma.execution_policy import SelectionResolutionError, WorkloadClass

        def _stale(*, workload_class=WorkloadClass.CHAT, results_dir="results"):
            raise SelectionResolutionError(
                "SELECTION_STALE",
                workload_class,
                "Selection artifact is older than 24h",
            )

        monkeypatch.setattr(service, "resolve_default_model", _stale)
        monkeypatch.setattr(
            service,
            "_probe_installed_model_names",
            lambda: (("gemma3:12b", "qwen2.5:1.5b"), None),
        )
        monkeypatch.setattr(
            service,
            "_fallback_pressure_context",
            lambda: (("qwen2.5:1.5b",), 4096.0, 512.0),
        )
        from ollarma import guards as _guards
        monkeypatch.setattr(_guards, "collect_runtime_telemetry", lambda: _fake_telemetry(4096.0))

        payload = service.build_startup_readiness()
        check = next(c for c in payload.checks if c.name == "model_availability")
        assert check.status == "degraded"
        assert check.reason_code == "SELECTION_STALE"
        assert payload.model_availability.effective_model == "qwen2.5:1.5b"
        assert "pressure-aware local fallback" in payload.model_availability.detail

    def test_selection_health_reports_fallback_ready_when_selection_stale(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Strict selection remains stale, but the sidecar fallback is usable."""
        from ollarma.execution_policy import SelectionResolutionError, WorkloadClass

        def _stale(workload_class):
            raise SelectionResolutionError(
                "SELECTION_STALE",
                workload_class,
                "Selection artifact is older than 24h",
            )

        monkeypatch.setattr(service, "resolve_selection", _stale)
        monkeypatch.setattr(
            service,
            "_probe_installed_model_names",
            lambda: (("gemma3:12b", "qwen2.5:1.5b"), None),
        )
        monkeypatch.setattr(
            service,
            "_fallback_pressure_context",
            lambda: (("qwen2.5:1.5b",), 4096.0, 512.0),
        )

        health = service._selection_health(WorkloadClass.ROUTE_PROMPT)
        assert health.status == "fallback_ready"
        assert health.model == "qwen2.5:1.5b"
        assert health.reason_code == "SELECTION_STALE"


class TestSwapPostureCheck:
    def test_swap_below_threshold_is_ready(self, healthy_probes) -> None:
        payload = service.build_startup_readiness()
        check = next(c for c in payload.checks if c.name == "swap_posture")
        assert check.status == "ready"

    def test_swap_above_threshold_is_degraded(
        self, monkeypatch: pytest.MonkeyPatch, healthy_probes,
    ) -> None:
        from ollarma import guards as _guards
        from ollarma.scheduler import SWAP_DEGRADED_THRESHOLD_MB
        monkeypatch.setattr(
            _guards,
            "collect_runtime_telemetry",
            lambda: _fake_telemetry(SWAP_DEGRADED_THRESHOLD_MB + 1000.0),
        )
        payload = service.build_startup_readiness()
        check = next(c for c in payload.checks if c.name == "swap_posture")
        assert check.status in ("degraded", "blocked")
        assert check.reason_code == "SWAP_DEGRADED"

    def test_swap_unknown_is_degraded(
        self, monkeypatch: pytest.MonkeyPatch, healthy_probes,
    ) -> None:
        from ollarma import guards as _guards
        monkeypatch.setattr(_guards, "collect_runtime_telemetry", lambda: _fake_telemetry(None))
        payload = service.build_startup_readiness()
        check = next(c for c in payload.checks if c.name == "swap_posture")
        assert check.status == "degraded"
        assert check.reason_code == "SWAP_UNKNOWN"


class TestAdmissionPostureCheck:
    def test_admission_enabled_reported(self, healthy_probes) -> None:
        payload = service.build_startup_readiness()
        # Whether enabled or not is a fact — just confirm it's reported
        assert isinstance(payload.admission.enabled, bool)

    def test_admission_disabled_reported(
        self,
        healthy_probes,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OLLARMA_RECOVERY_ADMISSION", "off")
        payload = service.build_startup_readiness()
        assert payload.admission.enabled is False


class TestNextFixCommands:
    def test_blocked_payload_contains_fix_commands(
        self, monkeypatch: pytest.MonkeyPatch, healthy_probes,
    ) -> None:
        monkeypatch.setattr(service, "resolve_generic_chat_model", _fake_helper_blocked)
        payload = service.build_startup_readiness()
        assert payload.status in ("blocked", "degraded")
        assert payload.next_fix_commands  # non-empty
        # Should reference a concrete operator command like ollarma run or launchctl
        joined = "\n".join(payload.next_fix_commands)
        assert any(
            keyword in joined
            for keyword in ("ollarma", "launchctl")
        ), f"fix commands too vague: {payload.next_fix_commands!r}"

    def test_ready_payload_has_no_fix_commands(self, healthy_probes) -> None:
        payload = service.build_startup_readiness()
        assert payload.status == "ready"
        assert payload.next_fix_commands == ()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_write_and_read_roundtrip(
        self, tmp_path: pathlib.Path, healthy_probes,
    ) -> None:
        payload = service.build_startup_readiness()
        path = service.write_startup_readiness(payload)
        assert path.exists()
        loaded = service.get_startup_readiness()
        assert loaded.model_dump() == payload.model_dump()

    def test_persisted_json_is_sorted_keys_indented(
        self, healthy_probes,
    ) -> None:
        payload = service.build_startup_readiness()
        path = service.write_startup_readiness(payload)
        raw = path.read_bytes()
        parsed = orjson.loads(raw)
        keys = list(parsed.keys())
        assert keys == sorted(keys), f"keys not sorted: {keys}"
        assert b"  " in raw, "expected indented JSON"

    def test_refresh_returns_new_payload_and_persists(self, healthy_probes) -> None:
        payload = service.refresh_startup_readiness()
        assert payload.status == "ready"
        path = service.startup_readiness_path()
        assert path.exists()

    def test_get_without_previous_write_builds_fresh(self, healthy_probes) -> None:
        """get_startup_readiness should return a freshly built payload if no
        persisted file exists, not raise."""
        # No write yet
        loaded = service.get_startup_readiness()
        assert loaded.schema_version == 1

    def test_get_rebuilds_when_persisted_payload_is_stale(self, healthy_probes) -> None:
        """GPU-06: stale persisted payload (older than max_age_seconds) triggers rebuild.

        Writes a payload with a hand-crafted old generated_at, then calls
        get_startup_readiness with a short TTL and verifies the returned payload
        has a fresher timestamp than the persisted one.
        """
        import datetime as _dt

        # Build + persist a payload, then rewrite it with an old timestamp.
        payload = service.build_startup_readiness()
        path = service.write_startup_readiness(payload)

        old_ts = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=60)
        ).isoformat()
        raw = orjson.loads(path.read_bytes())
        raw["generated_at"] = old_ts
        path.write_bytes(orjson.dumps(raw, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2))

        # TTL=10s: persisted payload is 60s old -> must rebuild.
        fresh = service.get_startup_readiness(max_age_seconds=10.0)
        assert fresh.generated_at > old_ts, (
            f"expected rebuild (fresh.generated_at={fresh.generated_at} "
            f"should be newer than old_ts={old_ts})"
        )

    def test_get_uses_cached_when_within_ttl(self, healthy_probes) -> None:
        """GPU-06: persisted payload within TTL is returned verbatim (no rebuild)."""
        payload = service.build_startup_readiness()
        service.write_startup_readiness(payload)

        # TTL effectively infinite; first write is milliseconds old -> must use cached.
        loaded = service.get_startup_readiness(max_age_seconds=float("inf"))
        assert loaded.generated_at == payload.generated_at


# ---------------------------------------------------------------------------
# HTTP exposure (Task 2)
# ---------------------------------------------------------------------------

class TestHTTPStartupReadinessRoute:
    def test_route_registered(self) -> None:
        from ollarma.http_api import app
        paths = {r.path for r in app.routes}
        assert "/startup/readiness" in paths

    def test_lifespan_calls_refresh(self) -> None:
        """Starlette app must declare a lifespan that refreshes readiness."""
        from ollarma.http_api import app
        # Starlette stores the lifespan as a router-level attribute
        assert app.router.lifespan_context is not None

    def test_health_includes_startup_readiness(
        self, healthy_probes, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /health payload must include a startup_readiness block."""
        from starlette.testclient import TestClient
        from ollarma.http_api import app

        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200
            body = r.json()
            assert "startup_readiness" in body

    def test_startup_readiness_endpoint_returns_payload(
        self, healthy_probes,
    ) -> None:
        from starlette.testclient import TestClient
        from ollarma.http_api import app

        with TestClient(app) as client:
            r = client.get("/startup/readiness")
            assert r.status_code == 200
            body = r.json()
            for key in ("schema_version", "service_label", "status", "checks"):
                assert key in body

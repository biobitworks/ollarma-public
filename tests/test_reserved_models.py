"""Reserved Antigence/Sentinel model policy tests."""

from __future__ import annotations

import datetime as dt

import pytest


def test_reserved_model_is_not_generic_chat_fallback(monkeypatch):
    """qwen3:1.7b is Antigence/Sentinel-only, not an Ollarma sidecar fallback."""
    from ollarma.execution_policy import SelectionResolutionError, WorkloadClass
    from ollarma.service import chat_with_model
    import ollarma.service as service_mod

    def _raise_selection(*args, **kwargs):
        raise SelectionResolutionError(
            "SELECTION_MISSING",
            WorkloadClass.CHAT,
            "No validated selection artifact found in results/",
        )

    class _FakeClient:
        def list(self):
            return type(
                "FakeList",
                (),
                {"models": [type("FakeModel", (), {"model": "qwen3:1.7b"})()]},
            )()

    monkeypatch.setattr(service_mod, "resolve_default_model", _raise_selection)
    monkeypatch.setattr(service_mod.ollama, "Client", lambda: _FakeClient())

    result = chat_with_model("hi")

    assert result.status == "blocked"
    assert result.model == "unavailable"
    assert result.reason_code == "SELECTION_MISSING"


def test_explicit_reserved_model_chat_refused():
    from ollarma.service import RESERVED_MODEL_REASON_CODE, chat_with_model

    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        chat_with_model("hi", model="qwen3:1.7b")


def test_reserved_model_benchmark_filter_refused(tmp_path):
    from ollarma.service import RESERVED_MODEL_REASON_CODE, run_benchmark

    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        run_benchmark(
            models_filter=["qwen3:1.7b"],
            skip_preflight=True,
            results_dir=tmp_path / "results",
        )


def test_reserved_model_pipeline_controls_refused():
    from ollarma.service import (
        RESERVED_MODEL_REASON_CODE,
        pipeline_drain_swap,
        pipeline_evict,
        pipeline_pin,
        pipeline_warmup,
    )

    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        pipeline_warmup("qwen3:1.7b")
    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        pipeline_pin("qwen3:1.7b")
    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        pipeline_evict("qwen3:1.7b")
    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        pipeline_drain_swap("qwen3:1.7b", "qwen2.5:1.5b")


def test_reserved_model_agent_loops_refused_before_ollama(monkeypatch, tmp_path):
    from ollarma.agent import agent_loop, fleet_agent_loop
    from ollarma.fleet import AdapterConfig
    from ollarma.reserved_models import RESERVED_MODEL_REASON_CODE
    import ollarma.agent as agent_mod

    def _client_should_not_be_constructed():
        raise AssertionError("reserved model guard should run before Ollama client construction")

    monkeypatch.setattr(agent_mod.ollama, "Client", _client_should_not_be_constructed)

    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        agent_loop("hi", "qwen3:1.7b", str(tmp_path))

    adapter = AdapterConfig(project_name="demo", project_root=str(tmp_path))
    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        fleet_agent_loop("hi", "qwen3:1.7b", adapter)


def test_reserved_selection_artifact_winner_refused(monkeypatch):
    import ollarma.execution_policy as policy
    from ollarma.execution_policy import SelectionResolutionError, WorkloadClass, resolve_selection
    from ollarma.reserved_models import RESERVED_MODEL_REASON_CODE

    monkeypatch.setattr(policy, "_latest_artifact_path", lambda _results_path: "dummy")
    monkeypatch.setattr(
        policy,
        "_load_artifact",
        lambda _path: {
            "run_id": "2026-05-31T00:00:00Z",
            "per_suite_winners": {"code": "qwen3:1.7b"},
            "pareto_frontier": ["qwen3:1.7b", "qwen2.5:1.5b"],
        },
    )

    with pytest.raises(SelectionResolutionError) as exc:
        resolve_selection(
            WorkloadClass.CHAT,
            now=dt.datetime(2026, 5, 31, 0, 0, tzinfo=dt.timezone.utc),
        )

    assert exc.value.reason_code == RESERVED_MODEL_REASON_CODE


def test_reserved_ranked_selection_artifact_pareto_entries_dropped(monkeypatch):
    import ollarma.execution_policy as policy
    from ollarma.execution_policy import WorkloadClass, resolve_ranked_selection

    monkeypatch.setattr(policy, "_latest_artifact_path", lambda _results_path: "dummy")
    monkeypatch.setattr(
        policy,
        "_load_artifact",
        lambda _path: {
            "run_id": "2026-05-31T00:00:00Z",
            "per_suite_winners": {"code": "qwen2.5:1.5b"},
            "pareto_frontier": ["qwen3:1.7b", "qwen2.5:1.5b", "qwen3.5:4b"],
        },
    )

    winner, pareto = resolve_ranked_selection(
        WorkloadClass.CHAT,
        now=dt.datetime(2026, 5, 31, 0, 0, tzinfo=dt.timezone.utc),
    )

    assert winner == "qwen2.5:1.5b"
    assert pareto == ("qwen2.5:1.5b", "qwen3.5:4b")


# --- routing-ladder + leaf-helper coverage (closes the ladder override/candidate leaks) ---


def test_reserved_models_leaf_helpers():
    from ollarma.reserved_models import (
        RESERVED_MODEL_REASON_CODE,
        assert_model_not_reserved,
        drop_reserved_models,
        is_reserved_model,
    )

    assert is_reserved_model("qwen3:1.7b") is True
    assert is_reserved_model(" qwen3:1.7b ") is True  # whitespace tolerant
    assert is_reserved_model("qwen2.5:1.5b") is False  # lean bridge, NOT reserved
    assert is_reserved_model(None) is False
    assert drop_reserved_models(("qwen3:1.7b", "qwen2.5:1.5b")) == ("qwen2.5:1.5b",)
    with pytest.raises(ValueError, match=RESERVED_MODEL_REASON_CODE):
        assert_model_not_reserved("qwen3:1.7b", context="test")


def test_ladder_override_refuses_reserved():
    """A generic/project caller cannot force the reserved model via ladder override."""
    from ollarma.routing_ladder import build_ladder

    with pytest.raises(ValueError, match="RESERVED_MODEL_ANTIGENCE_SENTINEL"):
        build_ladder(
            "route_prompt",
            swap_used_mb=0.0,
            swap_threshold_mb=4096.0,
            rescue_model="qwen2.5:1.5b",
            override_model="qwen3:1.7b",
        )


def test_ladder_drops_reserved_from_candidates():
    """Reserved tags in selection_result/ranked_alternates never become ladder rungs."""
    from ollarma.routing_ladder import build_ladder

    d = build_ladder(
        "route_prompt",
        selection_result="qwen3:1.7b",
        ranked_alternates=("qwen3:1.7b", "qwen2.5-coder:7b"),
        swap_used_mb=0.0,
        swap_threshold_mb=4096.0,
        rescue_model="qwen2.5:1.5b",
    )
    rung_models = {r.model for r in d.rungs_considered}
    assert "qwen3:1.7b" not in rung_models
    assert d.chosen_model != "qwen3:1.7b"
    assert d.chosen_model in {"qwen2.5-coder:7b", "qwen2.5:1.5b"}


def test_ladder_drops_reserved_rescue_model():
    """Even a misconfigured reserved rescue_model is filtered out of the ladder."""
    from ollarma.routing_ladder import build_ladder

    d = build_ladder(
        "route_prompt",
        selection_result="qwen2.5-coder:7b",
        swap_used_mb=0.0,
        swap_threshold_mb=4096.0,
        rescue_model="qwen3:1.7b",
    )
    assert all(r.model != "qwen3:1.7b" for r in d.rungs_considered)
    assert d.chosen_model != "qwen3:1.7b"

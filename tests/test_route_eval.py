"""Offline deterministic evaluation pack for retrieval-first routing."""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import orjson

from ollarma.fleet import AdapterConfig


def _build_repo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, AdapterConfig]:
    repo_root = tmp_path / "route-eval-project"
    (repo_root / "docs").mkdir(parents=True)
    (repo_root / "docs" / "workflow.md").write_text(
        "workflow manifest guidance and execution notes\n",
        encoding="utf-8",
    )
    adapter = AdapterConfig(
        project_name="eval-project",
        project_root=str(repo_root),
        adapter_source="yaml",
        knowledge_base={
            "sources": [
                {"path": "docs", "kind": "documents", "authority": "canonical"},
            ],
        },
    )
    return repo_root, adapter


def _stale_kb(repo_root: pathlib.Path) -> None:
    receipts_path = repo_root / ".ollarma" / "kb" / "receipts.jsonl"
    receipts_path.write_bytes(
        orjson.dumps(
            {
                "built_at": "2026-01-01T00:00:00Z",
                "project": "eval-project",
                "artifact_root": ".ollarma/kb",
                "reason": "test",
                "source_count": 1,
                "document_count": 1,
                "chunk_count": 1,
                "manifest_hash": "sha256:test",
                "source_paths": ["docs"],
            }
        )
        + b"\n"
    )


class TestRouteEvalPack:
    """Exact-answer, grounded-summary, stale-index, and escalation eval cases."""

    def test_exact_answer_case(self, tmp_path: pathlib.Path) -> None:
        """exact-answer: file lookup returns kb_direct with evidence refs."""
        from ollarma.service import build_project_kb, route_prompt

        repo_root, adapter = _build_repo(tmp_path)

        with patch("ollarma.service._load_project_registry", return_value={"eval-project": adapter}):
            build_project_kb("eval-project")
            result = route_prompt("where is the workflow manifest", "eval-project")

        assert result.lane == "kb_direct"
        assert result.reason_code == "KB_DIRECT_ANSWER"
        assert result.evidence_refs

    def test_grounded_summary_case(self, tmp_path: pathlib.Path) -> None:
        """grounded-summary: summary query uses bounded local synthesis."""
        from ollarma.service import build_project_kb, route_prompt
        from ollarma.guards import RuntimeTelemetry

        repo_root, adapter = _build_repo(tmp_path)
        mock_response = MagicMock()
        mock_response.message.content = "Summary grounded in docs/workflow.md"
        # Simulate low-swap environment so the ladder picks the benchmark winner.
        safe_telemetry = RuntimeTelemetry(
            loaded_models=("qwen3:4b",),
            loaded_model_count=1,
            swap_used_mb=50.0,
            telemetry_source="fake",
        )

        # Phase 53: patch resolve_ranked_selection AND collect_runtime_telemetry so
        # the routing ladder chooses qwen3:4b (benchmark winner, low swap, resident).
        with patch("ollarma.service._load_project_registry", return_value={"eval-project": adapter}), \
             patch("ollarma.service.resolve_ranked_selection", return_value=("qwen3:4b", ("qwen3:4b",))), \
             patch("ollarma.service.resolve_default_model", return_value="qwen3:4b"), \
             patch("ollarma.guards.collect_runtime_telemetry", return_value=safe_telemetry), \
             patch("ollarma.service.ollama.Client") as mock_client_cls:
            mock_client_cls.return_value.chat.return_value = mock_response
            build_project_kb("eval-project")
            result = route_prompt("summarize the workflow guidance", "eval-project")

        assert result.lane == "grounded_local_synthesis"
        assert result.reason_code == "GROUNDED_LOCAL_SYNTHESIS"
        assert result.model == "qwen3:4b"

    def test_stale_index_case(self, tmp_path: pathlib.Path) -> None:
        """stale-index: stale KB evidence escalates instead of answering."""
        from ollarma.service import build_project_kb, route_prompt

        repo_root, adapter = _build_repo(tmp_path)

        with patch("ollarma.service._load_project_registry", return_value={"eval-project": adapter}):
            build_project_kb("eval-project")
            _stale_kb(repo_root)
            result = route_prompt("summarize the workflow guidance", "eval-project")

        assert result.lane == "frontier_or_human"
        assert result.reason_code == "KB_STALE"
        assert result.escalation_receipt is not None

    def test_escalation_needed_case(self, tmp_path: pathlib.Path) -> None:
        """escalation-needed: unrelated query returns insufficient-grounding handoff."""
        from ollarma.service import build_project_kb, route_prompt

        repo_root, adapter = _build_repo(tmp_path)

        with patch("ollarma.service._load_project_registry", return_value={"eval-project": adapter}):
            build_project_kb("eval-project")
            result = route_prompt("summarize the nonexistent assay inventory", "eval-project")

        assert result.lane == "frontier_or_human"
        assert result.reason_code == "INSUFFICIENT_GROUNDED_EVIDENCE"
        assert result.route_receipt is not None

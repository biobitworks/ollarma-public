"""Tests for read-only KB status and SQLite-backed search surfaces."""
from __future__ import annotations

import pathlib

import orjson

from ollarma.kb import build_kb_artifacts
from ollarma.kb_contract import (
    KnowledgeBaseConfig,
    KnowledgeBaseSource,
    build_project_knowledge_contract,
)
from ollarma.kb_search import load_kb_status, search_kb


def _write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_contract(
    project_root: pathlib.Path,
    *,
    stale_behavior: str = "escalate",
):
    return build_project_knowledge_contract(
        project_name="kb-demo",
        project_root=str(project_root),
        knowledge_base=KnowledgeBaseConfig(
            artifact_root=".ollarma/kb",
            freshness_hours=24,
            stale_behavior=stale_behavior,
            sources=(
                KnowledgeBaseSource(path="docs", kind="documents"),
            ),
        ),
    )


class TestKbStatusAndSearch:
    """Read-only KB status/search contract tests."""

    def test_status_blocked_when_kb_not_built(self, tmp_path: pathlib.Path) -> None:
        """Status stays blocked until the KB artifact set exists."""
        project_root = tmp_path / "kb-project"
        _write_text(project_root / "docs" / "guide.md", "manifest backed workflow\n")

        contract = _make_contract(project_root)
        status = load_kb_status(contract)

        assert status.status == "blocked"
        assert status.reason_code == "KB_NOT_BUILT"
        assert status.search_db_path == ".ollarma/kb/search.sqlite"

    def test_search_returns_deterministic_hits_after_build(self, tmp_path: pathlib.Path) -> None:
        """Search returns ordered deterministic hits from the SQLite read model."""
        project_root = tmp_path / "kb-project"
        _write_text(project_root / "docs" / "b-guide.md", "workflow manifest guidance\n")
        _write_text(project_root / "docs" / "a-guide.md", "workflow manifest contract\n")

        contract = _make_contract(project_root)
        build_kb_artifacts(contract, reason="test")
        rebuilt = _make_contract(project_root)

        status = load_kb_status(rebuilt)
        result = search_kb(rebuilt, "workflow", limit=5)

        assert status.status == "ready"
        assert status.document_count == 2
        assert status.chunk_count == 2
        assert result.status == "ready"
        assert result.hit_count == 2
        assert [hit.path for hit in result.hits] == [
            "docs/a-guide.md",
            "docs/b-guide.md",
        ]

    def test_search_escalate_mode_returns_stale_hits(self, tmp_path: pathlib.Path) -> None:
        """Stale KBs in escalate mode still return hits, but status is stale."""
        project_root = tmp_path / "kb-project"
        _write_text(project_root / "docs" / "guide.md", "adapter schema and manifest guidance\n")

        contract = _make_contract(project_root, stale_behavior="escalate")
        build_kb_artifacts(contract, reason="test")

        receipts_path = project_root / ".ollarma" / "kb" / "receipts.jsonl"
        stale_receipt = {
            "built_at": "2026-01-01T00:00:00Z",
            "project": "kb-demo",
            "artifact_root": ".ollarma/kb",
            "reason": "test",
            "source_count": 1,
            "document_count": 1,
            "chunk_count": 1,
            "manifest_hash": "sha256:test",
            "source_paths": ["docs"],
        }
        receipts_path.write_bytes(orjson.dumps(stale_receipt) + b"\n")

        rebuilt = _make_contract(project_root, stale_behavior="escalate")
        status = load_kb_status(rebuilt)
        result = search_kb(rebuilt, "adapter", limit=5)

        assert status.status == "stale"
        assert status.reason_code == "KB_STALE"
        assert result.status == "stale"
        assert result.reason_code == "KB_STALE"
        assert result.hit_count == 1

    def test_search_block_mode_blocks_stale_kb(self, tmp_path: pathlib.Path) -> None:
        """Stale KBs in block mode fail closed."""
        project_root = tmp_path / "kb-project"
        _write_text(project_root / "docs" / "guide.md", "adapter schema and manifest guidance\n")

        contract = _make_contract(project_root, stale_behavior="block")
        build_kb_artifacts(contract, reason="test")

        receipts_path = project_root / ".ollarma" / "kb" / "receipts.jsonl"
        stale_receipt = {
            "built_at": "2026-01-01T00:00:00Z",
            "project": "kb-demo",
            "artifact_root": ".ollarma/kb",
            "reason": "test",
            "source_count": 1,
            "document_count": 1,
            "chunk_count": 1,
            "manifest_hash": "sha256:test",
            "source_paths": ["docs"],
        }
        receipts_path.write_bytes(orjson.dumps(stale_receipt) + b"\n")

        rebuilt = _make_contract(project_root, stale_behavior="block")
        result = search_kb(rebuilt, "adapter", limit=5)

        assert result.status == "blocked"
        assert result.reason_code == "KB_STALE"
        assert result.hit_count == 0

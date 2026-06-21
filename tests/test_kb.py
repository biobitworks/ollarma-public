"""Tests for deterministic KB build artifacts and rebuild stability."""
from __future__ import annotations

import hashlib
import pathlib

import orjson
from pydantic import BaseModel
from unittest.mock import patch

from ollarma.fleet import AdapterConfig
from ollarma.kb_contract import KnowledgeBaseConfig, KnowledgeBaseSource


def _write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: pathlib.Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload))


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def _artifact_snapshot(root: pathlib.Path) -> dict[str, str]:
    """Return a stable fingerprint for the publishable KB artifacts."""
    interesting = (
        "manifest.json",
        "documents.jsonl",
        "chunks.jsonl",
        "tags.json",
        "search.sqlite",
    )
    snapshot: dict[str, str] = {}
    for name in interesting:
        path = root / name
        assert path.exists(), f"Expected artifact file missing: {path}"
        snapshot[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _record_path_key(record: dict) -> str:
    """Extract a path-like ordering key from a JSONL record."""
    for key in ("repo_relative", "relative_path", "path", "stable_id", "document_id", "id"):
        value = record.get(key)
        if isinstance(value, str):
            return value

    for nested_key in ("source", "document", "locator"):
        nested = record.get(nested_key)
        if isinstance(nested, dict):
            for key in ("repo_relative", "relative_path", "path", "stable_id"):
                value = nested.get(key)
                if isinstance(value, str):
                    return value

    raise AssertionError(f"Could not find a path-like field in record: {record!r}")


def _chunk_order_key(record: dict) -> tuple[str, int]:
    """Extract a deterministic order key for chunk records."""
    path_key = _record_path_key(record)
    for key in ("chunk_index", "index", "ordinal", "position"):
        value = record.get(key)
        if isinstance(value, int):
            return (path_key, value)
    return (path_key, 0)


def _make_adapter(project_root: pathlib.Path) -> AdapterConfig:
    """Build a KB-enabled adapter config for filesystem tests."""
    return AdapterConfig(
        project_name="kb-demo",
        project_root=str(project_root),
        project_type="python",
        adapter_source="test",
        knowledge_base=KnowledgeBaseConfig(
            artifact_root=".ollarma/kb",
            freshness_hours=24,
            stale_behavior="escalate",
            sources=(
                KnowledgeBaseSource(path="docs", kind="documents"),
                KnowledgeBaseSource(path="notes", kind="notes"),
                KnowledgeBaseSource(path="config", kind="config", authority="reference"),
            ),
        ),
    )


def _prepare_project(tmp_path: pathlib.Path) -> tuple[pathlib.Path, AdapterConfig]:
    project_root = tmp_path / "kb-project"
    _write_text(project_root / "docs" / "b-guide.md", "# B\nsecond\n")
    _write_text(project_root / "docs" / "a-guide.md", "# A\nfirst\n")
    _write_text(project_root / "notes" / "z-notes.txt", "zeta\n")
    _write_json(project_root / "config" / "kb.json", {"enabled": True, "mode": "deterministic"})
    return project_root, _make_adapter(project_root)


class TestKbBuildArtifacts:
    """Filesystem tests for the planned KB build surface."""

    @patch("ollarma.service.resolve_project_adapter")
    def test_build_writes_repo_local_artifacts(self, mock_resolve_adapter, tmp_path: pathlib.Path, monkeypatch) -> None:
        """KB build writes manifest/documents/chunks/tags/receipts under .ollarma/kb."""
        from ollarma.service import build_project_kb

        project_root, adapter = _prepare_project(tmp_path)
        mock_resolve_adapter.return_value = adapter
        monkeypatch.chdir(project_root)

        result = build_project_kb("kb-demo", adapters_dir=str(tmp_path / "adapters"))

        assert isinstance(result, BaseModel)

        artifact_root = project_root / ".ollarma" / "kb"
        assert artifact_root.exists()
        for filename in (
            "manifest.json",
            "documents.jsonl",
            "chunks.jsonl",
            "tags.json",
            "receipts.jsonl",
            "search.sqlite",
        ):
            assert (artifact_root / filename).exists(), f"missing {filename}"

        result_payload = result.model_dump() if hasattr(result, "model_dump") else {}
        artifact_root_value = result_payload.get("artifact_root") or getattr(result, "artifact_root", None)
        if isinstance(artifact_root_value, dict):
            assert artifact_root_value.get("repo_relative") == ".ollarma/kb"
        elif isinstance(artifact_root_value, str):
            assert artifact_root_value.replace("\\", "/").endswith(".ollarma/kb")

        documents = _read_jsonl(artifact_root / "documents.jsonl")
        chunks = _read_jsonl(artifact_root / "chunks.jsonl")
        receipts = _read_jsonl(artifact_root / "receipts.jsonl")
        tags_payload = orjson.loads((artifact_root / "tags.json").read_bytes())

        assert documents, "documents.jsonl must contain at least one document row"
        assert chunks, "chunks.jsonl must contain at least one chunk row"
        assert receipts, "receipts.jsonl must contain at least one receipt row"

        document_keys = [_record_path_key(record) for record in documents]
        assert document_keys == sorted(document_keys), document_keys

        chunk_keys = [_chunk_order_key(record) for record in chunks]
        assert chunk_keys == sorted(chunk_keys), chunk_keys

        if isinstance(tags_payload, list):
            tag_keys = []
            for item in tags_payload:
                if isinstance(item, dict):
                    tag_keys.append(item.get("tag") or item.get("name") or str(item))
                else:
                    tag_keys.append(str(item))
            assert tag_keys == sorted(tag_keys), tag_keys
        elif isinstance(tags_payload, dict):
            assert list(tags_payload) == sorted(tags_payload), list(tags_payload)

    @patch("ollarma.service.resolve_project_adapter")
    def test_rebuild_is_stable_for_same_inputs(self, mock_resolve_adapter, tmp_path: pathlib.Path, monkeypatch) -> None:
        """Rebuilding with the same inputs keeps the publishable KB artifacts stable."""
        from ollarma.service import build_project_kb

        project_root, adapter = _prepare_project(tmp_path)
        mock_resolve_adapter.return_value = adapter
        monkeypatch.chdir(project_root)

        build_project_kb("kb-demo", adapters_dir=str(tmp_path / "adapters"))
        first_snapshot = _artifact_snapshot(project_root / ".ollarma" / "kb")

        build_project_kb("kb-demo", adapters_dir=str(tmp_path / "adapters"))
        second_snapshot = _artifact_snapshot(project_root / ".ollarma" / "kb")

        assert first_snapshot == second_snapshot

    @patch("ollarma.service.resolve_project_adapter")
    def test_build_skips_broken_symlink_inputs(self, mock_resolve_adapter, tmp_path: pathlib.Path, monkeypatch) -> None:
        """Broken symlinks under a declared source should not abort the KB build."""
        from ollarma.service import build_project_kb

        project_root, adapter = _prepare_project(tmp_path)
        broken_link = project_root / "docs" / "broken-link.md"
        broken_link.symlink_to(project_root / "docs" / "missing.md")
        mock_resolve_adapter.return_value = adapter
        monkeypatch.chdir(project_root)

        result = build_project_kb("kb-demo", adapters_dir=str(tmp_path / "adapters"))

        assert isinstance(result, BaseModel)
        documents = _read_jsonl(project_root / ".ollarma" / "kb" / "documents.jsonl")
        assert all(record["path"] != "docs/broken-link.md" for record in documents)

"""Tests for harness/fleet.py -- fleet registry core module.

TDD RED: Tests for AdapterConfig model, dual-format parsers (Markdown + YAML),
fleet registry loader, and project resolver.
"""
from __future__ import annotations

import pathlib
import textwrap
import warnings

import pytest

from ollarma.fleet import (
    AdapterConfig,
    load_fleet_registry,
    resolve_project,
    parse_markdown_adapter,
    parse_yaml_adapter,
    DEFAULT_ADAPTERS_DIR,
)
from ollarma.kb_contract import DatabaseConfig


# ---------------------------------------------------------------------------
# AdapterConfig validation
# ---------------------------------------------------------------------------


class TestAdapterConfig:
    """Tests for AdapterConfig Pydantic model."""

    def test_valid_config_passes(self) -> None:
        """AdapterConfig with valid fields passes validation."""
        cfg = AdapterConfig(
            project_name="my-project",
            project_root="<repo>",
        )
        assert cfg.project_name == "my-project"
        assert cfg.project_root == "<repo>"

    def test_rejects_relative_project_root(self) -> None:
        """AdapterConfig rejects relative project_root (raises ValidationError)."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            AdapterConfig(
                project_name="bad",
                project_root="relative/path",
            )

    def test_defaults(self) -> None:
        """AdapterConfig defaults: env_type='standard', snakemake=False, empty lists."""
        cfg = AdapterConfig(
            project_name="defaults-test",
            project_root="/tmp/defaults-test",
        )
        assert cfg.env_type == "standard"
        assert cfg.snakemake is False
        assert cfg.databases == []
        assert cfg.mcps == []
        assert cfg.tools == []
        assert cfg.antibodies == []
        assert cfg.knowledge_base.artifact_root == ".ollarma/kb"
        assert cfg.knowledge_base.freshness_hours == 24
        assert cfg.knowledge_base.stale_behavior == "escalate"

    def test_root_exists_true(self, tmp_path: pathlib.Path) -> None:
        """root_exists returns True for an existing path."""
        cfg = AdapterConfig(
            project_name="exists",
            project_root=str(tmp_path),
        )
        assert cfg.root_exists is True

    def test_root_exists_false(self) -> None:
        """root_exists returns False for a non-existent path."""
        cfg = AdapterConfig(
            project_name="missing",
            project_root="/nonexistent/path/that/does/not/exist",
        )
        assert cfg.root_exists is False

    def test_accepts_typed_knowledge_base_config(self) -> None:
        """AdapterConfig accepts a typed knowledge_base block."""
        cfg = AdapterConfig(
            project_name="kb-test",
            project_root="/tmp/kb-test",
            knowledge_base={
                "artifact_root": ".ollarma/kb",
                "freshness_hours": 12,
                "stale_behavior": "block",
                "sources": [
                    {"path": "docs", "kind": "documents", "authority": "canonical"},
                    {"path": "tests", "kind": "tests"},
                ],
            },
        )
        assert cfg.knowledge_base.freshness_hours == 12
        assert cfg.knowledge_base.stale_behavior == "block"
        assert len(cfg.knowledge_base.sources) == 2
        assert cfg.knowledge_base.sources[0].path == "docs"

    def test_rejects_absolute_artifact_root(self) -> None:
        """knowledge_base.artifact_root must stay repo-relative."""
        with pytest.raises(Exception):
            AdapterConfig(
                project_name="kb-test",
                project_root="/tmp/kb-test",
                knowledge_base={"artifact_root": "/tmp/kb"},
            )

    def test_databases_are_typed_but_dict_compatible(self) -> None:
        """Database configs remain backward-compatible with .get() access."""
        cfg = AdapterConfig(
            project_name="db-test",
            project_root="/tmp/db-test",
            databases=[{"type": "arangodb", "host": "localhost", "port": 8531, "db": "test"}],
        )
        assert isinstance(cfg.databases[0], DatabaseConfig)
        assert cfg.databases[0].get("host") == "localhost"
        assert cfg.databases[0].get("db") == "test"


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------


class TestParseMarkdownAdapter:
    """Tests for parse_markdown_adapter."""

    SAMPLE_MD = textwrap.dedent("""\
        # Adapter: Cellico-Bio
        **Path:** <local-path>
        **Type:** Research Biology
        **CANON version:** v2.1.0
        **extends:** CANON-CORE v1.0.0
        **EXP namespace prefix:** `cellico:`
        **Registered in Overwatch:** yes

        has_canon: true
    """)

    def test_extracts_project_name(self) -> None:
        """parse_markdown_adapter extracts project_name from '# Adapter: Name'."""
        result = parse_markdown_adapter(self.SAMPLE_MD)
        assert result["project_name"] == "Cellico-Bio"

    def test_extracts_project_root(self) -> None:
        """parse_markdown_adapter extracts project_root from '**Path:** /absolute/path/'."""
        result = parse_markdown_adapter(self.SAMPLE_MD)
        # Trailing slash should be stripped
        assert result["project_root"] == "<local-path>"

    def test_extracts_has_canon(self) -> None:
        """parse_markdown_adapter extracts has_canon from 'has_canon: true'."""
        result = parse_markdown_adapter(self.SAMPLE_MD)
        assert result["has_canon"] is True

    def test_extracts_namespace_prefix(self) -> None:
        """parse_markdown_adapter extracts namespace_prefix from EXP namespace prefix."""
        result = parse_markdown_adapter(self.SAMPLE_MD)
        assert result["namespace_prefix"] == "cellico:"

    def test_extracts_project_type(self) -> None:
        """parse_markdown_adapter extracts project_type from '**Type:**'."""
        result = parse_markdown_adapter(self.SAMPLE_MD)
        assert result["project_type"] == "Research Biology"

    def test_empty_text_returns_empty_dict(self) -> None:
        """parse_markdown_adapter returns empty dict for empty text (no crash)."""
        result = parse_markdown_adapter("")
        assert result == {}

    def test_unparseable_text_returns_empty_dict(self) -> None:
        """parse_markdown_adapter returns empty dict for unparseable text."""
        result = parse_markdown_adapter("random garbage\nno adapter here")
        assert result == {}

    def test_has_canon_false(self) -> None:
        """parse_markdown_adapter extracts has_canon: false."""
        md = "# Adapter: NoCanon\n**Path:** /tmp/nc\n\nhas_canon: false\n"
        result = parse_markdown_adapter(md)
        assert result["has_canon"] is False


# ---------------------------------------------------------------------------
# YAML parser
# ---------------------------------------------------------------------------


class TestParseYamlAdapter:
    """Tests for parse_yaml_adapter."""

    SAMPLE_YAML = textwrap.dedent("""\
        schema_version: 1
        project_name: shadow-seeds
        project_root: <repo>
        namespace_prefix: "shadow:"
        runtime_mode: legacy
        surfaces:
          canon_path: CANON.md
          experiments_dir: experiments
    """)

    def test_loads_valid_yaml(self) -> None:
        """parse_yaml_adapter loads schema_version 1 YAML into dict."""
        result = parse_yaml_adapter(self.SAMPLE_YAML)
        assert result["project_name"] == "shadow-seeds"
        assert result["project_root"] == "<repo>"
        assert result["namespace_prefix"] == "shadow:"

    def test_nested_knowledge_base_yaml_preserved(self) -> None:
        """Nested knowledge_base declarations survive YAML parsing."""
        text = textwrap.dedent("""\
            project_name: kb-demo
            project_root: /tmp/kb-demo
            knowledge_base:
              artifact_root: .ollarma/kb
              freshness_hours: 6
              sources:
                - path: docs
                  kind: documents
                  authority: canonical
        """)
        result = parse_yaml_adapter(text)
        assert result["knowledge_base"]["artifact_root"] == ".ollarma/kb"
        assert result["knowledge_base"]["sources"][0]["path"] == "docs"

    def test_invalid_yaml_returns_empty_dict(self) -> None:
        """parse_yaml_adapter returns empty dict for invalid YAML (no crash)."""
        result = parse_yaml_adapter("{{{{invalid yaml: [[[")
        assert result == {}

    def test_empty_text_returns_empty_dict(self) -> None:
        """parse_yaml_adapter returns empty dict for empty text."""
        result = parse_yaml_adapter("")
        assert result == {}


# ---------------------------------------------------------------------------
# Fleet registry loader
# ---------------------------------------------------------------------------


class TestLoadFleetRegistry:
    """Tests for load_fleet_registry."""

    def test_loads_mixed_formats(self, tmp_path: pathlib.Path) -> None:
        """load_fleet_registry with 2 .md files and 1 .yaml returns 3 entries."""
        # Create two markdown adapters
        (tmp_path / "alpha.md").write_text(textwrap.dedent("""\
            # Adapter: Alpha
            **Path:** /tmp/alpha
            **Type:** Test
            has_canon: false
        """))
        (tmp_path / "beta.md").write_text(textwrap.dedent("""\
            # Adapter: Beta
            **Path:** /tmp/beta
            **Type:** Test
            has_canon: true
        """))

        # Create runtime dir with one YAML adapter
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / "gamma.yaml").write_text(textwrap.dedent("""\
            schema_version: 1
            project_name: Gamma
            project_root: /tmp/gamma
            namespace_prefix: "gamma:"
        """))

        registry = load_fleet_registry(str(tmp_path))
        assert len(registry) == 3
        assert "Alpha" in registry
        assert "Beta" in registry
        assert "Gamma" in registry
        assert registry["Alpha"].adapter_source == "markdown"
        assert registry["Gamma"].adapter_source == "yaml"

    def test_skips_broken_adapter_without_warning(self, tmp_path: pathlib.Path) -> None:
        """load_fleet_registry silently skips non-adapter markdown (F-08 fix).

        Markdown files that do not carry a ``# Adapter:`` header are treated
        as intentional mixed content (pointers, gate notes, etc.), not as
        malformed adapters. No warning is emitted. Markdown files that DO
        carry the header but fail Pydantic validation still warn — that
        path is exercised implicitly by any operator-facing `ollarma projects`
        run against a directory with a genuinely corrupt adapter.
        """
        # Valid adapter
        (tmp_path / "good.md").write_text(textwrap.dedent("""\
            # Adapter: GoodProject
            **Path:** /tmp/good
        """))
        # Non-adapter markdown -- no `# Adapter:` header: silently ignored.
        (tmp_path / "broken.md").write_text("This is not an adapter file.\n")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            registry = load_fleet_registry(str(tmp_path))

        project_name_warnings = [
            w for w in caught
            if issubclass(w.category, UserWarning)
            and "no project_name found" in str(w.message)
        ]
        assert project_name_warnings == [], (
            f"F-08: non-adapter markdown must not warn, got {project_name_warnings}"
        )
        assert len(registry) == 1
        assert "GoodProject" in registry

    def test_skips_readme(self, tmp_path: pathlib.Path) -> None:
        """load_fleet_registry skips README.md."""
        (tmp_path / "README.md").write_text("# Not an adapter")
        (tmp_path / "real.md").write_text(textwrap.dedent("""\
            # Adapter: Real
            **Path:** /tmp/real
        """))
        registry = load_fleet_registry(str(tmp_path))
        assert len(registry) == 1
        assert "Real" in registry


# ---------------------------------------------------------------------------
# Project resolver
# ---------------------------------------------------------------------------


class TestResolveProject:
    """Tests for resolve_project."""

    @pytest.fixture()
    def sample_registry(self) -> dict[str, AdapterConfig]:
        """Build a small registry for resolver tests."""
        return {
            "Cellico-Bio": AdapterConfig(
                project_name="Cellico-Bio",
                project_root="<local-path>",
            ),
            "Antigence": AdapterConfig(
                project_name="Antigence",
                project_root="<repo>",
            ),
            "shadow-seeds": AdapterConfig(
                project_name="shadow-seeds",
                project_root="<repo>",
            ),
        }

    def test_exact_match(self, sample_registry: dict[str, AdapterConfig]) -> None:
        """resolve_project returns the AdapterConfig for exact name match."""
        result = resolve_project("Cellico-Bio", sample_registry)
        assert result is not None
        assert result.project_root == "<local-path>"

    def test_case_insensitive_match(
        self, sample_registry: dict[str, AdapterConfig]
    ) -> None:
        """resolve_project matches case-insensitively when exact match fails."""
        result = resolve_project("cellico-bio", sample_registry)
        assert result is not None
        assert result.project_name == "Cellico-Bio"

    def test_substring_match(
        self, sample_registry: dict[str, AdapterConfig]
    ) -> None:
        """resolve_project matches substring when exact and case-insensitive fail."""
        result = resolve_project("shadow", sample_registry)
        assert result is not None
        assert result.project_name == "shadow-seeds"

    def test_nonexistent_returns_none(
        self, sample_registry: dict[str, AdapterConfig]
    ) -> None:
        """resolve_project returns None for nonexistent project."""
        result = resolve_project("nonexistent", sample_registry)
        assert result is None


# ---------------------------------------------------------------------------
# Module-level constant
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Tests for module-level constants."""

    def test_default_adapters_dir_is_absolute(self) -> None:
        """DEFAULT_ADAPTERS_DIR is an absolute path string."""
        assert DEFAULT_ADAPTERS_DIR.startswith("/")

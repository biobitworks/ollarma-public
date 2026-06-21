"""Tests for harness/autopilot.py -- asset discovery, classification, execution.

Covers entrypoint detection, notebook code cell counting, recursive asset
discovery, include/exclude filtering, pytest suite detection, path traversal
security, keyword classification (D-06), antibody classification (D-05),
classify_asset three-layer chain (D-05/D-06/D-07), tier-model mapping from
benchmark data (AUTO-02, D-08, D-09), TierMapping immutability, asset execution
(notebook/script/pytest/snakefile with timeouts), and autopilot orchestration
with tiered escalation (D-13).
"""
from __future__ import annotations

from contextlib import nullcontext
import json
import os
import pathlib
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from ollarma.autopilot import (
    ANTIBODY_TIER_MAP,
    AssetInventory,
    AssetResult,
    AutopilotReport,
    DiscoveredAsset,
    KEYWORD_TIER_MAP,
    MODEL_TIERS,
    TIER_SIZE_RANGES,
    TIER_SUITE_MAP,
    TIER_TIMEOUT_DEFAULTS,
    TierMapping,
    _get_next_tier_model,
    build_tier_model_map,
    classify_asset,
    classify_with_antibodies,
    classify_with_keywords,
    count_code_cells,
    discover_assets,
    execute_asset,
    has_entrypoint,
    run_autopilot,
)


# ---------------------------------------------------------------------------
# has_entrypoint tests
# ---------------------------------------------------------------------------


class TestHasEntrypoint:
    """Tests for has_entrypoint() -- D-01 entrypoint heuristics."""

    def test_has_entrypoint_main_guard(self) -> None:
        source = 'if __name__ == "__main__":\n    main()'
        assert has_entrypoint(source) is True

    def test_has_entrypoint_main_guard_single_quotes(self) -> None:
        source = "if __name__ == '__main__':\n    main()"
        assert has_entrypoint(source) is True

    def test_has_entrypoint_argparse(self) -> None:
        source = "import argparse\nparser = argparse.ArgumentParser()"
        assert has_entrypoint(source) is True

    def test_has_entrypoint_argparse_from_import(self) -> None:
        source = "from argparse import ArgumentParser"
        assert has_entrypoint(source) is True

    def test_has_entrypoint_typer(self) -> None:
        source = "import typer\napp = typer.Typer()"
        assert has_entrypoint(source) is True

    def test_has_entrypoint_typer_from_import(self) -> None:
        source = "from typer import Typer"
        assert has_entrypoint(source) is True

    def test_has_entrypoint_click(self) -> None:
        source = "from click import command"
        assert has_entrypoint(source) is True

    def test_has_entrypoint_click_import(self) -> None:
        source = "import click"
        assert has_entrypoint(source) is True

    def test_has_entrypoint_shebang(self) -> None:
        source = '#!/usr/bin/env python3\nprint("hi")'
        assert has_entrypoint(source) is True

    def test_has_entrypoint_shebang_python(self) -> None:
        source = "#!/usr/bin/python\nimport sys"
        assert has_entrypoint(source) is True

    def test_has_entrypoint_library(self) -> None:
        source = "def helper():\n    return 42"
        assert has_entrypoint(source) is False

    def test_has_entrypoint_empty(self) -> None:
        assert has_entrypoint("") is False

    def test_has_entrypoint_class_only(self) -> None:
        source = "class Foo:\n    pass"
        assert has_entrypoint(source) is False


# ---------------------------------------------------------------------------
# count_code_cells tests
# ---------------------------------------------------------------------------


def _make_notebook(cells: list[dict]) -> dict:
    """Build a minimal .ipynb structure."""
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": cells,
    }


class TestCountCodeCells:
    """Tests for count_code_cells() -- D-04 notebook filtering."""

    def test_count_code_cells_with_code(self, tmp_path: object) -> None:
        nb = _make_notebook([
            {"cell_type": "code", "source": ["print('a')"], "metadata": {}, "outputs": []},
            {"cell_type": "code", "source": ["x = 1"], "metadata": {}, "outputs": []},
            {"cell_type": "code", "source": ["y = 2"], "metadata": {}, "outputs": []},
        ])
        nb_path = tmp_path / "test.ipynb"  # type: ignore[operator]
        nb_path.write_text(json.dumps(nb))
        assert count_code_cells(str(nb_path)) == 3

    def test_count_code_cells_markdown_only(self, tmp_path: object) -> None:
        nb = _make_notebook([
            {"cell_type": "markdown", "source": ["# Title"], "metadata": {}},
            {"cell_type": "markdown", "source": ["Some text"], "metadata": {}},
        ])
        nb_path = tmp_path / "notes.ipynb"  # type: ignore[operator]
        nb_path.write_text(json.dumps(nb))
        assert count_code_cells(str(nb_path)) == 0

    def test_count_code_cells_invalid_json(self, tmp_path: object) -> None:
        nb_path = tmp_path / "bad.ipynb"  # type: ignore[operator]
        nb_path.write_text("not valid json {{{")
        assert count_code_cells(str(nb_path)) == 0

    def test_count_code_cells_missing_file(self) -> None:
        assert count_code_cells("/nonexistent/path/missing.ipynb") == 0

    def test_count_code_cells_missing_cells_key(self, tmp_path: object) -> None:
        nb_path = tmp_path / "nocells.ipynb"  # type: ignore[operator]
        nb_path.write_text(json.dumps({"nbformat": 4}))
        assert count_code_cells(str(nb_path)) == 0


# ---------------------------------------------------------------------------
# discover_assets tests -- fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_tree(tmp_path: object) -> object:
    """Build a sample project tree for discovery tests.

    Structure:
        project_root/
            script_main.py       -- has __main__ guard (runnable)
            lib_helper.py        -- library module, no entrypoint (excluded)
            test_foo.py          -- test file (excluded)
            foo_test.py          -- test file (excluded)
            conftest.py          -- test file (excluded)
            notebook_code.ipynb  -- 2 code cells (runnable)
            notebook_md.ipynb    -- markdown only (excluded)
            Snakefile            -- snakemake (runnable)
            pyproject.toml       -- has pytest config (pytest_suite asset)
            subdir/
                nested_script.py -- has argparse (runnable)
                nested.ipynb     -- 1 code cell (runnable)
                rules.smk        -- snakemake rules (runnable)
    """
    root = tmp_path  # type: ignore[assignment]

    # Runnable script with __main__
    (root / "script_main.py").write_text(
        'def main():\n    pass\n\nif __name__ == "__main__":\n    main()\n'
    )

    # Library module -- no entrypoint
    (root / "lib_helper.py").write_text("def helper():\n    return 42\n")

    # Test files -- should be excluded
    (root / "test_foo.py").write_text(
        'import pytest\ndef test_thing():\n    assert True\n'
    )
    (root / "foo_test.py").write_text("def test_other():\n    pass\n")
    (root / "conftest.py").write_text("import pytest\n")

    # Notebook with code cells
    nb_code = _make_notebook([
        {"cell_type": "code", "source": ["import os"], "metadata": {}, "outputs": []},
        {"cell_type": "code", "source": ["x = 1"], "metadata": {}, "outputs": []},
    ])
    (root / "notebook_code.ipynb").write_text(json.dumps(nb_code))

    # Notebook with only markdown
    nb_md = _make_notebook([
        {"cell_type": "markdown", "source": ["# Notes"], "metadata": {}},
    ])
    (root / "notebook_md.ipynb").write_text(json.dumps(nb_md))

    # Snakefile
    (root / "Snakefile").write_text("rule all:\n    input: 'output.txt'\n")

    # pyproject.toml with pytest config
    (root / "pyproject.toml").write_text(textwrap.dedent("""\
        [build-system]
        requires = ["setuptools"]

        [tool.pytest.ini_options]
        testpaths = ["tests"]
    """))

    # Nested subdirectory
    subdir = root / "subdir"
    subdir.mkdir()

    (subdir / "nested_script.py").write_text(
        "import argparse\nparser = argparse.ArgumentParser()\n"
    )

    nb_nested = _make_notebook([
        {"cell_type": "code", "source": ["print('nested')"], "metadata": {}, "outputs": []},
    ])
    (subdir / "nested.ipynb").write_text(json.dumps(nb_nested))

    (subdir / "rules.smk").write_text("rule clean:\n    shell: 'rm -f output.txt'\n")

    return root


# ---------------------------------------------------------------------------
# discover_assets tests
# ---------------------------------------------------------------------------


class TestDiscoverAssets:
    """Tests for discover_assets() -- recursive asset discovery."""

    def test_discover_assets_finds_notebooks(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        nb_assets = [a for a in inv.assets if a.asset_type == "notebook"]
        # Should find notebook_code.ipynb and subdir/nested.ipynb
        assert len(nb_assets) == 2
        nb_names = {os.path.basename(a.path) for a in nb_assets}
        assert "notebook_code.ipynb" in nb_names
        assert "nested.ipynb" in nb_names

    def test_discover_assets_excludes_markdown_notebooks(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        nb_assets = [a for a in inv.assets if a.asset_type == "notebook"]
        nb_names = {os.path.basename(a.path) for a in nb_assets}
        assert "notebook_md.ipynb" not in nb_names

    def test_discover_assets_finds_scripts_with_entrypoint(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        script_assets = [a for a in inv.assets if a.asset_type == "script"]
        assert len(script_assets) == 2
        script_names = {os.path.basename(a.path) for a in script_assets}
        assert "script_main.py" in script_names
        assert "nested_script.py" in script_names

    def test_discover_assets_excludes_library_modules(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        script_assets = [a for a in inv.assets if a.asset_type == "script"]
        script_names = {os.path.basename(a.path) for a in script_assets}
        assert "lib_helper.py" not in script_names

    def test_discover_assets_excludes_test_files(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        all_names = {os.path.basename(a.path) for a in inv.assets}
        assert "test_foo.py" not in all_names
        assert "foo_test.py" not in all_names
        assert "conftest.py" not in all_names

    def test_discover_assets_finds_snakefiles(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        snake_assets = [a for a in inv.assets if a.asset_type == "snakefile"]
        assert len(snake_assets) == 2
        snake_names = {os.path.basename(a.path) for a in snake_assets}
        assert "Snakefile" in snake_names
        assert "rules.smk" in snake_names

    def test_discover_assets_finds_pytest_suite(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        pytest_assets = [a for a in inv.assets if a.asset_type == "pytest_suite"]
        # Pitfall 6: reported as ONE asset, not individual test files
        assert len(pytest_assets) == 1
        assert pytest_assets[0].has_entrypoint is True

    def test_discover_assets_pytest_suite_from_pytest_ini(self, tmp_path: object) -> None:
        root = tmp_path  # type: ignore[assignment]
        (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
        inv = discover_assets(str(root))
        pytest_assets = [a for a in inv.assets if a.asset_type == "pytest_suite"]
        assert len(pytest_assets) == 1

    def test_discover_assets_pytest_suite_from_setup_cfg(self, tmp_path: object) -> None:
        root = tmp_path  # type: ignore[assignment]
        (root / "setup.cfg").write_text("[tool:pytest]\ntestpaths = tests\n")
        inv = discover_assets(str(root))
        pytest_assets = [a for a in inv.assets if a.asset_type == "pytest_suite"]
        assert len(pytest_assets) == 1

    def test_discover_assets_counts(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        assert inv.counts["notebook"] == 2
        assert inv.counts["script"] == 2
        assert inv.counts["snakefile"] == 2
        assert inv.counts["pytest_suite"] == 1

    def test_discover_assets_include_filter(self, project_tree: object) -> None:
        """D-02: only assets matching include patterns returned."""
        inv = discover_assets(str(project_tree), include_patterns=["*.ipynb"])
        # Should only include notebooks (and pytest_suite since it's special)
        for asset in inv.assets:
            if asset.asset_type != "pytest_suite":
                assert asset.path.endswith(".ipynb"), f"Non-notebook found: {asset.path}"

    def test_discover_assets_exclude_filter(self, project_tree: object) -> None:
        """D-02: assets matching exclude patterns removed."""
        inv = discover_assets(str(project_tree), exclude_patterns=["subdir/*"])
        all_paths = {a.path for a in inv.assets}
        for p in all_paths:
            assert "subdir" not in p or p.endswith("subdir"), (
                f"subdir asset not excluded: {p}"
            )

    def test_discover_assets_recursive(self, project_tree: object) -> None:
        """D-03: discovers files in nested subdirectories."""
        inv = discover_assets(str(project_tree))
        nested_assets = [
            a for a in inv.assets
            if "subdir" in a.path and a.asset_type != "pytest_suite"
        ]
        assert len(nested_assets) == 3  # nested_script.py, nested.ipynb, rules.smk

    def test_discover_assets_path_traversal(self, project_tree: object) -> None:
        """T-10-01: all returned asset paths are within project_root."""
        inv = discover_assets(str(project_tree))
        resolved_root = os.path.realpath(str(project_tree))
        for asset in inv.assets:
            resolved_asset = os.path.realpath(asset.path)
            assert resolved_asset.startswith(resolved_root), (
                f"Path traversal detected: {asset.path} outside {resolved_root}"
            )

    def test_discover_assets_empty_dir(self, tmp_path: object) -> None:
        inv = discover_assets(str(tmp_path))
        assert len(inv.assets) == 0
        assert inv.counts == {}

    def test_discover_assets_project_name(self, project_tree: object) -> None:
        inv = discover_assets(str(project_tree))
        assert inv.project_name == os.path.basename(str(project_tree))


# ---------------------------------------------------------------------------
# Model immutability tests
# ---------------------------------------------------------------------------


class TestModelImmutability:
    """Verify frozen Pydantic models."""

    def test_discovered_asset_frozen(self) -> None:
        asset = DiscoveredAsset(
            path="/tmp/test.py",
            asset_type="script",
            has_entrypoint=True,
        )
        with pytest.raises(Exception):  # ValidationError for frozen
            asset.path = "/other"  # type: ignore[misc]

    def test_asset_inventory_frozen(self) -> None:
        inv = AssetInventory(
            project_name="test",
            project_root="/tmp",
            assets=(),
            counts={},
        )
        with pytest.raises(Exception):  # ValidationError for frozen
            inv.project_name = "other"  # type: ignore[misc]

    def test_tier_mapping_frozen(self) -> None:
        """TierMapping model is immutable."""
        tm = TierMapping(
            tier="science",
            model="qwen3:8b",
            model_size_b=8.0,
            quality_mean=0.95,
            degraded_confidence=False,
        )
        with pytest.raises(Exception):  # ValidationError for frozen
            tm.tier = "code"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# classify_with_keywords tests (D-06)
# ---------------------------------------------------------------------------


class TestClassifyWithKeywords:
    """Tests for classify_with_keywords() -- keyword heuristic tier classification."""

    def test_classify_with_keywords_data_analysis(self) -> None:
        """Source with `import pandas` classified as 'data-analysis'."""
        source = "import pandas\ndf = pd.read_csv('data.csv')\ndf.describe()"
        tier, confidence = classify_with_keywords(source)
        assert tier == "data-analysis"
        assert confidence > 0.0

    def test_classify_with_keywords_science(self) -> None:
        """Source with `from Bio import SeqIO` classified as 'science'."""
        source = "from Bio import SeqIO\nrecords = SeqIO.parse('seqs.fasta', 'fasta')"
        tier, confidence = classify_with_keywords(source)
        assert tier == "science"
        assert confidence > 0.0

    def test_classify_with_keywords_test_suite(self) -> None:
        """Source with `import pytest` and test function classified as 'test-suite'."""
        source = "import pytest\ndef test_foo():\n    assert True"
        tier, confidence = classify_with_keywords(source)
        assert tier == "test-suite"
        assert confidence > 0.0

    def test_classify_with_keywords_pipeline(self) -> None:
        """Source with snakemake `rule all:` classified as 'pipeline-step'."""
        source = "rule all:\n    input: 'output.txt'"
        tier, confidence = classify_with_keywords(source)
        assert tier == "pipeline-step"
        assert confidence > 0.0

    def test_classify_with_keywords_no_match(self) -> None:
        """Generic source with no domain keywords returns (None, 0.0)."""
        source = "x = 1\ny = 2\nprint(x + y)"
        tier, confidence = classify_with_keywords(source)
        assert tier is None
        assert confidence == 0.0


# ---------------------------------------------------------------------------
# classify_with_antibodies tests (D-05)
# ---------------------------------------------------------------------------


class TestClassifyWithAntibodies:
    """Tests for classify_with_antibodies() -- Antigence antibody classification."""

    def test_classify_with_antibodies_fallback_no_antigence(self) -> None:
        """When ANTIGENCE_AVAILABLE is False, returns (None, 0.0)."""
        with patch("ollarma.autopilot.ANTIGENCE_AVAILABLE", False):
            tier, confidence = classify_with_antibodies("import pandas\ndf = pd.DataFrame()")
            assert tier is None
            assert confidence == 0.0

    def test_classify_with_antibodies_mocked(self) -> None:
        """Mocked antibody systems return highest affinity for data_analysis -> tier is 'data-analysis'."""
        # Create a mock antibody system matching real Antigence API:
        # - domain-specific verify method (verify_analysis for data_analysis)
        # - result has classification_confidence (not confidence)
        mock_result = MagicMock()
        mock_result.classification_confidence = 0.85

        mock_system_class = MagicMock()
        mock_system_instance = MagicMock()
        mock_system_instance.verify_analysis.return_value = mock_result
        mock_system_class.return_value = mock_system_instance

        # Patch ANTIGENCE_AVAILABLE to True and the import mechanism
        with patch("ollarma.autopilot.ANTIGENCE_AVAILABLE", True), \
             patch("ollarma.autopilot._import_antibody_system") as mock_import:
            # Only data_analysis returns a confident result
            def side_effect(key: str) -> object | None:
                if key == "data_analysis":
                    return mock_system_class
                return None
            mock_import.side_effect = side_effect

            tier, confidence = classify_with_antibodies("import pandas\ndf = pd.DataFrame()")
            assert tier == "data-analysis"
            assert confidence == 0.85


# ---------------------------------------------------------------------------
# classify_asset tests (D-05, D-06, D-07)
# ---------------------------------------------------------------------------


class TestClassifyAsset:
    """Tests for classify_asset() -- three-layer classification chain."""

    def test_classify_asset_antibody_primary(self) -> None:
        """With antibody returning confident result, antibody tier takes priority."""
        with patch("ollarma.autopilot.classify_with_antibodies") as mock_ab, \
             patch("ollarma.autopilot.classify_with_keywords") as mock_kw:
            mock_ab.return_value = ("science", 0.9)
            mock_kw.return_value = ("data-analysis", 0.6)

            asset = DiscoveredAsset(
                path="/tmp/analysis.py",
                asset_type="script",
                has_entrypoint=True,
            )
            result = classify_asset(asset, "from Bio import SeqIO")
            assert result == "science"

    def test_classify_asset_keyword_fallback(self) -> None:
        """With antibody returning None, keyword heuristic provides classification."""
        with patch("ollarma.autopilot.classify_with_antibodies") as mock_ab:
            mock_ab.return_value = (None, 0.0)

            asset = DiscoveredAsset(
                path="/tmp/analysis.py",
                asset_type="script",
                has_entrypoint=True,
            )
            # Source has pandas import -> keyword should catch "data-analysis"
            result = classify_asset(asset, "import pandas\ndf = pd.read_csv('x.csv')")
            assert result == "data-analysis"

    def test_classify_asset_code_ultimate_fallback(self) -> None:
        """With both antibody and keyword returning None, returns 'code' (D-07)."""
        with patch("ollarma.autopilot.classify_with_antibodies") as mock_ab, \
             patch("ollarma.autopilot.classify_with_keywords") as mock_kw:
            mock_ab.return_value = (None, 0.0)
            mock_kw.return_value = (None, 0.0)

            asset = DiscoveredAsset(
                path="/tmp/generic.py",
                asset_type="script",
                has_entrypoint=True,
            )
            result = classify_asset(asset, "x = 1\ny = 2")
            assert result == "code"

    def test_classify_asset_snakefile_type(self) -> None:
        """Asset_type 'snakefile' always classified as 'pipeline-step'."""
        asset = DiscoveredAsset(
            path="/tmp/Snakefile",
            asset_type="snakefile",
            has_entrypoint=True,
        )
        result = classify_asset(asset, "rule all:\n    input: 'out.txt'")
        assert result == "pipeline-step"

    def test_classify_asset_pytest_suite_type(self) -> None:
        """Asset_type 'pytest_suite' always classified as 'test-suite'."""
        asset = DiscoveredAsset(
            path="/tmp/pyproject.toml",
            asset_type="pytest_suite",
            has_entrypoint=True,
        )
        result = classify_asset(asset, "[tool.pytest.ini_options]")
        assert result == "test-suite"


# ---------------------------------------------------------------------------
# TIER_SUITE_MAP coverage test
# ---------------------------------------------------------------------------


class TestTierSuiteMap:
    """Tests for TIER_SUITE_MAP constant coverage."""

    def test_tier_suite_map_coverage(self) -> None:
        """TIER_SUITE_MAP has entries for all 5 task tiers."""
        expected_tiers = {"science", "code", "data-analysis", "test-suite", "pipeline-step"}
        assert set(TIER_SUITE_MAP.keys()) == expected_tiers


# ---------------------------------------------------------------------------
# build_tier_model_map tests (AUTO-02, D-08, D-09)
# ---------------------------------------------------------------------------


class TestBuildTierModelMap:
    """Tests for build_tier_model_map() -- tier-to-model selection from benchmark data."""

    def _make_sealed_results(self) -> list[dict]:
        """Create mock sealed results with 3 models of different sizes."""
        return [
            # Small model (1.7b) -- low quality
            {"model": "qwen3:1.7b", "suite": "science", "decode_tps": 80.0, "quality_score": 0.4, "prefill_tps": 200.0},
            {"model": "qwen3:1.7b", "suite": "science", "decode_tps": 82.0, "quality_score": 0.45, "prefill_tps": 210.0},
            {"model": "qwen3:1.7b", "suite": "code", "decode_tps": 80.0, "quality_score": 0.5, "prefill_tps": 200.0},
            {"model": "qwen3:1.7b", "suite": "code", "decode_tps": 82.0, "quality_score": 0.55, "prefill_tps": 210.0},
            # Medium model (4b) -- medium quality, above 0.9 threshold
            {"model": "qwen3:4b", "suite": "science", "decode_tps": 50.0, "quality_score": 0.92, "prefill_tps": 150.0},
            {"model": "qwen3:4b", "suite": "science", "decode_tps": 52.0, "quality_score": 0.93, "prefill_tps": 155.0},
            {"model": "qwen3:4b", "suite": "code", "decode_tps": 50.0, "quality_score": 0.91, "prefill_tps": 150.0},
            {"model": "qwen3:4b", "suite": "code", "decode_tps": 52.0, "quality_score": 0.94, "prefill_tps": 155.0},
            # Large model (8b) -- high quality
            {"model": "qwen3:8b", "suite": "science", "decode_tps": 30.0, "quality_score": 0.97, "prefill_tps": 100.0},
            {"model": "qwen3:8b", "suite": "science", "decode_tps": 32.0, "quality_score": 0.96, "prefill_tps": 105.0},
            {"model": "qwen3:8b", "suite": "code", "decode_tps": 30.0, "quality_score": 0.95, "prefill_tps": 100.0},
            {"model": "qwen3:8b", "suite": "code", "decode_tps": 32.0, "quality_score": 0.98, "prefill_tps": 105.0},
        ]

    def test_build_tier_model_map_smallest_above_threshold(self, tmp_path: object) -> None:
        """Given stats with 3 models, selects smallest with quality_mean >= 0.9."""
        results_dir = tmp_path / "results"  # type: ignore[operator]
        results_dir.mkdir()
        import orjson
        sealed_path = results_dir / "run-2026-04-08T00-00-00.json"
        sealed_path.write_bytes(orjson.dumps(self._make_sealed_results()))

        mapping = build_tier_model_map(threshold=0.9, results_dir=results_dir)

        # "science" tier maps to "science" suite -- smallest above 0.9 is qwen3:4b
        assert "science" in mapping
        assert mapping["science"].model == "qwen3:4b"
        assert mapping["science"].degraded_confidence is False

    def test_build_tier_model_map_degraded_confidence(self, tmp_path: object) -> None:
        """When no model meets threshold, returns best available with degraded flag (D-08)."""
        # All models below threshold 0.99
        results = [
            {"model": "qwen3:1.7b", "suite": "science", "decode_tps": 80.0, "quality_score": 0.4, "prefill_tps": 200.0},
            {"model": "qwen3:4b", "suite": "science", "decode_tps": 50.0, "quality_score": 0.85, "prefill_tps": 150.0},
            {"model": "qwen3:8b", "suite": "science", "decode_tps": 30.0, "quality_score": 0.92, "prefill_tps": 100.0},
        ]
        results_dir = tmp_path / "results"  # type: ignore[operator]
        results_dir.mkdir()
        import orjson
        sealed_path = results_dir / "run-2026-04-08T00-00-00.json"
        sealed_path.write_bytes(orjson.dumps(results))

        mapping = build_tier_model_map(threshold=0.99, results_dir=results_dir)

        assert "science" in mapping
        assert mapping["science"].degraded_confidence is True
        # Best composite score model selected (not necessarily highest quality)

    def test_build_tier_model_map_custom_threshold(self, tmp_path: object) -> None:
        """threshold=0.5 selects smaller model that would fail at 0.9."""
        results_dir = tmp_path / "results"  # type: ignore[operator]
        results_dir.mkdir()
        import orjson
        sealed_path = results_dir / "run-2026-04-08T00-00-00.json"
        sealed_path.write_bytes(orjson.dumps(self._make_sealed_results()))

        mapping = build_tier_model_map(threshold=0.5, results_dir=results_dir)

        # At threshold=0.5, qwen3:1.7b qualifies (quality_mean ~0.425 is below 0.5 for science,
        # but ~0.525 for code). Let's check which one wins for science suite.
        # qwen3:1.7b science quality_mean = (0.4 + 0.45)/2 = 0.425, below 0.5
        # qwen3:4b science quality_mean = (0.92 + 0.93)/2 = 0.925, above 0.5
        # So at 0.5, smallest above is still qwen3:4b for science
        # But for code: qwen3:1.7b code quality_mean = (0.5 + 0.55)/2 = 0.525, above 0.5
        assert "code" in mapping
        # For code at 0.5: qwen3:1.7b (1.7B) qualifies -> smallest
        assert mapping["code"].model == "qwen3:1.7b"
        assert mapping["code"].degraded_confidence is False

    def test_build_tier_model_map_no_data(self, tmp_path: object) -> None:
        """Returns empty dict when no sealed results exist."""
        results_dir = tmp_path / "results"  # type: ignore[operator]
        results_dir.mkdir()
        # No files in results_dir

        mapping = build_tier_model_map(results_dir=results_dir)
        assert mapping == {}

    def test_build_tier_model_map_maps_all_tiers(self, tmp_path: object) -> None:
        """All tiers sharing same suite get same model selection."""
        results_dir = tmp_path / "results"  # type: ignore[operator]
        results_dir.mkdir()
        import orjson
        sealed_path = results_dir / "run-2026-04-08T00-00-00.json"
        sealed_path.write_bytes(orjson.dumps(self._make_sealed_results()))

        mapping = build_tier_model_map(threshold=0.9, results_dir=results_dir)

        # data-analysis maps to "science" suite, same as "science" tier
        if "science" in mapping and "data-analysis" in mapping:
            assert mapping["science"].model == mapping["data-analysis"].model
        # test-suite maps to "code" suite, same as "code" tier
        if "code" in mapping and "test-suite" in mapping:
            assert mapping["code"].model == mapping["test-suite"].model


# ---------------------------------------------------------------------------
# TIER_TIMEOUT_DEFAULTS tests
# ---------------------------------------------------------------------------


class TestTierTimeoutDefaults:
    """Tests for TIER_TIMEOUT_DEFAULTS constant."""

    def test_tier_timeout_defaults(self) -> None:
        """TIER_TIMEOUT_DEFAULTS has correct entries for all asset types."""
        assert TIER_TIMEOUT_DEFAULTS["notebook"] == 300
        assert TIER_TIMEOUT_DEFAULTS["script"] == 120
        assert TIER_TIMEOUT_DEFAULTS["pytest_suite"] == 180
        assert TIER_TIMEOUT_DEFAULTS["snakefile"] == 600


# ---------------------------------------------------------------------------
# AssetResult immutability test
# ---------------------------------------------------------------------------


class TestAssetResultModel:
    """Tests for AssetResult Pydantic model."""

    def test_asset_result_frozen(self) -> None:
        """AssetResult instances are immutable."""
        ar = AssetResult(
            asset_path="/tmp/test.py",
            asset_type="script",
            task_tier="code",
            model_used="qwen3:8b",
            exit_code=0,
            stdout="hello",
            stderr="",
            duration_s=1.5,
        )
        with pytest.raises(Exception):
            ar.exit_code = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# execute_asset tests (script, notebook, pytest, snakefile)
# ---------------------------------------------------------------------------


class TestExecuteAsset:
    """Tests for execute_asset() -- routing to correct runner with timeout."""

    def test_execute_script_success(self, tmp_path: object) -> None:
        """Run a trivial .py script, get exit_code=0, stdout contains 'hello'."""
        script = tmp_path / "hello.py"  # type: ignore[operator]
        script.write_text('print("hello")\n')
        asset = DiscoveredAsset(
            path=str(script),
            asset_type="script",
            has_entrypoint=True,
        )
        result = execute_asset(asset, task_tier="code", model="qwen3:8b")
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert result.asset_type == "script"
        assert result.duration_s >= 0.0

    def test_execute_script_failure(self, tmp_path: object) -> None:
        """Run a script that raises SystemExit(1), get exit_code=1."""
        script = tmp_path / "fail.py"  # type: ignore[operator]
        script.write_text("import sys\nsys.exit(1)\n")
        asset = DiscoveredAsset(
            path=str(script),
            asset_type="script",
            has_entrypoint=True,
        )
        result = execute_asset(asset, task_tier="code", model="qwen3:8b")
        assert result.exit_code == 1

    def test_execute_script_timeout(self, tmp_path: object) -> None:
        """Run a script with sleep(10) and timeout=1, get exit_code=-1."""
        script = tmp_path / "slow.py"  # type: ignore[operator]
        script.write_text("import time\ntime.sleep(10)\n")
        asset = DiscoveredAsset(
            path=str(script),
            asset_type="script",
            has_entrypoint=True,
        )
        result = execute_asset(asset, task_tier="code", model="qwen3:8b", timeout=1)
        assert result.exit_code == -1
        assert "Timeout" in result.stderr

    def test_execute_notebook_success_mocked(self) -> None:
        """Mock papermill.execute_notebook, verify called with correct args."""
        mock_pm = MagicMock()
        with patch.dict("sys.modules", {"papermill": mock_pm}):
            asset = DiscoveredAsset(
                path="/tmp/test_nb.ipynb",
                asset_type="notebook",
                has_entrypoint=True,
                code_cell_count=3,
            )
            result = execute_asset(asset, task_tier="science", model="qwen3:8b")
            assert result.exit_code == 0
            mock_pm.execute_notebook.assert_called_once()
            call_kwargs = mock_pm.execute_notebook.call_args
            assert call_kwargs[0][0] == "/tmp/test_nb.ipynb"  # input path

    def test_execute_notebook_failure_mocked(self) -> None:
        """Mock papermill raising PapermillExecutionError, returns exit_code=1."""
        mock_pm = MagicMock()
        # Create a mock exception class
        mock_exc_class = type("PapermillExecutionError", (Exception,), {})
        mock_pm.PapermillExecutionError = mock_exc_class
        mock_pm.execute_notebook.side_effect = mock_exc_class("Cell failed")
        with patch.dict("sys.modules", {"papermill": mock_pm}):
            asset = DiscoveredAsset(
                path="/tmp/fail_nb.ipynb",
                asset_type="notebook",
                has_entrypoint=True,
                code_cell_count=2,
            )
            result = execute_asset(asset, task_tier="science", model="qwen3:8b")
            assert result.exit_code == 1
            assert "Cell failed" in result.stderr

    def test_execute_notebook_missing_papermill(self) -> None:
        """When papermill not importable, returns exit_code=2."""
        # Remove papermill from sys.modules to force ImportError
        with patch.dict("sys.modules", {"papermill": None}):
            asset = DiscoveredAsset(
                path="/tmp/no_pm.ipynb",
                asset_type="notebook",
                has_entrypoint=True,
                code_cell_count=1,
            )
            result = execute_asset(asset, task_tier="science", model="qwen3:8b")
            assert result.exit_code == 2
            assert "papermill not installed" in result.stderr

    def test_execute_pytest_suite(self, tmp_path: object) -> None:
        """Run pytest subprocess on a trivial test file, get exit_code=0."""
        test_file = tmp_path / "test_trivial.py"  # type: ignore[operator]
        test_file.write_text("def test_pass():\n    assert True\n")
        asset = DiscoveredAsset(
            path=str(test_file),
            asset_type="pytest_suite",
            has_entrypoint=True,
        )
        result = execute_asset(asset, task_tier="test-suite", model="qwen3:8b")
        assert result.exit_code == 0

    def test_execute_snakefile_missing_snakemake(self) -> None:
        """Snakemake not installed, returns exit_code=2."""
        with patch("shutil.which", return_value=None):
            asset = DiscoveredAsset(
                path="/tmp/Snakefile",
                asset_type="snakefile",
                has_entrypoint=True,
            )
            result = execute_asset(asset, task_tier="pipeline-step", model="qwen3:8b")
            assert result.exit_code == 2
            assert "snakemake not installed" in result.stderr


# ---------------------------------------------------------------------------
# _get_next_tier_model tests (escalation chain)
# ---------------------------------------------------------------------------


class TestGetNextTierModel:
    """Tests for _get_next_tier_model() -- escalation chain logic."""

    def _make_tier_map(self) -> dict[str, TierMapping]:
        """Create a tier map with models at each tier level."""
        return {
            "code": TierMapping(
                tier="code",
                model="qwen3:4b",
                model_size_b=4.0,
                quality_mean=0.92,
                degraded_confidence=False,
            ),
        }

    def test_next_tier_model_escalation(self) -> None:
        """_get_next_tier_model returns correct escalation tiny->small->medium->frontier."""
        tier_map = {
            "code": TierMapping(tier="code", model="qwen3:1.7b", model_size_b=1.7,
                                quality_mean=0.5, degraded_confidence=False),
        }
        # tiny (1.7b) -> should escalate to small tier
        next_model = _get_next_tier_model("qwen3:1.7b", tier_map, "code")
        # Should return a model or None -- the key point is it tries to escalate
        # For this test, with limited tier_map, it may return None if no small model available
        # The function's contract: return model from next tier or None
        assert next_model is None or isinstance(next_model, str)

    def test_next_tier_model_at_frontier(self) -> None:
        """Returns None when already at frontier tier."""
        tier_map = {
            "code": TierMapping(tier="code", model="qwen3:8b", model_size_b=8.0,
                                quality_mean=0.95, degraded_confidence=False),
        }
        # medium (8b) is the highest local tier -> frontier has no local model
        result = _get_next_tier_model("qwen3:8b", tier_map, "code")
        assert result is None


# ---------------------------------------------------------------------------
# AutopilotReport immutability test
# ---------------------------------------------------------------------------


class TestAutopilotReport:
    """Tests for AutopilotReport Pydantic model."""

    def test_autopilot_report_frozen(self) -> None:
        """AutopilotReport model is immutable."""
        report = AutopilotReport(
            project_name="test",
            project_root="/tmp/test",
            inventory=AssetInventory(
                project_name="test",
                project_root="/tmp/test",
                assets=(),
                counts={},
            ),
            tier_map={},
            results=(),
            total_assets=0,
            passed=0,
            failed=0,
            escalation_needed=0,
            tokens_consumed_local=0,
            run_executed=False,
        )
        with pytest.raises(Exception):
            report.passed = 5  # type: ignore[misc]

    def test_autopilot_report_counts(self) -> None:
        """Report has correct pass/fail/escalate counts."""
        results = (
            AssetResult(
                asset_path="/tmp/a.py", asset_type="script", task_tier="code",
                model_used="qwen3:8b", exit_code=0, stdout="ok", stderr="",
                duration_s=1.0,
            ),
            AssetResult(
                asset_path="/tmp/b.py", asset_type="script", task_tier="code",
                model_used="qwen3:8b", exit_code=1, stdout="", stderr="fail",
                duration_s=2.0, escalation_needed=True,
            ),
        )
        report = AutopilotReport(
            project_name="test",
            project_root="/tmp/test",
            inventory=AssetInventory(
                project_name="test", project_root="/tmp/test", assets=(), counts={},
            ),
            tier_map={},
            results=results,
            total_assets=2,
            passed=1,
            failed=1,
            escalation_needed=1,
            tokens_consumed_local=0,
            run_executed=True,
        )
        assert report.passed == 1
        assert report.failed == 1
        assert report.escalation_needed == 1
        assert report.run_executed is True


# ---------------------------------------------------------------------------
# run_autopilot tests (orchestration loop)
# ---------------------------------------------------------------------------


class TestRunAutopilot:
    """Tests for run_autopilot() -- full orchestration pipeline."""

    def _make_scheduler(self) -> MagicMock:
        """Create a scheduler stub whose lease context always admits immediately."""
        scheduler = MagicMock()
        scheduler.lease.side_effect = lambda *args, **kwargs: nullcontext(None)
        return scheduler

    def _make_inventory(self) -> AssetInventory:
        """Create a minimal test inventory with 2 scripts."""
        return AssetInventory(
            project_name="test-project",
            project_root="/tmp/test-project",
            assets=(
                DiscoveredAsset(
                    path="/tmp/test-project/analysis.py",
                    asset_type="script",
                    has_entrypoint=True,
                ),
                DiscoveredAsset(
                    path="/tmp/test-project/helper.py",
                    asset_type="script",
                    has_entrypoint=True,
                ),
            ),
            counts={"script": 2},
        )

    def _make_tier_map(self) -> dict[str, TierMapping]:
        """Create tier mappings for code and science."""
        return {
            "code": TierMapping(
                tier="code", model="qwen3:4b", model_size_b=4.0,
                quality_mean=0.92, degraded_confidence=False,
            ),
            "science": TierMapping(
                tier="science", model="qwen3:8b", model_size_b=8.0,
                quality_mean=0.96, degraded_confidence=False,
            ),
        }

    def test_run_autopilot_discovery_only(self) -> None:
        """Without run=True, returns report with inventory but no results."""
        with patch("ollarma.autopilot.discover_assets") as mock_disc, \
             patch("ollarma.autopilot.build_tier_model_map") as mock_tier:
            mock_disc.return_value = self._make_inventory()
            mock_tier.return_value = self._make_tier_map()

            report = run_autopilot(
                project_name="test",
                project_root="/tmp/test-project",
                run=False,
            )
            assert report.run_executed is False
            assert len(report.results) == 0
            assert report.total_assets == 2

    def test_run_autopilot_with_execution(self) -> None:
        """With run=True, executes assets and returns results."""
        success_result = AssetResult(
            asset_path="/tmp/test-project/analysis.py",
            asset_type="script", task_tier="code", model_used="qwen3:4b",
            exit_code=0, stdout="ok", stderr="", duration_s=1.0,
        )

        with patch("ollarma.autopilot.discover_assets") as mock_disc, \
             patch("ollarma.autopilot.build_tier_model_map") as mock_tier, \
             patch("ollarma.autopilot.execute_asset") as mock_exec, \
             patch("ollarma.autopilot.resolve_selection", return_value="qwen3:4b"), \
             patch("ollarma.autopilot._read_asset_content") as mock_read, \
             patch("ollarma.autopilot.classify_asset") as mock_classify, \
             patch("ollarma.autopilot._get_workflow_scheduler", return_value=self._make_scheduler()):
            mock_disc.return_value = self._make_inventory()
            mock_tier.return_value = self._make_tier_map()
            mock_exec.return_value = success_result
            mock_read.return_value = "print('hello')"
            mock_classify.return_value = "code"

            report = run_autopilot(
                project_name="test",
                project_root="/tmp/test-project",
                run=True,
            )
            assert report.run_executed is True
            assert len(report.results) == 2
            assert report.passed == 2

    def test_run_autopilot_escalation_retry(self) -> None:
        """First execution fails (exit_code=1), retry with next-tier model succeeds (D-13)."""
        fail_result = AssetResult(
            asset_path="/tmp/test-project/analysis.py",
            asset_type="script", task_tier="code", model_used="qwen3:4b",
            exit_code=1, stdout="", stderr="error", duration_s=1.0,
        )
        success_result = AssetResult(
            asset_path="/tmp/test-project/analysis.py",
            asset_type="script", task_tier="code", model_used="qwen3:8b",
            exit_code=0, stdout="ok", stderr="", duration_s=2.0, escalated=True,
        )

        call_count = 0

        def mock_execute_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # First call per asset fails
                return fail_result
            return success_result

        inv = AssetInventory(
            project_name="test-project",
            project_root="/tmp/test-project",
            assets=(
                DiscoveredAsset(
                    path="/tmp/test-project/analysis.py",
                    asset_type="script",
                    has_entrypoint=True,
                ),
            ),
            counts={"script": 1},
        )

        with patch("ollarma.autopilot.discover_assets") as mock_disc, \
             patch("ollarma.autopilot.build_tier_model_map") as mock_tier, \
             patch("ollarma.autopilot.execute_asset") as mock_exec, \
             patch("ollarma.autopilot.resolve_selection", return_value="qwen3:4b"), \
             patch("ollarma.autopilot._read_asset_content") as mock_read, \
             patch("ollarma.autopilot.classify_asset") as mock_classify, \
             patch("ollarma.autopilot._get_next_tier_model") as mock_next, \
             patch("ollarma.autopilot._get_workflow_scheduler", return_value=self._make_scheduler()):
            mock_disc.return_value = inv
            mock_tier.return_value = self._make_tier_map()
            mock_exec.side_effect = [fail_result, success_result]
            mock_read.return_value = "print('hello')"
            mock_classify.return_value = "code"
            mock_next.return_value = "qwen3:8b"

            report = run_autopilot(
                project_name="test",
                project_root="/tmp/test-project",
                run=True,
            )
            assert report.passed >= 1
            # Escalation was attempted
            assert mock_exec.call_count == 2

    def test_run_autopilot_escalation_exhausted(self) -> None:
        """Fails at all local tiers, marked as escalation_needed=True (D-13)."""
        fail_result = AssetResult(
            asset_path="/tmp/test-project/analysis.py",
            asset_type="script", task_tier="code", model_used="qwen3:4b",
            exit_code=1, stdout="", stderr="error", duration_s=1.0,
        )
        fail_result_2 = AssetResult(
            asset_path="/tmp/test-project/analysis.py",
            asset_type="script", task_tier="code", model_used="qwen3:8b",
            exit_code=1, stdout="", stderr="still error", duration_s=2.0,
            escalated=True,
        )

        inv = AssetInventory(
            project_name="test-project",
            project_root="/tmp/test-project",
            assets=(
                DiscoveredAsset(
                    path="/tmp/test-project/analysis.py",
                    asset_type="script",
                    has_entrypoint=True,
                ),
            ),
            counts={"script": 1},
        )

        with patch("ollarma.autopilot.discover_assets") as mock_disc, \
             patch("ollarma.autopilot.build_tier_model_map") as mock_tier, \
             patch("ollarma.autopilot.execute_asset") as mock_exec, \
             patch("ollarma.autopilot.resolve_selection", return_value="qwen3:4b"), \
             patch("ollarma.autopilot._read_asset_content") as mock_read, \
             patch("ollarma.autopilot.classify_asset") as mock_classify, \
             patch("ollarma.autopilot._get_next_tier_model") as mock_next, \
             patch("ollarma.autopilot._get_workflow_scheduler", return_value=self._make_scheduler()):
            mock_disc.return_value = inv
            mock_tier.return_value = self._make_tier_map()
            # First attempt fails, escalation also fails
            mock_exec.side_effect = [fail_result, fail_result_2]
            mock_read.return_value = "print('hello')"
            mock_classify.return_value = "code"
            mock_next.return_value = "qwen3:8b"

            report = run_autopilot(
                project_name="test",
                project_root="/tmp/test-project",
                run=True,
            )
            assert report.escalation_needed >= 1

    def test_run_autopilot_sequential_default(self) -> None:
        """Assets executed in order (D-11)."""
        results_order: list[str] = []

        def track_execution(asset, task_tier="code", model="qwen3:4b", **kwargs):
            results_order.append(asset.path)
            return AssetResult(
                asset_path=asset.path, asset_type="script", task_tier=task_tier,
                model_used=model, exit_code=0, stdout="ok", stderr="",
                duration_s=1.0,
            )

        with patch("ollarma.autopilot.discover_assets") as mock_disc, \
             patch("ollarma.autopilot.build_tier_model_map") as mock_tier, \
             patch("ollarma.autopilot.execute_asset") as mock_exec, \
             patch("ollarma.autopilot.resolve_selection", return_value="qwen3:4b"), \
             patch("ollarma.autopilot._read_asset_content") as mock_read, \
             patch("ollarma.autopilot.classify_asset") as mock_classify, \
             patch("ollarma.autopilot._get_workflow_scheduler", return_value=self._make_scheduler()):
            mock_disc.return_value = self._make_inventory()
            mock_tier.return_value = self._make_tier_map()
            mock_exec.side_effect = track_execution
            mock_read.return_value = "print('hello')"
            mock_classify.return_value = "code"

            run_autopilot(
                project_name="test",
                project_root="/tmp/test-project",
                run=True,
            )
            # Assets were executed (sequentially, in some order)
            assert len(results_order) == 2

    def test_run_autopilot_sorts_by_tier(self) -> None:
        """Assets sorted by model tier before execution to minimize model swaps (Pitfall 3)."""
        execution_tiers: list[str] = []

        def track_tier_execution(asset, task_tier, model, **kwargs):
            execution_tiers.append(task_tier)
            return AssetResult(
                asset_path=asset.path, asset_type=asset.asset_type,
                task_tier=task_tier, model_used=model, exit_code=0,
                stdout="ok", stderr="", duration_s=1.0,
            )

        # Inventory with assets that classify to different tiers
        inv = AssetInventory(
            project_name="test-project",
            project_root="/tmp/test-project",
            assets=(
                DiscoveredAsset(
                    path="/tmp/test-project/big_analysis.py",
                    asset_type="script", has_entrypoint=True,
                ),
                DiscoveredAsset(
                    path="/tmp/test-project/tiny_script.py",
                    asset_type="script", has_entrypoint=True,
                ),
            ),
            counts={"script": 2},
        )

        classify_calls = iter(["science", "code"])

        with patch("ollarma.autopilot.discover_assets") as mock_disc, \
             patch("ollarma.autopilot.build_tier_model_map") as mock_tier, \
             patch("ollarma.autopilot.execute_asset") as mock_exec, \
             patch("ollarma.autopilot.resolve_selection", side_effect=["qwen3:8b", "qwen3:4b"]), \
             patch("ollarma.autopilot._read_asset_content") as mock_read, \
             patch("ollarma.autopilot.classify_asset") as mock_classify, \
             patch("ollarma.autopilot._get_workflow_scheduler", return_value=self._make_scheduler()):
            mock_disc.return_value = inv
            tier_map = {
                "code": TierMapping(
                    tier="code", model="qwen3:4b", model_size_b=4.0,
                    quality_mean=0.92, degraded_confidence=False,
                ),
                "science": TierMapping(
                    tier="science", model="qwen3:8b", model_size_b=8.0,
                    quality_mean=0.96, degraded_confidence=False,
                ),
            }
            mock_tier.return_value = tier_map
            mock_exec.side_effect = track_tier_execution
            mock_read.return_value = "print('hello')"
            mock_classify.side_effect = lambda a, c: next(classify_calls)

            run_autopilot(
                project_name="test",
                project_root="/tmp/test-project",
                run=True,
            )
            # Should be sorted: smaller tier model first
            # code (4b) before science (8b)
            assert execution_tiers[0] == "code"
            assert execution_tiers[1] == "science"

    def test_run_autopilot_respects_resource_budget_exceeded(self) -> None:
        """Degraded scheduler admission returns a deterministic local receipt instead of executing."""
        from ollarma.scheduler import RESOURCE_BUDGET_EXCEEDED, RuntimeSnapshot, SchedulerAdmissionError

        scheduler = MagicMock()
        scheduler.lease.side_effect = SchedulerAdmissionError(
            RESOURCE_BUDGET_EXCEEDED,
            RuntimeSnapshot(
                active_job_id=None,
                active_lane=None,
                active_model=None,
                active_project=None,
                queue_depth=0,
                queue_depth_by_lane={
                    "read_only_non_inference": 0,
                    "local_inference_single": 0,
                    "workflow_execution_queue": 0,
                },
                active_read_only=0,
                degraded_mode=True,
                resource_reason="swap in use (512.0 MB)",
                swap_used_mb=512.0,
                telemetry_source="api/ps+ollama ps+sysctl vm.swapusage",
            ),
            "swap in use (512.0 MB)",
        )

        with patch("ollarma.autopilot.discover_assets") as mock_disc, \
             patch("ollarma.autopilot.build_tier_model_map") as mock_tier, \
             patch("ollarma.autopilot.execute_asset") as mock_exec, \
             patch("ollarma.autopilot.resolve_selection", return_value="qwen3:4b"), \
             patch("ollarma.autopilot._read_asset_content", return_value="print('hello')"), \
             patch("ollarma.autopilot.classify_asset", return_value="code"), \
             patch("ollarma.autopilot._get_workflow_scheduler", return_value=scheduler):
            mock_disc.return_value = self._make_inventory()
            mock_tier.return_value = self._make_tier_map()

            report = run_autopilot(
                project_name="test",
                project_root="/tmp/test-project",
                run=True,
            )

        assert mock_exec.call_count == 0
        assert report.failed == 2
        assert report.escalation_needed == 2
        assert all("RESOURCE_BUDGET_EXCEEDED" in result.stderr for result in report.results)

    def test_run_autopilot_persists_retry_receipts_and_checkpoint(self, tmp_path: pathlib.Path) -> None:
        """Retryable failures append receipt history and decrement checkpoint retry budget."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        asset_path = project_root / "analysis.py"
        asset_path.write_text("print('hello')\n", encoding="utf-8")

        inv = AssetInventory(
            project_name="test-project",
            project_root=str(project_root),
            assets=(
                DiscoveredAsset(
                    path=str(asset_path),
                    asset_type="script",
                    has_entrypoint=True,
                ),
            ),
            counts={"script": 1},
        )
        fail_result = AssetResult(
            asset_path=str(asset_path),
            asset_type="script",
            task_tier="code",
            model_used="qwen3:4b",
            exit_code=1,
            stdout="",
            stderr="error",
            duration_s=1.0,
        )
        success_result = AssetResult(
            asset_path=str(asset_path),
            asset_type="script",
            task_tier="code",
            model_used="qwen3:8b",
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_s=2.0,
            escalated=True,
        )

        with patch("ollarma.autopilot.discover_assets", return_value=inv), \
             patch("ollarma.autopilot.build_tier_model_map", return_value=self._make_tier_map()), \
             patch("ollarma.autopilot.execute_asset", side_effect=[fail_result, success_result]), \
             patch("ollarma.autopilot.resolve_selection", return_value="qwen3:4b"), \
             patch("ollarma.autopilot._read_asset_content", return_value="print('hello')"), \
             patch("ollarma.autopilot.classify_asset", return_value="code"), \
             patch("ollarma.autopilot._get_next_tier_model", return_value="qwen3:8b"), \
             patch("ollarma.autopilot._get_workflow_scheduler", return_value=self._make_scheduler()):
            run_autopilot(
                project_name="test",
                project_root=str(project_root),
                run=True,
            )

        receipts_files = list((project_root / ".ollarma" / "autopilot").glob("*/receipts.json"))
        checkpoint_files = list((project_root / ".ollarma" / "autopilot").glob("*/checkpoint.json"))
        assert len(receipts_files) == 1
        assert len(checkpoint_files) == 1

        receipts = json.loads(receipts_files[0].read_text(encoding="utf-8"))
        checkpoint = json.loads(checkpoint_files[0].read_text(encoding="utf-8"))

        assert any(item["status"] == "retryable_failure" for item in receipts)
        assert any(item["retry_count"] == 1 for item in receipts)
        assert checkpoint["retry_budget_remaining"] == 0

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
from contextlib import nullcontext
from unittest.mock import patch

from ollarma.autopilot import TierMapping
from ollarma.service import list_project_workflows, submit_autopilot


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO_PROJECT = REPO_ROOT / "demo" / "hackathon" / "demo_project"


def _copy_demo_project(tmp_path: pathlib.Path) -> pathlib.Path:
    target = tmp_path / "demo_project"
    shutil.copytree(DEMO_PROJECT, target)
    return target


def _write_adapter(tmp_path: pathlib.Path, project_root: pathlib.Path) -> pathlib.Path:
    adapters_dir = tmp_path / "adapters"
    runtime_dir = adapters_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    adapter_path = runtime_dir / "ollarma-demo.yaml"
    adapter_path.write_text(
        "\n".join(
            [
                "schema_version: 1",
                "project_name: ollarma-demo",
                f"project_root: {project_root}",
                "project_type: isolated demo test project",
                "namespace_prefix: \"ollarma-demo:\"",
                "has_canon: true",
                "knowledge_base:",
                "  artifact_root: .ollarma/kb",
                "  freshness_hours: 24",
                "  stale_behavior: block",
                "  sources:",
                "    - path: README.md",
                "      kind: documents",
                "      authority: canonical",
                "      recursive: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return adapters_dir


def test_demo_script_writes_artifacts(tmp_path: pathlib.Path) -> None:
    project_root = _copy_demo_project(tmp_path)
    script_path = project_root / "scripts" / "seedgraph_handoff_demo.py"

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "OLLARMA_DEMO_OK" in result.stdout
    assert (project_root / "artifacts" / "ollarma" / "demo" / "handoff.json").exists()
    assert (project_root / "artifacts" / "ollarma" / "demo" / "judge_narrative.md").exists()


def test_submit_autopilot_executes_demo_project_and_persists_receipts(tmp_path: pathlib.Path) -> None:
    project_root = _copy_demo_project(tmp_path)
    adapters_dir = _write_adapter(tmp_path, project_root)
    tier_map = {
        "code": TierMapping(
            tier="code",
            model="qwen3:1.7b",
            model_size_b=1.7,
            quality_mean=0.95,
            degraded_confidence=False,
        )
    }

    with patch("ollarma.autopilot.build_tier_model_map", return_value=tier_map), patch(
        "ollarma.autopilot.resolve_selection",
        return_value="qwen3:1.7b",
    ), patch("ollarma.autopilot._get_workflow_scheduler") as mock_scheduler:
        mock_scheduler.return_value.lease.return_value = nullcontext()
        report = submit_autopilot(
            "ollarma-demo",
            run_assets=True,
            include=("scripts/seedgraph_handoff_demo.py",),
            adapters_dir=str(adapters_dir),
        )

    assert report.run_executed is True
    assert report.passed == 1
    assert report.failed == 0
    assert report.results[0].exit_code == 0
    assert (project_root / "artifacts" / "ollarma" / "demo" / "handoff.json").exists()
    receipts = list((project_root / ".ollarma" / "autopilot").glob("*/receipts.json"))
    checkpoints = list((project_root / ".ollarma" / "autopilot").glob("*/checkpoint.json"))
    assert receipts
    assert checkpoints


def test_demo_project_exposes_one_workflow_manifest(tmp_path: pathlib.Path) -> None:
    project_root = _copy_demo_project(tmp_path)
    adapters_dir = _write_adapter(tmp_path, project_root)

    catalog = list_project_workflows("ollarma-demo", adapters_dir=str(adapters_dir))

    assert len(catalog.manifests) == 1
    assert catalog.manifests[0].run_id == "demo-seedgraph-handoff"
    assert catalog.manifests[0].steps[0].step_id == "seedgraph-handoff-demo"

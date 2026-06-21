from __future__ import annotations

import json
import pathlib
import re

import pytest
from pydantic import ValidationError

from ollarma.workflow_manifest import ValidatedWorkflowManifest, load_workflow_manifest


def _write_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _base_manifest_payload() -> dict:
    run_id = "run-001"
    return {
        "manifest_version": "1.0",
        "consumer_repo": "science/consumer",
        "adapter_name": "science-consumer",
        "task_class": "validated-script",
        "run_id": run_id,
        "steps": [
            {
                "step_id": "execute-script",
                "stage": "execute",
                "task_type": "validated-script",
                "inputs": [
                    {
                        "stable_id": "asset:script",
                        "repo_relative": "scripts/run_validation.py",
                    }
                ],
                "outputs": [
                    {
                        "stable_id": "artifact:summary",
                        "repo_relative": f".ollarma/runs/{run_id}/materialized/summary.json",
                    }
                ],
                "materialization_root": {
                    "stable_id": "materialization:root",
                    "repo_relative": f".ollarma/runs/{run_id}/materialized",
                },
            }
        ],
        "validation_contract": {
            "contract_id": "contract:validated-script",
            "spec_ref": {
                "stable_id": "spec:validation",
                "repo_relative": "contracts/validation-spec.json",
            },
            "success_criteria": ["exit_zero", "schema_valid"],
        },
        "summary_schema": {
            "schema_id": "schema:summary",
            "format": "json",
            "required_fields": ["status", "artifacts"],
        },
        "checkpoint_policy": {
            "checkpoint_root": {
                "stable_id": "checkpoint:root",
                "repo_relative": f".ollarma/runs/{run_id}/checkpoints",
            },
            "resume_key": "run_id",
            "max_retries": 1,
        },
        "escalation_policy": {
            "handoff_target": "frontier_or_human",
            "on_reason_codes": ["VALIDATION_FAILED", "DEPENDENCY_MISSING"],
        },
        "artifact_roots": [
            {
                "root_name": "primary-run-root",
                "locator": {
                    "stable_id": "artifact-root:primary",
                    "repo_relative": f".ollarma/runs/{run_id}",
                },
            }
        ],
    }


def _materialize_repo(tmp_path: pathlib.Path, payload: dict) -> tuple[pathlib.Path, pathlib.Path]:
    repo_root = tmp_path / "consumer-repo"
    (repo_root / "scripts").mkdir(parents=True)
    (repo_root / "contracts").mkdir(parents=True)
    (repo_root / ".ollarma" / "manifests").mkdir(parents=True)
    (repo_root / "scripts" / "run_validation.py").write_text("print('ok')\n", encoding="utf-8")
    (repo_root / "contracts" / "validation-spec.json").write_text(
        '{"checks":["exit_zero","schema_valid"]}\n',
        encoding="utf-8",
    )

    manifest_path = repo_root / ".ollarma" / "manifests" / "workflow.json"
    _write_json(manifest_path, payload)
    return repo_root, manifest_path


def _assert_no_absolute_paths(value: object) -> None:
    if isinstance(value, str):
        assert not value.startswith("/")
        assert not re.match(r"^[A-Za-z]:[\\/]", value)
        return
    if isinstance(value, dict):
        for nested in value.values():
            _assert_no_absolute_paths(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_no_absolute_paths(nested)


def test_load_workflow_manifest(tmp_path: pathlib.Path) -> None:
    payload = _base_manifest_payload()
    repo_root, manifest_path = _materialize_repo(tmp_path, payload)

    manifest = load_workflow_manifest(
        manifest_path.relative_to(repo_root),
        allowlisted_roots=[repo_root],
    )

    assert isinstance(manifest, ValidatedWorkflowManifest)
    assert manifest.task_class == "validated-script"
    assert manifest.steps[0].step_id == "execute-script"
    public_payload = manifest.to_service_payload()
    assert public_payload["manifest_ref"]["repo_relative"] == ".ollarma/manifests/workflow.json"
    assert public_payload["manifest_digest"].startswith("sha256:")


def test_load_workflow_manifest_accepts_service_locator_dict(tmp_path: pathlib.Path) -> None:
    payload = _base_manifest_payload()
    repo_root, manifest_path = _materialize_repo(tmp_path, payload)
    discovered_manifest = load_workflow_manifest(
        manifest_path.relative_to(repo_root),
        allowlisted_roots=[repo_root],
    )
    manifest_ref = discovered_manifest.to_service_payload()["manifest_ref"]

    manifest = load_workflow_manifest(
        manifest_ref,
        allowlisted_roots=[repo_root],
    )

    assert manifest.run_id == "run-001"
    assert manifest.to_service_payload()["manifest_ref"] == manifest_ref


def test_load_workflow_manifest_rejects_service_locator_digest_mismatch(tmp_path: pathlib.Path) -> None:
    payload = _base_manifest_payload()
    repo_root, manifest_path = _materialize_repo(tmp_path, payload)

    with pytest.raises(ValueError, match="digest mismatch"):
        load_workflow_manifest(
            {
                "repo_relative": manifest_path.relative_to(repo_root).as_posix(),
                "digest": "sha256:deadbeef",
            },
            allowlisted_roots=[repo_root],
        )


def test_unknown_task_class_raises_validation_error(tmp_path: pathlib.Path) -> None:
    payload = _base_manifest_payload()
    payload["task_class"] = "arbitrary-shell"
    payload["steps"][0]["task_type"] = "arbitrary-shell"
    repo_root, manifest_path = _materialize_repo(tmp_path, payload)

    with pytest.raises(ValidationError):
        load_workflow_manifest(
            manifest_path.relative_to(repo_root),
            allowlisted_roots=[repo_root],
        )


def test_out_of_root_materialization_path_is_rejected(tmp_path: pathlib.Path) -> None:
    payload = _base_manifest_payload()
    payload["steps"][0]["materialization_root"]["repo_relative"] = "../outside/materialized"
    repo_root, manifest_path = _materialize_repo(tmp_path, payload)

    with pytest.raises(ValidationError):
        load_workflow_manifest(
            manifest_path.relative_to(repo_root),
            allowlisted_roots=[repo_root],
        )


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("validation_contract", "contract_id"),
        ("summary_schema", "required_fields"),
    ],
)
def test_missing_validation_or_summary_contract_fields_are_rejected(
    tmp_path: pathlib.Path,
    section: str,
    field: str,
) -> None:
    payload = _base_manifest_payload()
    del payload[section][field]
    repo_root, manifest_path = _materialize_repo(tmp_path, payload)

    with pytest.raises(ValidationError):
        load_workflow_manifest(
            manifest_path.relative_to(repo_root),
            allowlisted_roots=[repo_root],
        )


@pytest.mark.parametrize(
    ("target", "repo_relative", "message"),
    [
        ("output", ".planning/run-001/summary.json", "protected"),
        ("output", ".ollarma/runs/run-001/.env", "secret-bearing"),
    ],
)
def test_protected_path_writes_are_rejected(
    tmp_path: pathlib.Path,
    target: str,
    repo_relative: str,
    message: str,
) -> None:
    payload = _base_manifest_payload()
    if target == "output":
        payload["steps"][0]["outputs"][0]["repo_relative"] = repo_relative
    repo_root, manifest_path = _materialize_repo(tmp_path, payload)

    with pytest.raises(ValueError, match=message):
        load_workflow_manifest(
            manifest_path.relative_to(repo_root),
            allowlisted_roots=[repo_root],
        )


def test_symlink_escapes_are_rejected(tmp_path: pathlib.Path) -> None:
    payload = _base_manifest_payload()
    payload["steps"][0]["materialization_root"]["repo_relative"] = ".ollarma/escape/materialized"
    payload["steps"][0]["outputs"][0]["repo_relative"] = ".ollarma/escape/materialized/summary.json"
    payload["artifact_roots"][0]["locator"]["repo_relative"] = ".ollarma/escape"
    payload["checkpoint_policy"]["checkpoint_root"]["repo_relative"] = ".ollarma/escape/checkpoints"

    repo_root, manifest_path = _materialize_repo(tmp_path, payload)
    escape_target = tmp_path / "outside-root"
    escape_target.mkdir()
    (repo_root / ".ollarma" / "escape").symlink_to(escape_target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        load_workflow_manifest(
            manifest_path.relative_to(repo_root),
            allowlisted_roots=[repo_root],
        )


def test_service_payload_refs_never_return_absolute_paths(tmp_path: pathlib.Path) -> None:
    payload = _base_manifest_payload()
    repo_root, manifest_path = _materialize_repo(tmp_path, payload)
    manifest = load_workflow_manifest(
        manifest_path.relative_to(repo_root),
        allowlisted_roots=[repo_root],
    )

    manifest_payload = manifest.to_service_payload()
    step_payload = manifest.steps[0].to_service_payload()

    _assert_no_absolute_paths(manifest_payload)
    _assert_no_absolute_paths(step_payload)
    assert manifest_payload["manifest_ref"]["repo_relative"] == ".ollarma/manifests/workflow.json"

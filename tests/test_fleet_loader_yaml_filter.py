"""F-08 — fleet loader YAML filter.

Proves that `load_fleet_registry` silently ignores non-adapter markdown files
(pointers, gate notes) mixed into the adapter directory, and continues to
load valid YAML adapters in `runtime/`.

The failure mode this closes: `UserWarning: Skipping <name>.md: no
project_name found` emitted on every `ollarma projects` invocation, which
trains operators to filter out warnings.
"""
from __future__ import annotations

import warnings

import pytest

from ollarma.fleet import load_fleet_registry


def test_fleet_loader_ignores_md_files(tmp_path) -> None:
    """Non-adapter markdown files must NOT emit a warning (F-08 fix)."""
    # Simulate the real directory shape: a pointer and a gate note.
    (tmp_path / "INSIGHT_006_MASTER_PLAN_POINTER.md").write_text(
        "# Master Plan Pointer\n\nThis file is intentional mixed content.\n",
        encoding="utf-8",
    )
    (tmp_path / "cellico-bio-INSIGHT_006_GATE.md").write_text(
        "# Gate note\n\nNot an adapter.\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        registry = load_fleet_registry(str(tmp_path))

    # No "Skipping ...: no project_name found" UserWarnings from the loader.
    project_name_warnings = [
        w for w in caught
        if issubclass(w.category, UserWarning)
        and "no project_name found" in str(w.message)
    ]
    assert project_name_warnings == [], (
        f"expected zero project_name warnings, got {project_name_warnings}"
    )
    # Registry contains no adapters (all inputs were non-adapter markdown).
    assert registry == {}


def test_fleet_loader_still_loads_yaml(tmp_path) -> None:
    """A valid YAML adapter in runtime/ must still be loaded."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    yaml_text = (
        "project_name: example-proj\n"
        "project_root: /tmp/example-proj\n"
        "project_type: library\n"
        "has_canon: true\n"
    )
    (runtime_dir / "example-proj.yaml").write_text(yaml_text, encoding="utf-8")

    # A second file with a non-YAML extension in runtime/ must be ignored.
    (runtime_dir / "NOTES.md").write_text("# stray notes\n", encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        registry = load_fleet_registry(str(tmp_path))

    assert "example-proj" in registry
    cfg = registry["example-proj"]
    assert cfg.project_root == "/tmp/example-proj"
    assert cfg.adapter_source == "yaml"
    assert cfg.has_canon is True

    # No warnings triggered by the stray .md in runtime/ (extension filter).
    stray_md_warnings = [
        w for w in caught
        if issubclass(w.category, UserWarning)
        and "NOTES.md" in str(w.message)
    ]
    assert stray_md_warnings == [], (
        f"expected zero warnings for stray .md in runtime/, got {stray_md_warnings}"
    )

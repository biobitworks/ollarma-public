"""Regression tests for tf-cellico service-mode routing."""
from __future__ import annotations

from ollarma.fleet_tools import build_tool_registry
from ollarma.service import list_projects, resolve_project_adapter


def test_tf_cellico_is_registered_for_service_mode():
    registry = list_projects(service_mode=True)

    assert "tf-cellico" in registry
    adapter = registry["tf-cellico"]
    assert adapter.project_root == "<repo>"
    assert adapter.adapter_source == "yaml"


def test_tf_cellico_service_mode_tool_surface_stays_read_only():
    adapter = resolve_project_adapter("tf-cellico", service_mode=True)

    registry, definitions = build_tool_registry(
        adapter.project_root,
        adapter=adapter,
        service_mode=True,
    )

    assert set(registry) == {"read_file", "grep_search", "skill_invoke"}
    definition_names = {definition["function"]["name"] for definition in definitions}
    assert definition_names == {"read_file", "grep_search", "skill_invoke"}

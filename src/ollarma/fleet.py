"""fleet.py -- Fleet registry core module for multi-project adapter management.

Provides the AdapterConfig Pydantic model with dual-format parsing (Markdown + YAML),
a fleet registry loader that scans an adapters directory, and a project resolver
with exact, case-insensitive, and substring matching.

Exports:
    AdapterConfig       -- Pydantic model for adapter configuration
    parse_markdown_adapter -- Extract adapter fields from Markdown text
    parse_yaml_adapter     -- Extract adapter fields from YAML text
    load_fleet_registry    -- Scan adapters directory and return registry dict
    resolve_project        -- Look up a project by name in the registry
    DEFAULT_ADAPTERS_DIR   -- Default path to the adapters directory
"""
from __future__ import annotations

import logging
import pathlib
import re
import warnings
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from ollarma.antibody_profile import AntibodyProfile
from ollarma.discovery import DEFAULT_ADAPTERS_DIR as _DISCOVERY_DEFAULT
from ollarma.kb_contract import DatabaseConfig, KnowledgeBaseConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Backward-compatible export -- centralised in discovery.py
DEFAULT_ADAPTERS_DIR = _DISCOVERY_DEFAULT


# ---------------------------------------------------------------------------
# AdapterConfig model
# ---------------------------------------------------------------------------


class AdapterConfig(BaseModel):
    """Configuration for a project adapter.

    Validates that project_root is an absolute path. Provides a ``root_exists``
    property that checks whether the path actually exists on disk.
    """

    project_name: str
    project_root: str  # Must be absolute
    project_type: str = ""
    namespace_prefix: str = ""
    # Governance classification (see _infer_governance / _check_governance below).
    #   classification: is this a scientific-claim surface at all?
    #   canon_status:   present  -> governance CANON.md on disk, wired (has_canon=True)
    #                   pending  -> science repo, CANON not yet authored (reference-only)
    #                   not_applicable -> non-science ("other") repo, no CANON needed
    # ollarma is a hub, so EVERY repo may connect; CANON only governs the science layer.
    classification: Literal["science", "other"] = "other"
    canon_status: Literal["present", "pending", "not_applicable"] = "not_applicable"
    has_canon: bool = False
    env_type: str = "standard"
    snakemake: bool = False
    databases: list[DatabaseConfig] = Field(default_factory=list)
    mcps: list[dict] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    antibodies: list[str] = Field(default_factory=list)
    antibody_profile: AntibodyProfile | None = None
    surfaces: dict = Field(default_factory=dict)
    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)
    adapter_source: str = ""  # "markdown" or "yaml" -- provenance

    @field_validator("project_root")
    @classmethod
    def _validate_absolute_path(cls, v: str) -> str:
        """Enforce that project_root is an absolute path."""
        if not v.startswith("/"):
            raise ValueError(
                f"project_root must be an absolute path (starts with /), got: {v!r}"
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _infer_governance(cls, data: object) -> object:
        """Back-compat: infer classification/canon_status from has_canon when absent.

        Adapters authored before the classification model only set ``has_canon``.
        A repo with ``has_canon: true`` is treated as ``science``/``present``;
        one without as ``other``/``not_applicable``. Adapters that declare the
        fields explicitly are left untouched.
        """
        if not isinstance(data, dict):
            return data
        has_canon = bool(data.get("has_canon", False))
        if not data.get("classification"):
            data["classification"] = "science" if has_canon else "other"
        if not data.get("canon_status"):
            if data["classification"] == "science":
                data["canon_status"] = "present" if has_canon else "pending"
            else:
                data["canon_status"] = "not_applicable"
        return data

    @model_validator(mode="after")
    def _check_governance(self) -> "AdapterConfig":
        """Enforce the science/other ↔ canon_status ↔ has_canon invariants."""
        c, s, h = self.classification, self.canon_status, self.has_canon
        if c == "other":
            if s != "not_applicable":
                raise ValueError(
                    f"classification=other requires canon_status=not_applicable, got {s!r}"
                )
            if h:
                raise ValueError("classification=other cannot set has_canon=true")
        else:  # science
            if s == "not_applicable":
                raise ValueError(
                    "classification=science requires canon_status present|pending"
                )
            if s == "present" and not h:
                raise ValueError("canon_status=present requires has_canon=true")
            if s == "pending" and h:
                raise ValueError("canon_status=pending requires has_canon=false")
        return self

    @property
    def root_exists(self) -> bool:
        """Return True if the project_root directory exists on disk."""
        return pathlib.Path(self.project_root).exists()


# ---------------------------------------------------------------------------
# Markdown adapter parser
# ---------------------------------------------------------------------------

# Compiled regex patterns for Markdown adapter extraction
_RE_PROJECT_NAME = re.compile(r"^#\s+Adapter:\s*(.+)$", re.MULTILINE)
_RE_PROJECT_ROOT = re.compile(r"\*\*Path:\*\*\s*(.+)$", re.MULTILINE)
_RE_PROJECT_TYPE = re.compile(r"\*\*Type:\*\*\s*(.+)$", re.MULTILINE)
_RE_NAMESPACE_PREFIX = re.compile(r"\*\*EXP namespace prefix:\*\*\s*`([^`]+)`")
_RE_HAS_CANON = re.compile(r"^has_canon:\s*(true|false)\s*$", re.MULTILINE)
_RE_CLASSIFICATION = re.compile(r"^classification:\s*(science|other)\s*$", re.MULTILINE)
_RE_CANON_STATUS = re.compile(
    r"^canon_status:\s*(present|pending|not_applicable)\s*$", re.MULTILINE
)


def parse_markdown_adapter(text: str) -> dict:
    """Extract adapter fields from Markdown text.

    Returns a dict of found fields. Missing fields are omitted (not defaulted).
    Returns empty dict for empty or unparseable text -- never raises.
    """
    if not text or not text.strip():
        return {}

    result: dict = {}

    m = _RE_PROJECT_NAME.search(text)
    if m:
        result["project_name"] = m.group(1).strip()

    m = _RE_PROJECT_ROOT.search(text)
    if m:
        # Strip trailing slash for consistency
        root = m.group(1).strip().rstrip("/")
        result["project_root"] = root

    m = _RE_PROJECT_TYPE.search(text)
    if m:
        result["project_type"] = m.group(1).strip()

    m = _RE_NAMESPACE_PREFIX.search(text)
    if m:
        result["namespace_prefix"] = m.group(1).strip()

    m = _RE_HAS_CANON.search(text)
    if m:
        result["has_canon"] = m.group(1) == "true"

    m = _RE_CLASSIFICATION.search(text)
    if m:
        result["classification"] = m.group(1)

    m = _RE_CANON_STATUS.search(text)
    if m:
        result["canon_status"] = m.group(1)

    return result


# ---------------------------------------------------------------------------
# YAML adapter parser
# ---------------------------------------------------------------------------


def parse_yaml_adapter(text: str) -> dict:
    """Parse YAML adapter text into a dict.

    Returns the parsed dict, or empty dict on error -- never raises.
    """
    if not text or not text.strip():
        return {}

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}

    if not isinstance(data, dict):
        return {}

    return data


# ---------------------------------------------------------------------------
# Fleet registry loader
# ---------------------------------------------------------------------------


def load_fleet_registry(adapters_dir: str) -> dict[str, AdapterConfig]:
    """Scan *adapters_dir* for adapter files and return a registry dict.

    Scans for:
    - ``*.md`` markdown adapters whose first line is the ``# Adapter: <name>``
      header. Other ``.md`` files (pointers, gate notes, README.md, etc.) are
      ignored silently — they are legitimate mixed-in documentation rather
      than malformed adapters (F-08).
    - ``runtime/*.yaml`` and ``runtime/*.yml`` files in a ``runtime``
      subdirectory. Non-YAML files in ``runtime/`` are ignored silently.

    Each candidate file is parsed with the appropriate parser and wrapped in
    an AdapterConfig. Entries that fail Pydantic validation still emit a
    warning (a genuinely broken adapter is actionable).

    Returns a dict keyed by project_name.
    """
    registry: dict[str, AdapterConfig] = {}
    base = pathlib.Path(adapters_dir)

    # Scan top-level .md adapters (F-08: silently ignore non-adapter markdown).
    for md_file in sorted(base.glob("*.md")):
        if md_file.name.lower() == "readme.md":
            continue
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.warn(
                f"Skipping {md_file.name}: could not read file: {exc}",
                UserWarning,
                stacklevel=2,
            )
            continue

        fields = parse_markdown_adapter(text)
        if not fields.get("project_name"):
            # Not an adapter markdown document (pointer, gate note, etc.).
            # Treat as intentional mixed content, not an error.
            continue

        fields["adapter_source"] = "markdown"
        try:
            cfg = AdapterConfig(**fields)
            registry[cfg.project_name] = cfg
        except (TypeError, ValueError) as exc:
            warnings.warn(
                f"Skipping {md_file.name}: validation error: {exc}",
                UserWarning,
                stacklevel=2,
            )

    # Scan runtime/*.yaml and runtime/*.yml files (F-08: extension filter).
    runtime_dir = base / "runtime"
    if runtime_dir.is_dir():
        yaml_paths = sorted(
            {p for p in runtime_dir.glob("*.yaml")} | {p for p in runtime_dir.glob("*.yml")}
        )
        for yaml_file in yaml_paths:
            try:
                text = yaml_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                warnings.warn(
                    f"Skipping {yaml_file.name}: could not read file: {exc}",
                    UserWarning,
                    stacklevel=2,
                )
                continue

            fields = parse_yaml_adapter(text)
            if not fields.get("project_name"):
                warnings.warn(
                    f"Skipping {yaml_file.name}: no project_name found",
                    UserWarning,
                    stacklevel=2,
                )
                continue

            fields["adapter_source"] = "yaml"
            try:
                cfg = AdapterConfig(**fields)
                registry[cfg.project_name] = cfg
            except (TypeError, ValueError) as exc:
                warnings.warn(
                    f"Skipping {yaml_file.name}: validation error: {exc}",
                    UserWarning,
                    stacklevel=2,
                )

    return registry


# ---------------------------------------------------------------------------
# Project resolver
# ---------------------------------------------------------------------------


def resolve_project(
    name: str, registry: dict[str, AdapterConfig]
) -> Optional[AdapterConfig]:
    """Look up a project by name in the registry.

    Resolution order:
    1. Exact match on project_name
    2. Case-insensitive match
    3. Substring match (name is a substring of project_name)

    Returns the AdapterConfig if found, or None. Logs which resolution
    strategy was used for traceability (T-9-05 mitigation).
    """
    # 1. Exact match
    if name in registry:
        return registry[name]

    # 2. Case-insensitive match
    name_lower = name.lower()
    for key, cfg in registry.items():
        if key.lower() == name_lower:
            logger.info(
                "resolve_project(%r): case-insensitive match -> %s", name, key
            )
            return cfg

    # 3. Substring match
    matches: list[tuple[str, AdapterConfig]] = []
    for key, cfg in registry.items():
        if name_lower in key.lower():
            matches.append((key, cfg))

    if len(matches) == 1:
        logger.info(
            "resolve_project(%r): substring match -> %s", name, matches[0][0]
        )
        return matches[0][1]

    if len(matches) > 1:
        matched_names = [m[0] for m in matches]
        logger.warning(
            "resolve_project(%r): ambiguous substring match among %s; "
            "returning first hit: %s",
            name,
            matched_names,
            matches[0][0],
        )
        return matches[0][1]

    return None

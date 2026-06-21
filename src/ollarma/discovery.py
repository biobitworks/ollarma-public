"""discovery.py -- Adapter path resolution and plugin discovery.

Provides three public functions:
- resolve_adapters_dir:  4-level precedence (CLI > env > config > default)
- discover_entry_point_adapters:  entry_point-based plugin discovery
- validate_adapter_path:  path validation with allowlist enforcement for service mode

Exports:
    DEFAULT_ADAPTERS_DIR  -- Hardcoded fallback adapter directory
    ENV_VAR               -- Environment variable name for adapter path override
    CONFIG_PATH           -- Default config file path (~/.config/ollarma/config.toml)
    resolve_adapters_dir  -- 4-level adapter path resolution
    discover_entry_point_adapters  -- Entry-point plugin discovery
    validate_adapter_path -- Path validation with allowlist enforcement
"""
from __future__ import annotations

import logging
import os
import pathlib
import warnings
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_ADAPTERS_DIR = "<repo>/adapters"
ENV_VAR = "OLLARMA_ADAPTERS_DIR"
CONFIG_PATH = pathlib.Path.home() / ".config" / "ollarma" / "config.toml"


# ---------------------------------------------------------------------------
# 4-level adapter path resolution
# ---------------------------------------------------------------------------


def resolve_adapters_dir(cli_override: str | None = None) -> str:
    """Resolve adapter directory with 4-level precedence.

    Resolution order:
        1. CLI flag (``cli_override`` parameter)
        2. ``OLLARMA_ADAPTERS_DIR`` environment variable
        3. ``~/.config/ollarma/config.toml`` ``[adapters] dir`` key
        4. Hardcoded ``DEFAULT_ADAPTERS_DIR`` (backward compatibility)

    Returns the resolved directory path as a string.
    """
    # Level 1: CLI flag
    if cli_override is not None:
        logger.debug("Adapter dir from CLI: %s", cli_override)
        return cli_override

    # Level 2: Environment variable
    env_val = os.environ.get(ENV_VAR)
    if env_val:
        logger.debug("Adapter dir from %s: %s", ENV_VAR, env_val)
        return env_val

    # Level 3: Config file
    if CONFIG_PATH.exists():
        import tomllib

        try:
            with open(CONFIG_PATH, "rb") as f:
                config = tomllib.load(f)
            dir_val = config.get("adapters", {}).get("dir")
            if dir_val:
                expanded = str(pathlib.Path(dir_val).expanduser())
                logger.debug("Adapter dir from config: %s", expanded)
                return expanded
        except Exception as exc:
            logger.warning("Failed to read config %s: %s", CONFIG_PATH, exc)

    # Level 4: Default
    logger.debug("Adapter dir from default: %s", DEFAULT_ADAPTERS_DIR)
    return DEFAULT_ADAPTERS_DIR


# ---------------------------------------------------------------------------
# Entry-point plugin discovery
# ---------------------------------------------------------------------------


def discover_entry_point_adapters() -> list[str]:
    """Find adapter directories registered by external packages.

    External packages register via their ``pyproject.toml``::

        [project.entry-points."ollarma.adapters"]
        myproject = "mypackage.adapters:get_adapters_dir"

    The callable must return a ``str`` (directory path) pointing to an
    existing directory on disk.

    Returns a list of valid directory paths.  Invalid or erroring entry
    points are skipped with a warning.
    """
    dirs: list[str] = []
    eps = entry_points(group="ollarma.adapters")

    for ep in eps:
        try:
            func = ep.load()
            path = func()
        except Exception as exc:
            warnings.warn(
                f"Failed to load adapter entry point {ep.name}: {exc}",
                UserWarning,
                stacklevel=2,
            )
            continue

        if not isinstance(path, str):
            warnings.warn(
                f"Entry point {ep.name} returned non-string: {type(path).__name__}",
                UserWarning,
                stacklevel=2,
            )
            continue

        if not pathlib.Path(path).is_dir():
            warnings.warn(
                f"Entry point {ep.name} returned non-directory path: {path}",
                UserWarning,
                stacklevel=2,
            )
            continue

        dirs.append(path)
        logger.info("Discovered adapter dir from %s: %s", ep.name, path)

    return dirs


# ---------------------------------------------------------------------------
# Path validation with allowlist enforcement
# ---------------------------------------------------------------------------


def validate_adapter_path(
    path: str,
    allowlist: list[str] | None = None,
    service_mode: bool = False,
) -> pathlib.Path:
    """Validate and resolve an adapter path.

    In service mode with a non-empty allowlist, the resolved path must be
    within one of the allowed directories.  Symlinks are resolved before
    checking (T-11-06 mitigation).

    Args:
        path: Adapter directory path to validate.
        allowlist: List of allowed parent directories (only enforced in service mode).
        service_mode: When True, enforce allowlist restrictions.

    Returns:
        The resolved ``pathlib.Path``.

    Raises:
        ValueError: If the resolved path is outside the allowlist in service mode.
        FileNotFoundError: If the resolved path does not exist as a directory.
    """
    resolved = pathlib.Path(path).resolve()

    # Enforce allowlist in service mode
    if service_mode and allowlist is not None and len(allowlist) > 0:
        if not any(
            resolved.is_relative_to(pathlib.Path(a).resolve()) for a in allowlist
        ):
            raise ValueError(
                f"Adapter path {path} is outside allowed directories. "
                f"Allowed: {allowlist}"
            )

    # Check existence
    if not resolved.is_dir():
        raise FileNotFoundError(f"Adapter directory does not exist: {path}")

    return resolved

"""namespace_registry.py -- Thread-safe registry of valid namespace prefixes.

Derives valid prefixes from the fleet adapter directory (no separate namespaces.yaml
config file needed). Loaded once at process start; reload via reload() on SIGHUP.

Singleton pattern matches _scheduler in service.py.
"""
from __future__ import annotations

import threading


_lock = threading.Lock()


class NamespaceRegistry:
    """Thread-safe registry of valid namespace prefixes.

    Valid prefixes are derived from AdapterConfig.namespace_prefix across all
    loaded fleet adapters. The empty string is always valid (maps to __unscoped__).
    Non-empty, unrecognised prefixes fail closed (NS-02).
    """

    def __init__(self) -> None:
        self._prefixes: frozenset[str] = frozenset()
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with _lock:
            if self._loaded:
                return
            self._load_from_fleet()

    def _load_from_fleet(self) -> None:
        """Derive valid prefixes by scanning the fleet adapter registry."""
        # Import here to avoid circular imports at module load time
        from ollarma import service as _svc

        registry = _svc.list_projects(service_mode=True)
        prefixes = {adapter.namespace_prefix for adapter in registry.values()}
        # Empty string is handled as __unscoped__ — always valid, not stored in registry
        prefixes.discard("")
        self._prefixes = frozenset(prefixes)
        self._loaded = True

    def is_registered(self, prefix: str) -> bool:
        """Return True if prefix is a registered namespace OR is empty (unscoped).

        Empty prefix is always accepted — existing callers that predate Phase 30
        are never broken by NS-02 validation (backward-compatible contract).
        """
        if not prefix:
            return True  # empty → __unscoped__ bucket, always valid
        self._ensure_loaded()
        return prefix in self._prefixes

    def reload(self) -> None:
        """Force a fresh load from the fleet registry (e.g., on SIGHUP)."""
        with _lock:
            self._loaded = False
            self._load_from_fleet()


# Process-level singleton — matches pattern of _scheduler in service.py
NAMESPACE_REGISTRY = NamespaceRegistry()

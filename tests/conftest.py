"""conftest.py — pytest configuration for local-model-bench test suite.

Registers the 'live' marker for tests requiring a running Ollama instance.
Live tests are skipped by default: pytest -m "not live"
To run live tests: pytest -m live
"""
import subprocess

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: mark test as requiring a live Ollama instance (skipped by default)",
    )


@pytest.fixture(autouse=True)
def _isolate_idempotency_store(tmp_path, monkeypatch):
    """Redirect the idempotency SQLite cache to tmp_path for every test.

    Prevents cross-test contamination where a cached result from test A
    satisfies the idempotency check in test B (which expects fresh inference).
    Each test gets a clean, empty idempotency store.
    """
    import ollarma.idempotency as idem

    monkeypatch.setattr(idem, "_CACHE_BASE", tmp_path / "idem_cache")
    monkeypatch.setattr(idem, "_stores", {})


@pytest.fixture(autouse=True)
def _disable_recovery_admission_by_default(monkeypatch):
    """v4.3 ADMIT-06: opt out of recovery admission during tests by default.

    Admission checks against the real ollarma CWD would surface any developer
    sidecar worktrees as STRANDED_WORKTREE and block every test that exercises
    a gated entrypoint (route_prompt, submit_workflow, submit_autopilot,
    run_agent). Tests for admission itself override this fixture in-file.
    """
    monkeypatch.setenv("OLLARMA_RECOVERY_ADMISSION", "off")


@pytest.fixture
def require_ollama():
    """Skip test if Ollama is not reachable."""
    try:
        subprocess.run(
            ["ollama", "ps"],
            capture_output=True,
            timeout=5,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("Ollama not available — skipping live test")


def _sibling_status(registry: dict) -> dict[str, set[str]]:
    """Snapshot git status --porcelain lines for each sibling repo."""
    import pathlib
    status: dict[str, set[str]] = {}
    for name, adapter in registry.items():
        root = pathlib.Path(adapter.project_root)
        if not root.is_dir():
            continue
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            status[name] = set(result.stdout.strip().splitlines())
        except (subprocess.TimeoutExpired, OSError):
            pass  # skip unreachable repos
    return status


@pytest.fixture(scope="session", autouse=True)
def assert_no_sibling_mutation():
    """SAFE-06: Assert no sibling project repo was mutated during the test run.

    Snapshots git status --porcelain on sibling repos BEFORE tests, then
    compares AFTER. Only new lines (mutations introduced by tests) trigger
    failure. Pre-existing uncommitted changes are ignored.
    Skips gracefully if adapters directory is missing or empty.
    """
    import pathlib
    registry: dict = {}
    before: dict[str, set[str]] = {}

    try:
        from ollarma.discovery import resolve_adapters_dir
        from ollarma.fleet import load_fleet_registry
        adapters_dir = resolve_adapters_dir()
        if pathlib.Path(adapters_dir).is_dir():
            registry = load_fleet_registry(adapters_dir)
            if registry:
                before = _sibling_status(registry)
    except Exception:
        pass  # adapters not configured -- skip check

    yield  # all tests run here

    if not registry:
        return

    # Paths that are tooling infrastructure, not code mutations caused by tests
    _TOOLING_PREFIXES = (".claude/", ".DS_Store", ".ollarma/", ".planning/")

    import warnings

    after = _sibling_status(registry)
    for name in after:
        new_lines = after[name] - before.get(name, set())
        # Filter out tooling artifacts (Claude worktrees, macOS metadata, ollarma infra)
        new_lines = {
            line for line in new_lines
            if not any(line.lstrip(" ?!MA").strip().startswith(p) for p in _TOOLING_PREFIXES)
        }
        if new_lines:
            msg = (
                f"Sibling repo '{name}' changed during test run "
                f"(new changes):\n" + "\n".join(sorted(new_lines))
            )
            # Warn rather than fail — concurrent development on other
            # projects can cause benign diffs unrelated to our tests.
            warnings.warn(msg, stacklevel=1)

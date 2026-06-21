"""Tests for ollarma/discovery.py -- adapter path resolution and plugin discovery.

TDD RED: Tests for 4-level precedence resolution, entry-point plugin discovery,
and path validation with allowlist enforcement.
"""
from __future__ import annotations

import pathlib
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from ollarma.discovery import (
    DEFAULT_ADAPTERS_DIR,
    ENV_VAR,
    CONFIG_PATH,
    resolve_adapters_dir,
    discover_entry_point_adapters,
    validate_adapter_path,
)


# ---------------------------------------------------------------------------
# resolve_adapters_dir -- 4-level precedence
# ---------------------------------------------------------------------------


class TestResolveAdaptersDir:
    """Tests for 4-level precedence: CLI > env > config > default."""

    def test_level1_cli_override(self) -> None:
        """CLI override takes highest precedence."""
        result = resolve_adapters_dir(cli_override="/custom/path")
        assert result == "/custom/path"

    def test_level2_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OLLARMA_ADAPTERS_DIR env var used when no CLI override."""
        monkeypatch.setenv("OLLARMA_ADAPTERS_DIR", "/env/path")
        result = resolve_adapters_dir()
        assert result == "/env/path"

    def test_level3_config_file(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config file used when no CLI or env var."""
        # Remove env var if set
        monkeypatch.delenv("OLLARMA_ADAPTERS_DIR", raising=False)

        # Create a config file
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            textwrap.dedent("""\
                [adapters]
                dir = "/config/path"
            """)
        )
        # Patch CONFIG_PATH to point to our temp file
        monkeypatch.setattr("ollarma.discovery.CONFIG_PATH", config_file)

        result = resolve_adapters_dir()
        assert result == "/config/path"

    def test_level4_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hardcoded default used when no overrides at all."""
        monkeypatch.delenv("OLLARMA_ADAPTERS_DIR", raising=False)
        # Point config to a nonexistent file
        monkeypatch.setattr(
            "ollarma.discovery.CONFIG_PATH",
            pathlib.Path("/nonexistent/config.toml"),
        )
        result = resolve_adapters_dir()
        assert result == DEFAULT_ADAPTERS_DIR

    def test_precedence_cli_beats_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI override beats env var."""
        monkeypatch.setenv("OLLARMA_ADAPTERS_DIR", "/env/path")
        result = resolve_adapters_dir(cli_override="/cli/path")
        assert result == "/cli/path"

    def test_precedence_env_beats_config(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var beats config file."""
        monkeypatch.setenv("OLLARMA_ADAPTERS_DIR", "/env/path")
        config_file = tmp_path / "config.toml"
        config_file.write_text('[adapters]\ndir = "/config/path"\n')
        monkeypatch.setattr("ollarma.discovery.CONFIG_PATH", config_file)

        result = resolve_adapters_dir()
        assert result == "/env/path"

    def test_precedence_config_beats_default(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config file beats hardcoded default."""
        monkeypatch.delenv("OLLARMA_ADAPTERS_DIR", raising=False)
        config_file = tmp_path / "config.toml"
        config_file.write_text('[adapters]\ndir = "/config/path"\n')
        monkeypatch.setattr("ollarma.discovery.CONFIG_PATH", config_file)

        result = resolve_adapters_dir()
        assert result == "/config/path"

    def test_config_expands_tilde(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config file dir with ~ gets expanded."""
        monkeypatch.delenv("OLLARMA_ADAPTERS_DIR", raising=False)
        config_file = tmp_path / "config.toml"
        config_file.write_text('[adapters]\ndir = "~/my-adapters"\n')
        monkeypatch.setattr("ollarma.discovery.CONFIG_PATH", config_file)

        result = resolve_adapters_dir()
        assert result == str(pathlib.Path.home() / "my-adapters")

    def test_empty_env_var_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty env var is treated as unset (falls through to next level)."""
        monkeypatch.setenv("OLLARMA_ADAPTERS_DIR", "")
        monkeypatch.setattr(
            "ollarma.discovery.CONFIG_PATH",
            pathlib.Path("/nonexistent/config.toml"),
        )
        result = resolve_adapters_dir()
        assert result == DEFAULT_ADAPTERS_DIR


# ---------------------------------------------------------------------------
# discover_entry_point_adapters -- plugin discovery
# ---------------------------------------------------------------------------


class TestDiscoverEntryPointAdapters:
    """Tests for importlib.metadata entry_point-based adapter discovery."""

    def test_no_plugins_returns_empty(self) -> None:
        """Returns empty list when no plugins are installed."""
        with patch("ollarma.discovery.entry_points", return_value=[]):
            result = discover_entry_point_adapters()
        assert result == []

    def test_valid_plugin_returns_path(self, tmp_path: pathlib.Path) -> None:
        """Returns path when a mock entry point returns a valid directory."""
        adapter_dir = tmp_path / "adapters"
        adapter_dir.mkdir()

        mock_ep = MagicMock()
        mock_ep.name = "test-project"
        mock_ep.load.return_value = lambda: str(adapter_dir)

        with patch("ollarma.discovery.entry_points", return_value=[mock_ep]):
            result = discover_entry_point_adapters()

        assert result == [str(adapter_dir)]

    def test_entry_point_raises_warns_and_skips(self) -> None:
        """Warns and skips when entry point callable raises."""
        mock_ep = MagicMock()
        mock_ep.name = "broken-plugin"
        mock_ep.load.return_value = MagicMock(side_effect=RuntimeError("kaboom"))

        with patch("ollarma.discovery.entry_points", return_value=[mock_ep]):
            with pytest.warns(UserWarning, match="broken-plugin"):
                result = discover_entry_point_adapters()

        assert result == []

    def test_non_string_return_warns_and_skips(self) -> None:
        """Warns and skips when entry point returns non-string."""
        mock_ep = MagicMock()
        mock_ep.name = "bad-return"
        mock_ep.load.return_value = lambda: 42  # Not a string

        with patch("ollarma.discovery.entry_points", return_value=[mock_ep]):
            with pytest.warns(UserWarning, match="bad-return"):
                result = discover_entry_point_adapters()

        assert result == []

    def test_nonexistent_dir_warns_and_skips(self) -> None:
        """Warns and skips when entry point returns a string that is not a directory."""
        mock_ep = MagicMock()
        mock_ep.name = "missing-dir"
        mock_ep.load.return_value = lambda: "/nonexistent/path/that/does/not/exist"

        with patch("ollarma.discovery.entry_points", return_value=[mock_ep]):
            with pytest.warns(UserWarning, match="missing-dir"):
                result = discover_entry_point_adapters()

        assert result == []


# ---------------------------------------------------------------------------
# validate_adapter_path -- security validation
# ---------------------------------------------------------------------------


class TestValidateAdapterPath:
    """Tests for adapter path validation with allowlist enforcement."""

    def test_service_mode_allows_path_within_allowlist(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Path within allowlist succeeds in service mode."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        sub = allowed / "sub"
        sub.mkdir()

        result = validate_adapter_path(
            str(sub), allowlist=[str(allowed)], service_mode=True
        )
        assert result == sub.resolve()

    def test_service_mode_rejects_path_outside_allowlist(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Path outside allowlist raises ValueError in service mode."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        with pytest.raises(ValueError, match="outside allowed directories"):
            validate_adapter_path(
                str(outside), allowlist=[str(allowed)], service_mode=True
            )

    def test_service_mode_symlink_escape_rejected(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Symlink that escapes allowlist is rejected in service mode."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        secret = tmp_path / "secret"
        secret.mkdir()
        # Create symlink inside allowed that points outside
        escape_link = allowed / "escape"
        escape_link.symlink_to(secret)

        with pytest.raises(ValueError, match="outside allowed directories"):
            validate_adapter_path(
                str(escape_link), allowlist=[str(allowed)], service_mode=True
            )

    def test_non_service_mode_allows_any_path(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Non-service mode allows any valid directory path."""
        any_dir = tmp_path / "anywhere"
        any_dir.mkdir()

        result = validate_adapter_path(str(any_dir), service_mode=False)
        assert result == any_dir.resolve()

    def test_non_service_mode_ignores_allowlist(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Non-service mode ignores allowlist even if provided."""
        outside = tmp_path / "outside"
        outside.mkdir()

        result = validate_adapter_path(
            str(outside), allowlist=["/some/other/path"], service_mode=False
        )
        assert result == outside.resolve()

    def test_nonexistent_directory_raises(self) -> None:
        """Nonexistent directory raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="does not exist"):
            validate_adapter_path("/nonexistent/path/that/does/not/exist")

    def test_service_mode_empty_allowlist_allows_all(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Service mode with empty allowlist allows all paths."""
        any_dir = tmp_path / "anywhere"
        any_dir.mkdir()

        result = validate_adapter_path(
            str(any_dir), allowlist=[], service_mode=True
        )
        assert result == any_dir.resolve()

    def test_service_mode_none_allowlist_allows_all(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Service mode with None allowlist allows all paths."""
        any_dir = tmp_path / "anywhere"
        any_dir.mkdir()

        result = validate_adapter_path(
            str(any_dir), allowlist=None, service_mode=True
        )
        assert result == any_dir.resolve()

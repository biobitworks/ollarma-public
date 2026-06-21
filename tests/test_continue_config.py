"""Tests for bench continue-config command.

Validates .continuerc.json generation with correct Ollama provider,
model selection, and override behavior.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from typer.testing import CliRunner
from unittest.mock import patch

from ollarma.cli import app


runner = CliRunner()


class TestContinueConfig:
    """Tests for .continuerc.json generation."""

    def test_command_registered(self) -> None:
        """continue-config is a registered CLI command."""
        result = runner.invoke(app, ["--help"])
        assert "continue-config" in result.output

    def test_generates_json_file(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """continue-config writes .continuerc.json to current directory."""
        monkeypatch.chdir(tmp_path)
        with patch("ollarma.cli.resolve_default_model", return_value="qwen2.5-coder:7b"):
            result = runner.invoke(app, ["continue-config"])
        assert result.exit_code == 0
        config_path = tmp_path / ".continuerc.json"
        assert config_path.exists()

    def test_json_is_valid(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """Generated file is valid JSON."""
        monkeypatch.chdir(tmp_path)
        with patch("ollarma.cli.resolve_default_model", return_value="qwen2.5-coder:7b"):
            runner.invoke(app, ["continue-config"])
        config = json.loads((tmp_path / ".continuerc.json").read_text())
        assert "models" in config
        assert "tabAutocompleteModel" in config

    def test_models_array_has_ollama_provider(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """models[0] has provider='ollama' and apiBase pointing to localhost."""
        monkeypatch.chdir(tmp_path)
        with patch("ollarma.cli.resolve_default_model", return_value="qwen2.5-coder:7b"):
            runner.invoke(app, ["continue-config"])
        config = json.loads((tmp_path / ".continuerc.json").read_text())
        assert config["models"][0]["provider"] == "ollama"
        assert config["models"][0]["apiBase"] == "http://localhost:11434"

    def test_default_model_from_bench(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """Default model comes from resolve_default_model()."""
        monkeypatch.chdir(tmp_path)
        with patch("ollarma.cli.resolve_default_model", return_value="qwen3:8b"):
            runner.invoke(app, ["continue-config"])
        config = json.loads((tmp_path / ".continuerc.json").read_text())
        assert config["models"][0]["model"] == "qwen3:8b"

    def test_model_override(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """--model flag overrides default."""
        monkeypatch.chdir(tmp_path)
        with patch("ollarma.cli.resolve_default_model", return_value="qwen2.5-coder:7b"):
            result = runner.invoke(app, ["continue-config", "--model", "phi4-mini"])
        assert result.exit_code == 0
        config = json.loads((tmp_path / ".continuerc.json").read_text())
        assert config["models"][0]["model"] == "phi4-mini"
        assert config["tabAutocompleteModel"]["model"] == "phi4-mini"

    def test_tab_autocomplete_model_matches(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """tabAutocompleteModel uses same model as chat model."""
        monkeypatch.chdir(tmp_path)
        with patch("ollarma.cli.resolve_default_model", return_value="qwen2.5-coder:7b"):
            runner.invoke(app, ["continue-config"])
        config = json.loads((tmp_path / ".continuerc.json").read_text())
        chat_model = config["models"][0]["model"]
        tab_model = config["tabAutocompleteModel"]["model"]
        assert chat_model == tab_model

    def test_prints_model_name(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """Command output includes the model name."""
        monkeypatch.chdir(tmp_path)
        with patch("ollarma.cli.resolve_default_model", return_value="qwen2.5-coder:7b"):
            result = runner.invoke(app, ["continue-config"])
        assert "qwen2.5-coder:7b" in result.output

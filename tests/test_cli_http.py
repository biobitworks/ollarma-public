"""Tests for HTTP-related CLI behavior."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from ollarma.cli import app


runner = CliRunner()


class TestServeCommand:
    """Tests for `ollarma serve` CLI safety behavior."""

    @patch("uvicorn.run")
    def test_serve_rejects_non_local_host(self, mock_run) -> None:
        """serve rejects non-loopback bind addresses."""
        result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
        assert result.exit_code == 1
        assert "localhost-only" in result.output.lower()
        mock_run.assert_not_called()

    @patch("uvicorn.run")
    def test_serve_runs_with_localhost(self, mock_run) -> None:
        """serve allows localhost binds and delegates to uvicorn."""
        result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "8484"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            "ollarma.http_api:app",
            host="127.0.0.1",
            port=8484,
            log_level="info",
        )

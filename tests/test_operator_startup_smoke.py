"""Tests for Plan 51-03 `ollarma startup-smoke` (PERSIST-01 + PERSIST-03).

Smoke inspects launchctl + HTTP. All subprocess + HTTP calls mocked here.
No live service required.
"""
from __future__ import annotations

import io
import subprocess
import urllib.error
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from ollarma.cli import app

runner = CliRunner()


@dataclass
class _R:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _fake_urlopen(responses: dict[str, bytes | Exception]):
    """Return an urlopen replacement that serves mapped path payloads."""
    def _open(url_or_req, *args, **kwargs):  # noqa: ARG001
        url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
        # Match on path suffix
        for path, payload in responses.items():
            if url.endswith(path):
                if isinstance(payload, Exception):
                    raise payload
                m = MagicMock()
                m.__enter__ = lambda s: s
                m.__exit__ = lambda *args: None
                m.read.return_value = payload
                m.status = 200
                return m
        raise urllib.error.URLError(f"unmocked url: {url}")
    return _open


class TestStartupSmokeHealthy:
    def test_ready_state_exits_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # launchctl list returns success
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _R(returncode=0, stdout="- 123 com.byron.ollarma"),
        )
        # HTTP endpoints return ready
        import urllib.request as _ureq
        monkeypatch.setattr(
            _ureq, "urlopen",
            _fake_urlopen({
                "/health": b'{"status":"ready","startup_readiness":{"status":"ready"}}',
                "/startup/readiness": b'{"schema_version":1,"status":"ready","checks":[]}',
                "/models/status": b'{"loaded_model_count":0,"pipelines":{}}',
                "/metrics": b'# HELP ollarma_ready 1\n',
            }),
        )

        result = runner.invoke(app, ["startup-smoke"])
        assert result.exit_code == 0, result.output


class TestStartupSmokeDegraded:
    def test_degraded_exits_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Degraded state must still exit 0 — it's observational, not fatal."""
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _R(returncode=0, stdout="- 123 com.byron.ollarma"),
        )
        import urllib.request as _ureq
        monkeypatch.setattr(
            _ureq, "urlopen",
            _fake_urlopen({
                "/health": b'{"status":"degraded"}',
                "/startup/readiness": b'{"schema_version":1,"status":"degraded","checks":[]}',
                "/models/status": b"{}",
                "/metrics": b"# ok",
            }),
        )
        result = runner.invoke(app, ["startup-smoke"])
        assert result.exit_code == 0, result.output


class TestStartupSmokeBlocked:
    def test_blocked_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _R(returncode=0, stdout="- 123 com.byron.ollarma"),
        )
        import urllib.request as _ureq
        monkeypatch.setattr(
            _ureq, "urlopen",
            _fake_urlopen({
                "/health": b'{"status":"blocked"}',
                "/startup/readiness": b'{"schema_version":1,"status":"blocked","checks":[]}',
                "/models/status": b"{}",
                "/metrics": b"",
            }),
        )
        result = runner.invoke(app, ["startup-smoke"])
        assert result.exit_code == 1, result.output


class TestStartupSmokeFailureModes:
    def test_http_unreachable_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _R(returncode=0, stdout="- 123 com.byron.ollarma"),
        )
        import urllib.request as _ureq
        monkeypatch.setattr(
            _ureq, "urlopen",
            _fake_urlopen({
                "/health": urllib.error.URLError("connection refused"),
            }),
        )
        result = runner.invoke(app, ["startup-smoke"])
        assert result.exit_code == 1, result.output

    def test_missing_launchd_service_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # launchctl list returns nothing (service not loaded)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _R(returncode=0, stdout=""),
        )
        import urllib.request as _ureq
        monkeypatch.setattr(
            _ureq, "urlopen",
            _fake_urlopen({
                "/health": b'{"status":"ready"}',
                "/startup/readiness": b'{"schema_version":1,"status":"ready","checks":[]}',
                "/models/status": b"{}",
                "/metrics": b"",
            }),
        )
        result = runner.invoke(app, ["startup-smoke"])
        assert result.exit_code == 1, result.output


class TestStartupSmokeArgShape:
    def test_honors_base_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called_urls: list[str] = []

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _R(returncode=0, stdout="- 123 com.byron.ollarma"),
        )
        import urllib.request as _ureq

        def _rec(url_or_req, *a, **k):  # noqa: ARG001
            url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
            called_urls.append(url)
            m = MagicMock()
            m.__enter__ = lambda s: s
            m.__exit__ = lambda *args: None
            m.read.return_value = b'{"schema_version":1,"status":"ready","checks":[]}'
            m.status = 200
            return m

        monkeypatch.setattr(_ureq, "urlopen", _rec)
        result = runner.invoke(
            app, ["startup-smoke", "--base-url", "http://localhost:9999"],
        )
        assert result.exit_code == 0, result.output
        assert any("localhost:9999" in u for u in called_urls)

    def test_all_four_endpoints_probed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called_paths: list[str] = []

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _R(returncode=0, stdout="- 123 com.byron.ollarma"),
        )
        import urllib.request as _ureq

        def _rec(url_or_req, *a, **k):  # noqa: ARG001
            url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
            called_paths.append(url)
            m = MagicMock()
            m.__enter__ = lambda s: s
            m.__exit__ = lambda *args: None
            m.read.return_value = b'{"schema_version":1,"status":"ready","checks":[]}'
            m.status = 200
            return m

        monkeypatch.setattr(_ureq, "urlopen", _rec)
        result = runner.invoke(app, ["startup-smoke"])
        assert result.exit_code == 0, result.output
        # Smoke probes 4 endpoints
        assert any("/health" in u for u in called_paths)
        assert any("/startup/readiness" in u for u in called_paths)
        assert any("/models/status" in u for u in called_paths)
        assert any("/metrics" in u for u in called_paths)

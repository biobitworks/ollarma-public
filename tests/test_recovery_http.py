"""Tests for recovery HTTP endpoints (phase 43, HTTP-01/02, TEST-05)."""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
from starlette.testclient import TestClient


def _git(repo: pathlib.Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")


@pytest.fixture
def clean_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def client_in_repo(clean_repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Run the HTTP app with CWD inside clean_repo (so service.recover_* defaults
    resolve to the fixture repo)."""
    monkeypatch.chdir(clean_repo)
    from ollarma.http_api import app
    return TestClient(app), clean_repo


class TestRecoveryStatusEndpoint:
    def test_status_404_when_no_packet(self, client_in_repo) -> None:
        client, _ = client_in_repo
        r = client.get("/recovery/status")
        assert r.status_code == 404
        assert r.json().get("error") == "no_recovery_packet"

    def test_status_200_after_scan(self, client_in_repo) -> None:
        client, _ = client_in_repo
        scan = client.post("/recovery/scan", json={})
        assert scan.status_code == 200
        r = client.get("/recovery/status")
        assert r.status_code == 200
        assert r.json()["state"] == "clean"


class TestRecoveryScanEndpoint:
    def test_scan_returns_fresh_packet(self, client_in_repo) -> None:
        client, repo = client_in_repo
        r = client.post("/recovery/scan", json={})
        assert r.status_code == 200, r.text
        packet = r.json()
        assert packet["state"] == "clean"
        assert packet["source"] == "http"
        latest = repo / ".ollarma" / "incidents" / "latest.json"
        assert latest.exists()

    def test_scan_honors_base_branch(self, client_in_repo) -> None:
        client, _ = client_in_repo
        # Passing a nonexistent base should still succeed (scanner treats
        # missing base as "no ahead commits").
        r = client.post("/recovery/scan", json={"base_branch": "develop"})
        assert r.status_code == 200

    def test_scan_accepts_empty_body(self, client_in_repo) -> None:
        client, _ = client_in_repo
        r = client.post("/recovery/scan")
        assert r.status_code == 200


class TestRoutingRegistration:
    def test_recovery_routes_registered(self) -> None:
        from ollarma.http_api import app
        paths = {r.path for r in app.routes}
        assert "/recovery/status" in paths
        assert "/recovery/scan" in paths

"""Tests for ollarma.sibling_reader — fail-closed allowlisted file reader.

Covers:
  - Unit tests: allowlist enforcement, symlink escape, missing file, namespace threading
  - HTTP integration tests: /v1/sibling/read endpoint via Starlette TestClient
"""
from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from ollarma.sibling_reader import (
    SIBLING_NOT_ALLOWLISTED,
    SiblingReadReceipt,
    read_sibling_path,
)


# ---------------------------------------------------------------------------
# Unit tests: sibling_reader.py core logic
# ---------------------------------------------------------------------------


def test_read_allowlisted_path(tmp_path: pathlib.Path) -> None:
    """File under allowlisted root is returned with correct content + namespace."""
    target = tmp_path / "notes.txt"
    target.write_text("hello sibling", encoding="utf-8")

    receipt = read_sibling_path(
        target,
        namespace="my-ns",
        allowlisted_roots=[tmp_path],
    )

    assert receipt.content == "hello sibling"
    assert receipt.namespace == "my-ns"
    assert str(target.resolve()) == receipt.path
    assert receipt.project is None


def test_reject_outside_allowlist(tmp_path: pathlib.Path, tmp_path_factory) -> None:
    """Path outside all allowlisted roots raises ValueError with SIBLING_NOT_ALLOWLISTED."""
    allowed_dir = tmp_path_factory.mktemp("allowed")
    other_dir = tmp_path

    target = other_dir / "secret.txt"
    target.write_text("not allowed", encoding="utf-8")

    with pytest.raises(ValueError, match=SIBLING_NOT_ALLOWLISTED):
        read_sibling_path(
            target,
            namespace="ns",
            allowlisted_roots=[allowed_dir],
        )


def test_reject_empty_allowlist(tmp_path: pathlib.Path) -> None:
    """Empty allowlisted_roots always rejects — fail-closed."""
    target = tmp_path / "file.txt"
    target.write_text("data", encoding="utf-8")

    with pytest.raises(ValueError, match=SIBLING_NOT_ALLOWLISTED):
        read_sibling_path(
            target,
            namespace="ns",
            allowlisted_roots=[],
        )


def test_reject_symlink_escape(tmp_path: pathlib.Path, tmp_path_factory) -> None:
    """Symlink pointing outside the allowlisted root is rejected after resolve()."""
    allowed_dir = tmp_path_factory.mktemp("allowed")
    outside_dir = tmp_path

    outside_file = outside_dir / "outside.txt"
    outside_file.write_text("outside content", encoding="utf-8")

    # Create symlink inside allowed_dir pointing to outside_dir file
    link = allowed_dir / "escape.txt"
    link.symlink_to(outside_file)

    # The symlink resolves to outside_file which is NOT under allowed_dir
    with pytest.raises(ValueError, match=SIBLING_NOT_ALLOWLISTED):
        read_sibling_path(
            link,
            namespace="ns",
            allowlisted_roots=[allowed_dir],
        )


def test_missing_file_raises_value_error(tmp_path: pathlib.Path) -> None:
    """Path under allowlist that doesn't exist raises ValueError with SIBLING_READ_ERROR."""
    missing = tmp_path / "does_not_exist.txt"

    with pytest.raises(ValueError, match="SIBLING_READ_ERROR"):
        read_sibling_path(
            missing,
            namespace="ns",
            allowlisted_roots=[tmp_path],
        )


def test_namespace_threaded_to_receipt(tmp_path: pathlib.Path) -> None:
    """namespace parameter is faithfully reflected in the returned receipt."""
    target = tmp_path / "readme.md"
    target.write_text("# readme", encoding="utf-8")

    receipt = read_sibling_path(
        target,
        namespace="myproject",
        allowlisted_roots=[tmp_path],
    )

    assert receipt.namespace == "myproject"


# ---------------------------------------------------------------------------
# HTTP integration tests: POST /v1/sibling/read
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Fresh Starlette TestClient for HTTP tests."""
    from ollarma.http_api import app

    return TestClient(app, raise_server_exceptions=False)


def _make_receipt(tmp_path: pathlib.Path) -> SiblingReadReceipt:
    """Helper: build a SiblingReadReceipt for mocking."""
    return SiblingReadReceipt(
        path=str(tmp_path / "file.txt"),
        content="test content",
        namespace="test-ns",
        project=None,
        read_at="2026-01-01T00:00:00Z",
    )


def test_http_sibling_read_allowlisted(client, tmp_path: pathlib.Path) -> None:
    """POST /v1/sibling/read with allowlisted path returns 200 with receipt fields."""
    receipt = _make_receipt(tmp_path)

    with patch("ollarma.service.read_sibling_file", return_value=receipt):
        response = client.post(
            "/v1/sibling/read",
            json={"path": str(tmp_path / "file.txt"), "namespace_prefix": "test-ns"},
        )

    assert response.status_code == 200
    body = response.json()
    assert "path" in body
    assert "content" in body
    assert "namespace" in body


def test_http_sibling_read_not_allowlisted(client, tmp_path: pathlib.Path) -> None:
    """POST /v1/sibling/read with non-allowlisted path returns 403 with reason_code."""
    with patch(
        "ollarma.service.read_sibling_file",
        side_effect=ValueError(SIBLING_NOT_ALLOWLISTED),
    ):
        response = client.post(
            "/v1/sibling/read",
            json={"path": "/etc/passwd"},
        )

    assert response.status_code == 403
    body = response.json()
    assert body.get("reason_code") == SIBLING_NOT_ALLOWLISTED


def test_http_sibling_read_missing_path_returns_400(client) -> None:
    """POST /v1/sibling/read without path key returns 400."""
    response = client.post("/v1/sibling/read", json={"namespace_prefix": "ns"})
    assert response.status_code == 400


def test_http_sibling_read_uses_namespace_header(client, tmp_path: pathlib.Path) -> None:
    """X-Namespace-Prefix header is used when namespace_prefix not in body."""
    receipt = _make_receipt(tmp_path)

    captured_args: list = []

    def _capture(path, namespace_prefix=None, **kwargs):
        captured_args.append(namespace_prefix)
        return receipt

    with patch("ollarma.service.read_sibling_file", side_effect=_capture):
        response = client.post(
            "/v1/sibling/read",
            json={"path": str(tmp_path / "file.txt")},
            headers={"X-Namespace-Prefix": "header-ns"},
        )

    assert response.status_code == 200
    assert captured_args[0] == "header-ns"

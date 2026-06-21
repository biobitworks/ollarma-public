"""Tests for BearerAuthMiddleware (Phase 38 HARDEN-03)."""
import pytest
from unittest.mock import patch
from starlette.testclient import TestClient
from ollarma.http_api import app


def test_no_auth_token_env_allows_all():
    """When OLLARMA_AUTH_TOKEN is not set, all requests pass through."""
    with patch("ollarma.http_api._AUTH_TOKEN", None):
        client = TestClient(app)
        resp = client.get("/health")
    assert resp.status_code == 200


def test_auth_token_set_blocks_unauthenticated():
    """When token set, request without Authorization header returns 401."""
    with patch("ollarma.http_api._AUTH_TOKEN", "secret-token-123"):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized"


def test_auth_token_set_allows_correct_bearer():
    """When token set, request with correct Bearer token passes through."""
    with patch("ollarma.http_api._AUTH_TOKEN", "secret-token-123"):
        client = TestClient(app)
        resp = client.get("/health", headers={"Authorization": "Bearer secret-token-123"})
    assert resp.status_code == 200


def test_auth_token_wrong_bearer_401():
    """When token set, wrong Bearer token returns 401."""
    with patch("ollarma.http_api._AUTH_TOKEN", "correct-token"):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401

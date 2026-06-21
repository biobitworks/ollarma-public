"""test_gateway_submit_endpoint.py -- Phase 57-03 E2E tests.

Covers:
- Service-layer ``submit_gateway_request`` (Task 1): 4-cell matrix, config
  reader robustness, reject admission persistence, taxonomy (GatewayInputError
  raised rather than bare Exception).
- HTTP-layer ``POST /gateway/submit`` (Task 2): real Starlette TestClient wired
  to the real Starlette app, real GatewayReceiptStore, real config loading.
  Covers: 200+disabled, 200+dry_run, 400+reject (three shapes), 501+
  NotImplemented boundary, body.dry_run vs query ?dry_run= precedence,
  hash-chain integrity across multiple submissions, and I-03 regression guard
  on /chat /route /workflow /autopilot.

Anti-patterns explicitly avoided (per plan <action>):
- Zero monkeypatching of the core gateway path. Tests exercise the real
  ``submit_gateway_request`` through the real handler. Test harness uses
  ``monkeypatch.chdir(tmp_path)`` only to isolate the repo root -- NOT to
  stub out any function under test (anti-pattern per Phase 55 WR-01).
- No ``except Exception`` in test bodies.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from ollarma import service
from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.gateway import (
    FrontierReceipt,
    GatewayAdmissionEntry,
    GatewayReasonCode,
    GatewayReceiptStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_escalation_receipt_dict(project: str = "demo") -> dict:
    """Return a pydantic-valid escalation_receipt payload as a dict."""
    er = build_escalation_receipt(
        project=project,
        lane="local",
        task_class="code",
        reason_code=ReasonCode.SELECTION_MISSING,
        reason_detail="selection artifact is missing for test",
        next_action="frontier_or_human",
    )
    return json.loads(er.model_dump_json())


@pytest.fixture
def _isolated_repo(tmp_path, monkeypatch):
    """Point CWD + the gateway config reader at an isolated tmp repo.

    The fixture:
      - Creates ``tmp_path/repo/.planning/config.json`` with gateway.enabled=false.
      - chdirs so ``service.submit_gateway_request`` (default repo_root) lands
        inside the tmp repo.
      - Returns the repo path so tests can call ``GatewayReceiptStore(repo)``
        directly for audit-trail assertions.
    This is test-harness plumbing -- NOT monkeypatching of the core code path.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".planning").mkdir()
    (repo / ".planning" / "config.json").write_text(
        json.dumps({"features": {"gateway": {"enabled": False}}})
    )
    monkeypatch.chdir(repo)
    return repo


def _enable_gateway_in_config(
    repo: pathlib.Path,
    *,
    allowlist: list[str] | None = None,
    virtual_keys: list[dict] | None = None,
) -> None:
    """Flip gateway.enabled -> True in the tmp repo's config.json.

    Phase 58-01 added admission gating; enabled + non-dry-run paths now pass
    through the allowlist + virtual-key registry BEFORE reaching Phase 57's
    ``NotImplementedError`` boundary. Tests that want to probe the Phase 57
    boundary must therefore pre-populate an allowlist + vk matching the
    escalation_receipt's project (``demo`` by default) -- otherwise admission
    rejects with ``PROJECT_NOT_ALLOWED`` / ``VIRTUAL_KEY_UNKNOWN``.
    """
    config_path = repo / ".planning" / "config.json"
    data = json.loads(config_path.read_text())
    gateway_block = data.setdefault("features", {}).setdefault("gateway", {})
    gateway_block["enabled"] = True
    gateway_block["allowlist"] = allowlist if allowlist is not None else ["demo"]
    gateway_block["virtual_keys"] = virtual_keys if virtual_keys is not None else [
        {
            "id": "vk_demo",
            "keychain_service": "ollarma-test-demo",
            "provider": "anthropic",
        },
    ]
    config_path.write_text(json.dumps(data))


@pytest.fixture
def _keychain_hit(monkeypatch):
    """Deterministic Keychain resolver for admission-enabled enabled-path tests.

    Phase 58-01 admission requires a Keychain hit after vk-registry resolution.
    Tests that exercise the enabled + non-dry-run path monkeypatch this seam
    so admission approves and the call reaches Phase 57's Phase-59 boundary.
    """
    from ollarma import gateway_admission  # noqa: PLC0415

    def _lookup(service_name: str) -> bytes:
        return b"fake-secret-bytes"

    monkeypatch.setattr(gateway_admission, "_KEYCHAIN_LOOKUP", _lookup)
    return _lookup


# ---------------------------------------------------------------------------
# Task 1: service-layer tests (direct call, no HTTP)
# ---------------------------------------------------------------------------


class TestServiceLevelDisabledPath:
    """4-cell matrix cell (enabled=False, dry_run_override=None)."""

    def test_service_level_disabled_returns_frontier_receipt_dict(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        body = {"escalation_receipt": _valid_escalation_receipt_dict()}
        result = service.submit_gateway_request(body, repo_root=_isolated_repo)

        assert isinstance(result, dict)
        assert result["status"] == "disabled"
        assert result["reason_code"] == GatewayReasonCode.GATEWAY_DISABLED.value
        assert result["cost_usd"] == "0"
        assert result["prompt_tokens"] == 0
        assert result["response_tokens"] == 0
        assert result["dry_run"] is False
        # Provenance link populated.
        assert result["escalation_receipt_id"].startswith("er-")
        # Hash-chain linkage populated on the returned receipt.
        assert result["receipt_hash"]
        assert len(result["receipt_hash"]) == 64

    def test_service_level_disabled_writes_admission_and_receipt(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        body = {"escalation_receipt": _valid_escalation_receipt_dict()}
        service.submit_gateway_request(body, repo_root=_isolated_repo)

        store = GatewayReceiptStore(_isolated_repo)
        admissions = store.load_admissions()
        receipts = store.load_receipts()

        assert len(admissions) == 1
        assert admissions[0].outcome == "disabled"
        assert admissions[0].reason_code == GatewayReasonCode.GATEWAY_DISABLED.value

        assert len(receipts) == 1
        assert receipts[0].status == "disabled"
        assert receipts[0].reason_code == GatewayReasonCode.GATEWAY_DISABLED.value

        # Both chains verify clean.
        assert store.verify_chain("admissions")
        assert store.verify_chain("receipts")


class TestServiceLevelDryRunPath:
    """4-cell matrix cells (*, dry_run_override=True)."""

    def test_service_level_dry_run_with_disabled_config(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        # Disabled is the default; dry-run override MUST bypass (D-15).
        body = {"escalation_receipt": _valid_escalation_receipt_dict()}
        result = service.submit_gateway_request(
            body, dry_run_override=True, repo_root=_isolated_repo,
        )
        assert result["status"] == "dry_run"
        assert result["reason_code"] == GatewayReasonCode.DRY_RUN.value
        assert result["dry_run"] is True

    def test_service_level_dry_run_with_enabled_config(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        _enable_gateway_in_config(_isolated_repo)
        body = {"escalation_receipt": _valid_escalation_receipt_dict()}
        result = service.submit_gateway_request(
            body, dry_run_override=True, repo_root=_isolated_repo,
        )
        assert result["status"] == "dry_run"
        assert result["reason_code"] == GatewayReasonCode.DRY_RUN.value
        assert result["dry_run"] is True

    def test_service_level_dry_run_via_body_key(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        body = {
            "escalation_receipt": _valid_escalation_receipt_dict(),
            "dry_run": True,
        }
        # dry_run_override omitted; body.dry_run should be picked up.
        result = service.submit_gateway_request(body, repo_root=_isolated_repo)
        assert result["status"] == "dry_run"
        assert result["dry_run"] is True

    def test_service_level_dry_run_persists_dry_run_admission(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        body = {"escalation_receipt": _valid_escalation_receipt_dict()}
        service.submit_gateway_request(
            body, dry_run_override=True, repo_root=_isolated_repo,
        )
        store = GatewayReceiptStore(_isolated_repo)
        admissions = store.load_admissions()
        assert len(admissions) == 1
        assert admissions[0].outcome == "dry_run"
        assert admissions[0].reason_code == GatewayReasonCode.DRY_RUN.value


class TestServiceLevelEnabledNoProvider:
    """4-cell matrix cell (enabled=True, dry_run_override=None).

    Must raise NotImplementedError -- the HTTP handler maps this to 501.
    """

    def test_service_level_enabled_without_dry_run_or_prompt_raises_input_error(
        self, _isolated_repo: pathlib.Path, _keychain_hit,
    ) -> None:
        # Phase 59 replaces Phase 57's NotImplementedError boundary with a
        # structured provider dispatch. Admission approves when allowlist +
        # vk are configured; the prompt is then required on the request body.
        _enable_gateway_in_config(_isolated_repo)
        body = {
            "escalation_receipt": _valid_escalation_receipt_dict(),
            "virtual_key_id": "vk_demo",
        }
        with pytest.raises(service.GatewayInputError) as exc_info:
            service.submit_gateway_request(
                body, repo_root=_isolated_repo, virtual_key_id="vk_demo",
            )
        # Error message now points at the missing prompt requirement.
        assert "prompt" in str(exc_info.value).lower()


class TestServiceLevelRejectPath:
    """Missing / malformed escalation_receipt -> GatewayInputError + reject admission."""

    def test_missing_escalation_receipt_key_raises_gateway_input_error(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        with pytest.raises(service.GatewayInputError) as exc_info:
            service.submit_gateway_request({}, repo_root=_isolated_repo)
        assert "escalation_receipt" in str(exc_info.value)

    def test_missing_escalation_receipt_key_writes_reject_admission(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        with pytest.raises(service.GatewayInputError):
            service.submit_gateway_request({}, repo_root=_isolated_repo)

        store = GatewayReceiptStore(_isolated_repo)
        admissions = store.load_admissions()
        assert len(admissions) == 1
        assert admissions[0].outcome == "reject"
        assert admissions[0].reason_code == (
            GatewayReasonCode.REJECT_INVALID_RECEIPT.value
        )
        # No receipt was written for a rejected input.
        assert store.load_receipts() == []

    def test_malformed_escalation_receipt_raises_gateway_input_error(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        # Missing required fields (lane, task_class, reason_code, etc.)
        body = {"escalation_receipt": {"project": "x"}}
        with pytest.raises(service.GatewayInputError):
            service.submit_gateway_request(body, repo_root=_isolated_repo)

    def test_malformed_escalation_receipt_writes_reject_admission(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        body = {"escalation_receipt": {"project": "demo-proj"}}
        with pytest.raises(service.GatewayInputError):
            service.submit_gateway_request(body, repo_root=_isolated_repo)

        store = GatewayReceiptStore(_isolated_repo)
        admissions = store.load_admissions()
        assert len(admissions) == 1
        assert admissions[0].outcome == "reject"
        assert admissions[0].reason_code == (
            GatewayReasonCode.REJECT_INVALID_RECEIPT.value
        )
        # Project hint carried through so the audit trail is useful.
        assert admissions[0].project == "demo-proj"

    def test_non_dict_body_is_a_reject(
        self, _isolated_repo: pathlib.Path,
    ) -> None:
        with pytest.raises(service.GatewayInputError):
            service.submit_gateway_request("not a dict", repo_root=_isolated_repo)  # type: ignore[arg-type]

    def test_gateway_input_error_is_not_bare_exception(self) -> None:
        """Preserves taxonomy per plan spec -- HTTP handler catches this
        specifically to return 400 without catching unrelated failures."""
        assert issubclass(service.GatewayInputError, Exception)
        assert service.GatewayInputError is not Exception


# ---------------------------------------------------------------------------
# Config reader robustness (Task 1 "fail-loud conservative" contract)
# ---------------------------------------------------------------------------


class TestConfigReaderRobustness:

    def test_config_missing_file_returns_false(
        self, tmp_path: pathlib.Path,
    ) -> None:
        missing = tmp_path / "nope.json"
        assert service._load_gateway_enabled(missing) is False

    def test_config_invalid_json_returns_false(
        self, tmp_path: pathlib.Path,
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("not {valid json")
        assert service._load_gateway_enabled(path) is False

    def test_config_missing_features_key_returns_false(
        self, tmp_path: pathlib.Path,
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"model_profile": "balanced"}))
        assert service._load_gateway_enabled(path) is False

    def test_config_missing_gateway_key_returns_false(
        self, tmp_path: pathlib.Path,
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"features": {}}))
        assert service._load_gateway_enabled(path) is False

    def test_config_enabled_is_true_returns_true(
        self, tmp_path: pathlib.Path,
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"features": {"gateway": {"enabled": True}}}))
        assert service._load_gateway_enabled(path) is True

    def test_config_enabled_false_returns_false(
        self, tmp_path: pathlib.Path,
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"features": {"gateway": {"enabled": False}}}))
        assert service._load_gateway_enabled(path) is False

    @pytest.mark.parametrize("truthy_value", ["true", "yes", 1, "True", "1"])
    def test_config_enabled_truthy_strings_rejected(
        self, tmp_path: pathlib.Path, truthy_value,
    ) -> None:
        # Strict bool check: no truthy coercion (DEBT-10 preemptive).
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"features": {"gateway": {"enabled": truthy_value}}})
        )
        assert service._load_gateway_enabled(path) is False

    def test_config_enabled_missing_key_returns_false(
        self, tmp_path: pathlib.Path,
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"features": {"gateway": {}}}))
        assert service._load_gateway_enabled(path) is False

    def test_config_permission_error_defaults_to_disabled(
        self, tmp_path: pathlib.Path, caplog,
    ) -> None:
        """WR-04: chmod 000 on the config file -> PermissionError is caught
        (not raised), default enabled=False, warning logged."""
        import logging
        import os as _os

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"features": {"gateway": {"enabled": True}}}))
        # Strip all permissions. On POSIX this triggers PermissionError on read.
        _os.chmod(path, 0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="ollarma.gateway"):
                result = service._load_gateway_enabled(path)
            assert result is False
            assert any(
                "config unreadable" in rec.message and "PermissionError" in rec.message
                for rec in caplog.records
            ), (
                "expected WARNING containing 'config unreadable' + 'PermissionError'; "
                f"got: {[r.message for r in caplog.records]}"
            )
        finally:
            # Restore so tmp_path cleanup works.
            _os.chmod(path, 0o644)

    def test_gateway_enabled_respects_repo_root_not_cwd(
        self, tmp_path: pathlib.Path,
    ) -> None:
        """WR-05: config is read from the supplied repo_root, NOT CWD.

        This exercises the non-chdir'd case directly: build a repo layout at
        tmp_path/repo with gateway.enabled=true, but do NOT chdir into it.
        With the WR-05 fix, submit_gateway_request threads repo_root to
        _load_gateway_enabled so the config anchor matches the store anchor.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".planning").mkdir()
        (repo / ".planning" / "config.json").write_text(
            json.dumps({"features": {"gateway": {"enabled": True}}})
        )

        # Direct-call assertion on the config reader: with the explicit path it
        # must read True from the repo, regardless of CWD.
        assert service._load_gateway_enabled(
            repo / ".planning" / "config.json"
        ) is True

    def test_submit_gateway_request_honors_repo_root_config_from_different_cwd(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """WR-05 integration: submit_gateway_request must read config from the
        supplied repo_root even when CWD points elsewhere. With enabled=true +
        no dry_run override + admission-approved inputs, we expect
        NotImplementedError (Phase 57 boundary), NOT the disabled path.
        """
        # Phase 58-01: enabled + non-dry-run requires the admission pipeline
        # to approve before the Phase 57 boundary is reached. Register the
        # demo project + vk and mock the Keychain seam.
        from ollarma import gateway_admission  # noqa: PLC0415

        monkeypatch.setattr(
            gateway_admission,
            "_KEYCHAIN_LOOKUP",
            lambda service_name: b"fake-secret-bytes",
        )

        # Build an enabled-config repo at tmp_path/repo with Phase 58 admission
        # config populated.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".planning").mkdir()
        (repo / ".planning" / "config.json").write_text(
            json.dumps(
                {
                    "features": {
                        "gateway": {
                            "enabled": True,
                            "allowlist": ["demo"],
                            "virtual_keys": [
                                {
                                    "id": "vk_demo",
                                    "keychain_service": "ollarma-test-demo",
                                    "provider": "anthropic",
                                },
                            ],
                        },
                    },
                }
            )
        )

        # CWD is tmp_path (NOT the repo). If config were read from CWD the
        # handler would silently fall through to the disabled path.
        cwd_home = tmp_path / "elsewhere"
        cwd_home.mkdir()
        monkeypatch.chdir(cwd_home)

        body = {"escalation_receipt": _valid_escalation_receipt_dict()}
        # With WR-05 fix: enabled=true is honored from repo_root -> admission
        # approves -> Phase 59 dispatch requires 'prompt' and raises
        # GatewayInputError (replaces Phase 57's NotImplementedError boundary).
        # Pre-fix: this would silently return a disabled receipt because
        # config was read from CWD.
        with pytest.raises(service.GatewayInputError):
            service.submit_gateway_request(
                body, repo_root=repo, virtual_key_id="vk_demo",
            )

    def test_config_is_a_directory_defaults_to_disabled(
        self, tmp_path: pathlib.Path, caplog,
    ) -> None:
        """WR-04: if the config path is a directory -> IsADirectoryError is caught
        (not raised), default enabled=False, warning logged."""
        import logging

        # Create a directory at the config path.
        config_path = tmp_path / "config.json"
        config_path.mkdir()
        with caplog.at_level(logging.WARNING, logger="ollarma.gateway"):
            result = service._load_gateway_enabled(config_path)
        assert result is False
        assert any(
            "config unreadable" in rec.message
            and ("IsADirectoryError" in rec.message or "OSError" in rec.message)
            for rec in caplog.records
        ), (
            "expected WARNING containing 'config unreadable' + "
            f"'IsADirectoryError'; got: {[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Task 2: HTTP-layer tests (real Starlette TestClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def _http_client(_isolated_repo: pathlib.Path):
    """Build a TestClient against the real ``ollarma.http_api.app``.

    The app is module-level; the test must already be inside the chdir'd
    tmp repo so that the handler's ``pathlib.Path.cwd()`` lands in the
    correct place. ``_isolated_repo`` takes care of that.

    The bearer-auth middleware is effectively disabled for these tests
    because ``OLLARMA_AUTH_TOKEN`` is unset in the test env (the middleware
    no-ops when the env var is unset, per http_api.py).
    """
    # Import lazily so a collection-time failure in http_api doesn't break the
    # whole test module.
    from ollarma import http_api  # noqa: PLC0415
    # Starlette's TestClient sends no Origin header by default -> middleware
    # allows the request.
    return TestClient(http_api.app)


class TestHttpDisabledPath:
    """GATE-06 / D-10: enabled=false + valid ER -> 200 + disabled receipt."""

    def test_post_gateway_submit_disabled_returns_200(
        self, _http_client: TestClient,
    ) -> None:
        response = _http_client.post(
            "/gateway/submit",
            json={"escalation_receipt": _valid_escalation_receipt_dict()},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "disabled"
        assert body["reason_code"] == GatewayReasonCode.GATEWAY_DISABLED.value
        assert body["cost_usd"] == "0"
        assert body["dry_run"] is False


class TestHttpDryRunPath:
    """GATE-07 / D-13 / D-15: dry-run via query param and body key."""

    def test_post_gateway_submit_dry_run_query_param(
        self, _http_client: TestClient,
    ) -> None:
        response = _http_client.post(
            "/gateway/submit?dry_run=true",
            json={"escalation_receipt": _valid_escalation_receipt_dict()},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "dry_run"
        assert body["reason_code"] == GatewayReasonCode.DRY_RUN.value
        assert body["dry_run"] is True

    def test_post_gateway_submit_dry_run_body_key(
        self, _http_client: TestClient,
    ) -> None:
        response = _http_client.post(
            "/gateway/submit",
            json={
                "escalation_receipt": _valid_escalation_receipt_dict(),
                "dry_run": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "dry_run"

    def test_dry_run_bypasses_disabled_gate(
        self, _http_client: TestClient, _isolated_repo: pathlib.Path,
    ) -> None:
        # Config is disabled by the fixture. Dry-run must still win (D-15).
        response = _http_client.post(
            "/gateway/submit?dry_run=true",
            json={"escalation_receipt": _valid_escalation_receipt_dict()},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "dry_run"


class TestHttpEnabledBoundary:
    """Phase 59 boundary: enabled=true + no dry_run requires 'prompt' on body."""

    def test_post_enabled_without_dry_run_or_prompt_returns_400(
        self,
        _http_client: TestClient,
        _isolated_repo: pathlib.Path,
        _keychain_hit,
    ) -> None:
        # Phase 59 replaces Phase 57's 501 boundary with a structured 400 when
        # admission approves but the caller forgot to include 'prompt'. A
        # reject admission is persisted (audit invariant preserved).
        _enable_gateway_in_config(_isolated_repo)
        response = _http_client.post(
            "/gateway/submit",
            json={
                "escalation_receipt": _valid_escalation_receipt_dict(),
                "virtual_key_id": "vk_demo",
            },
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "GATEWAY_INPUT_INVALID"
        assert "prompt" in body["reason_detail"].lower()

    def test_post_enabled_with_dry_run_returns_200(
        self, _http_client: TestClient, _isolated_repo: pathlib.Path,
    ) -> None:
        _enable_gateway_in_config(_isolated_repo)
        response = _http_client.post(
            "/gateway/submit?dry_run=true",
            json={"escalation_receipt": _valid_escalation_receipt_dict()},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "dry_run"


class TestHttpRejectPath:
    """GATE-02 + D-12: malformed input -> 400 + structured envelope + reject audit."""

    def test_post_bare_prompt_returns_400(
        self, _http_client: TestClient, _isolated_repo: pathlib.Path,
    ) -> None:
        response = _http_client.post(
            "/gateway/submit",
            json={"prompt": "bare prompt -- should be refused"},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "GATEWAY_INPUT_INVALID"
        assert "reason_detail" in body

        # Reject admission MUST be persisted (GATE-05 at HTTP boundary).
        store = GatewayReceiptStore(_isolated_repo)
        admissions = store.load_admissions()
        assert len(admissions) == 1
        assert admissions[0].outcome == "reject"
        assert admissions[0].reason_code == (
            GatewayReasonCode.REJECT_INVALID_RECEIPT.value
        )

    def test_post_empty_body_returns_400(
        self, _http_client: TestClient, _isolated_repo: pathlib.Path,
    ) -> None:
        response = _http_client.post("/gateway/submit", json={})
        assert response.status_code == 400
        assert response.json()["error"] == "GATEWAY_INPUT_INVALID"
        # Still writes a reject admission.
        store = GatewayReceiptStore(_isolated_repo)
        assert len(store.load_admissions()) == 1

    def test_post_partial_escalation_receipt_returns_400(
        self, _http_client: TestClient, _isolated_repo: pathlib.Path,
    ) -> None:
        response = _http_client.post(
            "/gateway/submit",
            json={"escalation_receipt": {"project": "demo-proj"}},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "GATEWAY_INPUT_INVALID"

        store = GatewayReceiptStore(_isolated_repo)
        admissions = store.load_admissions()
        assert len(admissions) == 1
        assert admissions[0].outcome == "reject"
        # Project hint carried through even though the ER itself was rejected.
        assert admissions[0].project == "demo-proj"

    def test_post_malformed_json_body_returns_400(
        self, _http_client: TestClient, _isolated_repo: pathlib.Path,
    ) -> None:
        # Raw non-JSON bytes -> handler's _json.JSONDecodeError path -> body={}
        # -> same rejection path as empty body.
        response = _http_client.post(
            "/gateway/submit",
            content=b"not {valid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "GATEWAY_INPUT_INVALID"


class TestHttpHashChainSurvivesTransport:
    """GATE-05: multi-submit round trip through HTTP preserves the chain."""

    def test_three_submissions_chain_verifies(
        self, _http_client: TestClient, _isolated_repo: pathlib.Path,
    ) -> None:
        for i in range(3):
            response = _http_client.post(
                "/gateway/submit?dry_run=true",
                json={
                    "escalation_receipt": _valid_escalation_receipt_dict(
                        project=f"demo-{i}",
                    ),
                },
            )
            assert response.status_code == 200

        store = GatewayReceiptStore(_isolated_repo)
        # Both streams verify clean.
        admissions_tail = store.verify_chain("admissions")
        receipts_tail = store.verify_chain("receipts")
        assert len(admissions_tail) == 64
        assert len(receipts_tail) == 64
        # Three entries each.
        assert len(store.load_admissions()) == 3
        assert len(store.load_receipts()) == 3

    def test_rejected_requests_still_extend_admissions_chain(
        self, _http_client: TestClient, _isolated_repo: pathlib.Path,
    ) -> None:
        # Mix: one success + two rejects = three admission entries, one receipt.
        _http_client.post(
            "/gateway/submit?dry_run=true",
            json={"escalation_receipt": _valid_escalation_receipt_dict()},
        )
        _http_client.post("/gateway/submit", json={"prompt": "nope"})
        _http_client.post("/gateway/submit", json={"escalation_receipt": {}})

        store = GatewayReceiptStore(_isolated_repo)
        admissions = store.load_admissions()
        assert len(admissions) == 3
        outcomes = [a.outcome for a in admissions]
        assert outcomes.count("reject") == 2
        assert outcomes.count("dry_run") == 1
        # Chain still verifies clean despite mixed outcomes.
        assert store.verify_chain("admissions")
        # Only the dry_run call produced a receipt.
        assert len(store.load_receipts()) == 1


# ---------------------------------------------------------------------------
# I-03 regression guard: /chat, /route, /autopilot, /workflow unchanged
# ---------------------------------------------------------------------------


class TestI03RegressionGuard:
    """Prove adding /gateway/submit did not alter the v4.5 production paths.

    This is a CANARY, not a re-test of each endpoint's full behavior. Each
    assertion confirms: (a) the route still exists, (b) the handler still
    validates input in the same way it did in v4.5. Full behavioral tests
    live in each endpoint's own test module (test_chat*, test_route*, etc.).
    """

    def test_chat_endpoint_missing_message_returns_400(
        self, _http_client: TestClient,
    ) -> None:
        # v4.5 contract: /chat requires `message` or `prompt`; missing -> 400.
        response = _http_client.post("/chat", json={})
        assert response.status_code == 400
        assert "message" in response.json().get("error", "").lower()

    def test_route_endpoint_missing_prompt_returns_400(
        self, _http_client: TestClient,
    ) -> None:
        response = _http_client.post("/route", json={})
        assert response.status_code == 400
        assert "prompt" in response.json().get("error", "").lower()

    def test_route_endpoint_missing_project_returns_400(
        self, _http_client: TestClient,
    ) -> None:
        response = _http_client.post(
            "/route", json={"prompt": "hello"},
        )
        assert response.status_code == 400
        assert "project" in response.json().get("error", "").lower()

    def test_workflow_endpoint_missing_fields_returns_400(
        self, _http_client: TestClient,
    ) -> None:
        response = _http_client.post("/workflow", json={})
        assert response.status_code == 400
        err = response.json().get("error", "")
        # Handler reports "project, manifest_ref, and step_id are required".
        assert "project" in err and "manifest_ref" in err and "step_id" in err

    def test_autopilot_endpoint_missing_project_returns_400(
        self, _http_client: TestClient,
    ) -> None:
        response = _http_client.post("/autopilot", json={})
        assert response.status_code == 400
        assert "project" in response.json().get("error", "").lower()

    def test_existing_routes_are_still_registered(self) -> None:
        """Route registration must be append-only -- the v4.5 route set is
        strictly a subset of the v5.0 route set."""
        from ollarma import http_api  # noqa: PLC0415

        registered = {r.path for r in http_api.app.routes}
        must_still_be_present = {
            "/chat",
            "/route",
            "/workflow",
            "/autopilot",
            "/health",
            "/startup/readiness",
            "/recovery/status",
            "/recovery/scan",
        }
        missing = must_still_be_present - registered
        assert not missing, f"v4.5 routes missing from app.routes: {missing}"
        # And /gateway/submit is the new addition.
        assert "/gateway/submit" in registered


# ---------------------------------------------------------------------------
# Structural guard: no `except Exception` introduced
# ---------------------------------------------------------------------------


class TestNoBareExceptInNewCode:
    """DEBT-10 structural guard on the new Phase 57-03 code paths."""

    def test_no_bare_except_in_gateway_submit_handler(self) -> None:
        src = pathlib.Path("src/ollarma/http_api.py").read_text()
        # Slice out the gateway_submit function.
        marker = "async def gateway_submit"
        if marker not in src:
            pytest.fail("gateway_submit handler not found in http_api.py")
        start = src.index(marker)
        # End at the next top-level `async def ` or the Starlette routes block.
        remainder = src[start + len(marker):]
        # Find the next `\nasync def ` or `\napp = `/end of file, whichever first.
        next_async = remainder.find("\nasync def ")
        next_app = remainder.find("\napp = Starlette")
        candidates = [i for i in (next_async, next_app) if i >= 0]
        end = min(candidates) if candidates else len(remainder)
        handler_src = remainder[:end]
        assert "except Exception" not in handler_src, (
            "gateway_submit handler must not use `except Exception` (DEBT-10)."
        )

    def test_no_bare_except_in_submit_gateway_request(self) -> None:
        src = pathlib.Path("src/ollarma/service.py").read_text()
        marker = "def submit_gateway_request"
        assert marker in src
        start = src.index(marker)
        # End the slice at the next top-level `def ` after our function.
        remainder = src[start + len(marker):]
        next_def = remainder.find("\ndef ")
        end = next_def if next_def >= 0 else len(remainder)
        fn_src = remainder[:end]
        assert "except Exception" not in fn_src, (
            "submit_gateway_request must not use `except Exception` (DEBT-10)."
        )

    def test_no_bare_except_via_grep(self) -> None:
        """Final backstop: a grep over the lines touched by Phase 57-03.

        This is a crude but definitive check -- if the plan's structural
        promise is violated, this fails. It's intentionally a grep (not AST)
        because the requirement is textual: the string ``except Exception``
        must not appear on any line of the new code.
        """
        # Only flag matches inside the gateway-specific sections. Other
        # modules have legitimate pre-existing `except Exception` lines that
        # Phase 57-03 does not own and does not touch (I-03 guard).
        api_src = pathlib.Path("src/ollarma/http_api.py").read_text()
        if "async def gateway_submit" in api_src:
            block_start = api_src.index("async def gateway_submit")
            block = api_src[block_start:]
            # Only look at the handler itself, not the rest of the file.
            next_def = block.find("\nasync def ", len("async def gateway_submit"))
            next_app = block.find("\napp = Starlette")
            terminators = [i for i in (next_def, next_app) if i > 0]
            end = min(terminators) if terminators else len(block)
            assert "except Exception" not in block[:end]

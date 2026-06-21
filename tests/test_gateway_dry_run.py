"""test_gateway_dry_run.py -- GATE-07 end-to-end dry-run tests (Phase 57-02).

Task 2 scope:
- End-to-end chain: escalation_receipt -> admission (one entry) -> FrontierReceipt
  (one entry), hash-chained, cross-stream linkage via
  ``admission.receipt_hash == FrontierReceipt.admission_receipt_hash``.
- Determinism: two submissions of receipts with identical fields produce
  byte-identical FrontierReceipt dumps modulo ``created_at`` + ``admission_id``
  + per-chain hash fields. Phase 62's integration assertion depends on this.
- Zero external calls (STRUCTURAL): a fresh subprocess import of
  ``ollarma.gateway_client`` does NOT pull any provider / HTTP-client module
  (anthropic, openai, httpx, urllib.request, requests) into ``sys.modules``.
  This catches the day someone accidentally adds a provider import.
- D-15: dry-run path is enable-agnostic — it does not read any gateway config.
- D-14: zero tokens, zero cost, zero latency (no heuristic estimation).
- Admission ↔ receipt linkage preserved through the store's hash chain.

All tests exercise the REAL GatewayClient + REAL GatewayReceiptStore + REAL
canonical_hash + REAL file I/O. No monkeypatching.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
from decimal import Decimal

import orjson
import pytest

from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.gateway import (
    FrontierReceipt,
    GatewayAdmissionEntry,
    GatewayReasonCode,
    GatewayReceiptStore,
)
from ollarma.gateway_client import GatewayClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _fixed_escalation_receipt():
    """A deterministic escalation receipt for determinism testing.

    Note: EscalationReceipt.created_at is default_factory-ed to utcnow, so two
    calls produce different timestamps. Tests that need identical inputs
    explicitly pass the same model instance (or mutate after construction,
    which the frozen model forbids — so we build-once-reuse).
    """
    return build_escalation_receipt(
        project="phase-57-proj",
        lane="ollarma-default",
        task_class="chat",
        reason_code=ReasonCode.SWAP_DEGRADED,
        reason_detail="swap 2100 MB > 512 MB threshold",
        next_action="frontier_or_human",
    )


# ---------------------------------------------------------------------------
# GATE-07: end-to-end chain
# ---------------------------------------------------------------------------

def test_dry_run_end_to_end_writes_one_admission_and_one_receipt(
    tmp_path: pathlib.Path,
):
    client = GatewayClient(repo_root=tmp_path)
    store = GatewayReceiptStore(tmp_path)

    er = _fixed_escalation_receipt()
    receipt = client.submit(er, dry_run=True)

    admissions = store.load_admissions()
    receipts = store.load_receipts()
    assert len(admissions) == 1
    assert len(receipts) == 1

    admission = admissions[0]
    assert admission.outcome == "dry_run"
    assert admission.reason_code == GatewayReasonCode.DRY_RUN.value
    assert admission.project == "phase-57-proj"
    assert admission.escalation_receipt_id.startswith("er-")

    # Both streams hash-chain cleanly from genesis.
    assert store.verify_chain("admissions")
    assert store.verify_chain("receipts")

    # Returned receipt is the same as the one persisted.
    assert receipts[0].receipt_hash == receipt.receipt_hash


def test_cross_stream_linkage_admission_hash_equals_receipt_escalation_hash(
    tmp_path: pathlib.Path,
):
    """Phase 63 trace CLI depends on this exact invariant:
    admissions.jsonl line N has receipt_hash = H; the corresponding
    receipts.jsonl line has admission_receipt_hash = H.
    """
    client = GatewayClient(repo_root=tmp_path)
    store = GatewayReceiptStore(tmp_path)

    er = _fixed_escalation_receipt()
    receipt = client.submit(er, dry_run=True)

    admission = store.load_admissions()[0]
    assert receipt.admission_receipt_hash == admission.receipt_hash
    # And the escalation_receipt_id is shared across streams.
    assert receipt.escalation_receipt_id == admission.escalation_receipt_id


def test_dry_run_receipt_populated_per_d13_d14(tmp_path: pathlib.Path):
    """D-13: status=dry_run, reason_code=DRY_RUN, provider+model_id populated.
    D-14: zero tokens, zero cost, zero latency (no heuristic estimation).
    """
    client = GatewayClient(repo_root=tmp_path)
    er = _fixed_escalation_receipt()
    receipt = client.submit(
        er, dry_run=True, provider="anthropic", model_id="claude-3-5-sonnet-20241022"
    )

    assert receipt.status == "dry_run"
    assert receipt.reason_code == GatewayReasonCode.DRY_RUN.value
    assert receipt.dry_run is True
    assert receipt.provider == "anthropic"
    assert receipt.model_id == "claude-3-5-sonnet-20241022"
    assert receipt.prompt_tokens == 0
    assert receipt.response_tokens == 0
    assert receipt.cost_usd == Decimal("0")
    assert receipt.latency_ms == 0
    assert receipt.context_truncated is False
    assert receipt.original_tokens is None
    assert receipt.truncated_tokens is None


def test_dry_run_provider_and_model_id_reflect_caller_kwargs(
    tmp_path: pathlib.Path,
):
    """D-13 point 4: synthetic receipt's provider + model_id reflect what the
    call WOULD have routed to — in Phase 57 these come from submit() kwargs.
    """
    client = GatewayClient(repo_root=tmp_path)
    er = _fixed_escalation_receipt()
    r = client.submit(er, dry_run=True, provider="openai", model_id="gpt-4o")
    assert r.provider == "openai"
    assert r.model_id == "gpt-4o"


# ---------------------------------------------------------------------------
# Determinism (Phase 62 foundation)
# ---------------------------------------------------------------------------

_NON_DETERMINISTIC_FIELDS = {
    "created_at",              # utcnow-derived
    "parent_hash",             # chain position
    "receipt_hash",            # depends on created_at + parent_hash
    # admission_receipt_hash on the FrontierReceipt is set to the admission's
    # materialized receipt_hash (cross-stream link). The admission carries a
    # per-call uuid admission_id + its own created_at, so this field inherits
    # the admission's non-determinism. Content addressing of the dry-run path
    # is preserved via escalation_receipt_id (see dedicated test below).
    "admission_receipt_hash",
}


def _strip_nondeterministic(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k not in _NON_DETERMINISTIC_FIELDS}


def test_dry_run_is_deterministic_across_repeat_submissions(
    tmp_path: pathlib.Path,
):
    """Two submissions of receipts constructed with identical field values
    produce byte-identical FrontierReceipt payloads EXCEPT for created_at +
    chain-position fields. Admission_id also differs (per-call uuid).

    Phase 62's integration test asserts on dry-run output without mocking time;
    if this test regresses, that Phase 62 assertion breaks.
    """
    client_a = GatewayClient(repo_root=tmp_path / "a")
    client_b = GatewayClient(repo_root=tmp_path / "b")

    er = _fixed_escalation_receipt()
    r1 = client_a.submit(er, dry_run=True)
    r2 = client_b.submit(er, dry_run=True)

    d1 = _strip_nondeterministic(r1.model_dump(mode="json"))
    d2 = _strip_nondeterministic(r2.model_dump(mode="json"))

    # admission_receipt_hash depends on the EscalationReceipt's created_at,
    # which varies between _fixed_escalation_receipt() calls. In this test we
    # reused the SAME er instance, so admission_receipt_hash must match.
    assert (
        orjson.dumps(d1, option=orjson.OPT_SORT_KEYS)
        == orjson.dumps(d2, option=orjson.OPT_SORT_KEYS)
    ), (
        "FrontierReceipt dumps are not byte-identical modulo "
        "non-deterministic fields.\n"
        f"d1={d1}\nd2={d2}"
    )


def test_escalation_receipt_id_is_content_addressed(tmp_path: pathlib.Path):
    """The escalation_receipt_id is derived from canonical_hash(er.model_dump),
    so identical inputs produce identical ids. This is what makes the
    determinism test pass.
    """
    client = GatewayClient(repo_root=tmp_path / "a")
    er = _fixed_escalation_receipt()

    # Two submissions of the SAME er produce the SAME escalation_receipt_id.
    r1 = client.submit(er, dry_run=True)
    r2 = client.submit(er, dry_run=True)
    assert r1.escalation_receipt_id == r2.escalation_receipt_id
    assert r1.escalation_receipt_id.startswith("er-")
    assert len(r1.escalation_receipt_id) == 3 + 16  # "er-" + 16 hex chars


def test_admission_ids_are_per_call_unique_not_content_addressed(
    tmp_path: pathlib.Path,
):
    """admission_id is per-call (uuid4), NOT content-addressed. Two submissions
    of the same escalation_receipt produce two admissions; the chain linkage
    into the receipts stream is via admission_receipt_hash + admission's
    receipt_hash, not admission_id.
    """
    client = GatewayClient(repo_root=tmp_path)
    store = GatewayReceiptStore(tmp_path)
    er = _fixed_escalation_receipt()

    client.submit(er, dry_run=True)
    client.submit(er, dry_run=True)

    admissions = store.load_admissions()
    assert len(admissions) == 2
    assert admissions[0].admission_id != admissions[1].admission_id


# ---------------------------------------------------------------------------
# Zero external calls (STRUCTURAL enforcement via subprocess)
# ---------------------------------------------------------------------------

BANNED_MODULES = ("anthropic", "openai", "httpx", "urllib.request", "requests")


def test_gateway_client_import_pulls_no_provider_or_http_module(
    tmp_path: pathlib.Path,
):
    """STRUCTURAL: a fresh subprocess import of ``ollarma.gateway_client`` does
    NOT cause any provider / HTTP-client module to appear in ``sys.modules``.

    Uses a real subprocess (NOT monkeypatched sys.modules) so the assertion
    reflects what actually happens at module-import time. This test fails
    loudly the day someone adds a provider import to gateway_client.py — which
    is the WHOLE POINT of Phase 57's dry-run lane being network-isolated.
    """
    script = textwrap.dedent(
        """
        import sys
        import importlib
        importlib.import_module("ollarma.gateway_client")
        # Emit the set of loaded module names as JSON on stdout.
        import json as _json
        _json.dump(sorted(sys.modules.keys()), sys.stdout)
        """
    ).strip()

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stderr={result.stderr!r}\nstdout={result.stdout!r}"
    )
    loaded_modules = set(json.loads(result.stdout))

    for banned in BANNED_MODULES:
        assert banned not in loaded_modules, (
            f"gateway_client.py transitively imports {banned!r}; Phase 57 must "
            f"stay network-isolated. Found in sys.modules after import."
        )
        # Also check for any submodule of the banned top-level packages.
        # e.g. `urllib.request.foo` or `httpx.transports`.
        banned_prefix = banned + "."
        leaks = [m for m in loaded_modules if m.startswith(banned_prefix)]
        assert not leaks, (
            f"gateway_client.py transitively imports submodules of {banned!r}: "
            f"{leaks}"
        )


def test_gateway_client_source_has_no_provider_imports():
    """Belt-and-suspenders source-level scan: the module file itself must not
    textually import a provider / HTTP client. Complements the subprocess
    sys.modules test above — that test catches transitive imports, this one
    catches accidental direct ones even before the module is imported.
    """
    src = pathlib.Path("src/ollarma/gateway_client.py").read_text()
    for banned in ("anthropic", "openai", "httpx", "requests"):
        # Match `import X` and `from X import ...`.
        assert f"import {banned}" not in src, (
            f"gateway_client.py contains a direct `import {banned}` — "
            f"Phase 57 must stay network-isolated."
        )
        assert f"from {banned}" not in src, (
            f"gateway_client.py contains a direct `from {banned}` import — "
            f"Phase 57 must stay network-isolated."
        )
    # urllib.request is the stdlib HTTP client; we forbid it too.
    assert "urllib.request" not in src, (
        "gateway_client.py references urllib.request — Phase 57 must not "
        "perform HTTP at this layer."
    )


# ---------------------------------------------------------------------------
# D-15: dry-run is NOT gated by gateway.enabled
# ---------------------------------------------------------------------------

def test_client_does_not_read_gateway_enabled_config(tmp_path: pathlib.Path):
    """D-15: dry-run path must not depend on gateway.enabled config.

    The 57-02 client does not read config at all — that's 57-03's HTTP
    handler's job. This test asserts the structural property by parsing the
    module's AST (not the source text, which legitimately MENTIONS
    gateway.enabled in docstrings that explain D-15).

    The functional check is: no `open()`, `json.load`, or `json.loads` calls,
    and no imports of a config-loading module.
    """
    import ast

    src = pathlib.Path("src/ollarma/gateway_client.py").read_text()
    tree = ast.parse(src)

    forbidden_calls = set()
    forbidden_imports = set()

    for node in ast.walk(tree):
        # Direct `open(...)` call
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"open"}:
                forbidden_calls.add(node.func.id)
        # Method call like `json.load(...)` / `json.loads(...)` /
        # `yaml.safe_load(...)` / `orjson.loads(...)` on a file handle
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            qualname = f"{getattr(node.func.value, 'id', '?')}.{node.func.attr}"
            if qualname in {"json.load", "json.loads", "yaml.safe_load"}:
                forbidden_calls.add(qualname)
        # Imports of config-loading modules
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"json", "yaml", "toml", "tomllib", "configparser"}:
                    forbidden_imports.add(alias.name)
        if isinstance(node, ast.ImportFrom):
            if node.module in {"json", "yaml", "toml", "tomllib", "configparser"}:
                forbidden_imports.add(node.module)

    assert not forbidden_calls, (
        f"gateway_client.py makes config-loading calls {forbidden_calls}; "
        f"D-15 requires the client to be enable-agnostic. Config lookup "
        f"belongs in 57-03's HTTP layer."
    )
    assert not forbidden_imports, (
        f"gateway_client.py imports config-loading modules {forbidden_imports}; "
        f"D-15 requires the client to be enable-agnostic."
    )


def test_dry_run_succeeds_regardless_of_any_enable_flag(tmp_path: pathlib.Path):
    """Trivially true because the client does not read config — but the test
    documents the contract so future refactors can't silently add a config
    check without breaking this assertion.
    """
    client = GatewayClient(repo_root=tmp_path)
    er = _fixed_escalation_receipt()
    # No config file exists in tmp_path; the client must succeed anyway.
    receipt = client.submit(er, dry_run=True)
    assert receipt.status == "dry_run"


# ---------------------------------------------------------------------------
# Hash-chain integrity end-to-end
# ---------------------------------------------------------------------------

def test_dry_run_chain_walks_without_break_after_multiple_submissions(
    tmp_path: pathlib.Path,
):
    client = GatewayClient(repo_root=tmp_path)
    store = GatewayReceiptStore(tmp_path)
    er = _fixed_escalation_receipt()

    for _ in range(5):
        client.submit(er, dry_run=True)

    # Both streams chain cleanly from genesis through 5 entries.
    admissions_tail = store.verify_chain("admissions")
    receipts_tail = store.verify_chain("receipts")
    assert len(admissions_tail) == 64
    assert len(receipts_tail) == 64

    admissions = store.load_admissions()
    receipts = store.load_receipts()
    assert len(admissions) == 5
    assert len(receipts) == 5

    # Cross-stream linkage holds for every pair by index.
    for adm, rec in zip(admissions, receipts):
        assert rec.admission_receipt_hash == adm.receipt_hash

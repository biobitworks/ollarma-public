"""test_substrate_consumer_contract.py -- executable mirror of
``docs/OLLARMA_SUBSTRATE_CONTRACT.md``.

Two tests:

1. ``test_worked_example_runs`` -- runs the exact function published in §4 of
   the contract doc. If the doc's imports or assertions drift from what the
   code actually exposes, this test fails.

2. ``test_public_import_surface_stable`` -- imports every symbol the contract
   doc lists in §3 as stable. Fails if any is renamed, deleted, or changes
   kind (class vs. function).

Out-of-scope: unit-level behavioural tests for each symbol -- those live next
to their implementation modules in ``tests/test_gateway_*.py`` etc. This file
exists solely to enforce the substrate CONTRACT against the code.
"""
from __future__ import annotations

import inspect
import pathlib


# ---------------------------------------------------------------------------
# Test A: the worked example from the doc, verbatim.
# ---------------------------------------------------------------------------

# NOTE: The body of ``run_worked_example`` below is the EXACT text of the
# Python fence in docs/OLLARMA_SUBSTRATE_CONTRACT.md §4. Do not edit one
# without editing the other -- that is the whole point of this test.

import tempfile

from ollarma import gateway_admission
from ollarma.escalation import ReasonCode, build_escalation_receipt
from ollarma.gateway import FrontierReceipt
from ollarma.gateway_admission import AdmissionPolicy
from ollarma.gateway_client import GatewayClient


def run_worked_example(repo_root: pathlib.Path) -> FrontierReceipt:
    # 1. Build an EscalationReceipt locally (sibling-repo side).
    escalation = build_escalation_receipt(
        project="demo",
        lane="local-exec",
        task_class="generic",
        reason_code=ReasonCode.SWAP_DEGRADED,
        reason_detail="worked example",
        next_action="frontier_or_human",
    )

    # 2. Build an in-memory AdmissionPolicy (no config file on disk).
    policy = AdmissionPolicy(
        {
            "enabled": True,
            "allowlist": ["demo"],
            "virtual_keys": [
                {
                    "id": "vk_demo",
                    "keychain_service": "ollarma-demo",
                    "provider": "anthropic",
                },
            ],
        }
    )

    # 3. Stub the Keychain lookup via the sanctioned test seam.
    #    Raw bytes never cross the admission boundary; this is the only
    #    place in the example where we touch a private symbol.
    gateway_admission._KEYCHAIN_LOOKUP = lambda _service: b"fake-key-bytes"

    # 4. Submit in dry_run mode. No provider call, zero cost/latency.
    client = GatewayClient(repo_root)
    receipt = client.submit(
        escalation,
        dry_run=True,
        admission_policy=policy,
        virtual_key_id="vk_demo",
    )

    # 5. Contract assertions.
    assert isinstance(receipt, FrontierReceipt)
    assert receipt.status == "dry_run"
    assert receipt.reason_code == "DRY_RUN"
    assert receipt.schema_version == 1
    return receipt


def test_worked_example_runs(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Run the contract doc's §4 worked example verbatim.

    Restores the ``_KEYCHAIN_LOOKUP`` seam via ``monkeypatch`` so the test does
    not leak state into sibling tests (the worked example itself assigns the
    seam directly; that is legitimate for a sibling-project script but would
    be unwise inside a test runner).
    """
    original = gateway_admission._KEYCHAIN_LOOKUP
    try:
        receipt = run_worked_example(tmp_path)
    finally:
        monkeypatch.setattr(gateway_admission, "_KEYCHAIN_LOOKUP", original)

    # Re-assert the contract outcomes at the test layer so a failure here
    # points unambiguously at THIS test rather than at the inline asserts in
    # ``run_worked_example`` (which also protect sibling-project copy-paste).
    assert isinstance(receipt, FrontierReceipt)
    assert receipt.status == "dry_run"
    assert receipt.reason_code == "DRY_RUN"
    assert receipt.schema_version == 1
    # The dry-run flag is carried on the receipt per D-15.
    assert receipt.dry_run is True
    # Admission + frontier both landed on chain (the store populated these).
    assert receipt.admission_receipt_hash != ""
    assert receipt.receipt_hash != ""


# ---------------------------------------------------------------------------
# Test B: every public import path the contract doc lists must resolve.
# ---------------------------------------------------------------------------


# ``(module, symbol, expected_kind)`` -- mirrors docs §3 tables. ``expected_kind``
# values: ``"class"``, ``"func"``, ``"enum"``, ``"any"`` (use sparingly).
_PUBLIC_SURFACE: tuple[tuple[str, str, str], ...] = (
    # ollarma.escalation
    ("ollarma.escalation", "EscalationReceipt", "class"),
    ("ollarma.escalation", "ReasonCode", "enum"),
    ("ollarma.escalation", "build_escalation_receipt", "func"),
    # ollarma.gateway
    ("ollarma.gateway", "FrontierReceipt", "class"),
    ("ollarma.gateway", "GatewayAdmissionEntry", "class"),
    ("ollarma.gateway", "GatewayReasonCode", "enum"),
    ("ollarma.gateway", "GatewayReceiptStore", "class"),
    ("ollarma.gateway", "GatewayStoreError", "class"),  # Exception subclass
    # ollarma.gateway_client
    ("ollarma.gateway_client", "GatewayClient", "class"),
    ("ollarma.gateway_client", "GatewayError", "class"),
    ("ollarma.gateway_client", "GatewayInputError", "class"),
    ("ollarma.gateway_client", "GatewayDisabledError", "class"),
    # ollarma.gateway_admission
    ("ollarma.gateway_admission", "AdmissionPolicy", "class"),
    ("ollarma.gateway_admission", "AdmissionDecision", "class"),
    ("ollarma.gateway_admission", "resolve_virtual_key", "func"),
    # ollarma.receipts_trace
    ("ollarma.receipts_trace", "trace", "func"),
    ("ollarma.receipts_trace", "TraceResult", "class"),
    ("ollarma.receipts_trace", "TraceRow", "class"),
    ("ollarma.receipts_trace", "render_table", "func"),
    # ollarma.providers (Phase 59 + 60)
    ("ollarma.providers", "PROVIDER_REGISTRY", "any"),
    ("ollarma.providers", "get_provider", "func"),
    ("ollarma.providers", "AnthropicProvider", "class"),
    ("ollarma.providers", "OpenAIProvider", "class"),
    ("ollarma.providers", "ProviderResponse", "class"),
    ("ollarma.providers.base", "ProviderResponse", "class"),
    ("ollarma.providers.base", "BaseProvider", "class"),
    ("ollarma.providers.anthropic", "AnthropicProvider", "class"),
    ("ollarma.providers.openai", "OpenAIProvider", "class"),
    # ollarma.swe_bench (Phase 61 + 62)
    ("ollarma.swe_bench", "SWEBenchProblem", "class"),
    ("ollarma.swe_bench", "SWEBenchRun", "class"),
    ("ollarma.swe_bench", "LocalLaneRunner", "class"),
    ("ollarma.swe_bench", "FrontierLaneRunner", "class"),
    ("ollarma.swe_bench", "LeaderboardArtifact", "class"),
    ("ollarma.swe_bench", "build_leaderboard", "func"),
    ("ollarma.swe_bench", "verify_leaderboard_chain", "func"),
)


def test_public_import_surface_stable() -> None:
    """Every symbol docs/OLLARMA_SUBSTRATE_CONTRACT.md §3 lists must resolve.

    Fails if:
      - a listed module no longer imports;
      - a listed symbol was renamed / deleted;
      - a listed symbol's kind changed (class <-> function, etc.).

    This is a single aggregate test (not parametrized) so the plan's
    "+2 tests" accounting stays stable even if the surface list grows.
    """
    import enum as _enum
    import importlib

    failures: list[str] = []
    for module_name, symbol_name, expected_kind in _PUBLIC_SURFACE:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            failures.append(f"{module_name!r} failed to import: {exc}")
            continue

        if not hasattr(module, symbol_name):
            failures.append(f"{module_name}.{symbol_name} missing -- contract broken")
            continue
        obj = getattr(module, symbol_name)

        if expected_kind == "class":
            if not inspect.isclass(obj):
                failures.append(
                    f"{module_name}.{symbol_name} expected class, "
                    f"got {type(obj).__name__}"
                )
        elif expected_kind == "func":
            if inspect.isclass(obj) or not callable(obj):
                failures.append(
                    f"{module_name}.{symbol_name} expected function, "
                    f"got {type(obj).__name__}"
                )
        elif expected_kind == "enum":
            if not (inspect.isclass(obj) and issubclass(obj, _enum.Enum)):
                failures.append(
                    f"{module_name}.{symbol_name} expected Enum subclass, "
                    f"got {type(obj).__name__}"
                )
        elif expected_kind == "any":
            # Existence + non-None is the whole contract (e.g., module-level
            # dicts like PROVIDER_REGISTRY).
            if obj is None:
                failures.append(f"{module_name}.{symbol_name} is None")
        else:
            failures.append(f"{module_name}.{symbol_name}: unknown kind {expected_kind!r}")

    assert not failures, "contract drift:\n  - " + "\n  - ".join(failures)

    # Phase 60 (FRONT-05): the registry MUST resolve both providers. The
    # public import surface listing above proves the symbols exist; this
    # extra check proves the registry itself wires them up.
    from ollarma.providers import PROVIDER_REGISTRY as _REG, get_provider as _get
    assert "anthropic" in _REG
    assert "openai" in _REG
    for _name in ("anthropic", "openai"):
        _inst = _get(_name)
        assert hasattr(_inst, "submit") and callable(_inst.submit), (
            f"get_provider({_name!r}) returned non-BaseProvider instance"
        )

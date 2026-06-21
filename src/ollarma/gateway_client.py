"""gateway_client.py -- GatewayClient abstraction for the Phase 57 frontier
gateway.

Phase 57 scope:
- GatewayClient.submit(escalation_receipt, dry_run=True) -> FrontierReceipt
  (dry-run lane fully implemented; real-provider lane lands in Phase 59).
- Narrow exception taxonomy: GatewayError base + GatewayInputError +
  GatewayDisabledError. Zero bare ``except Exception`` per DEBT-10.
- Network isolation: this module does NOT import any HTTP / provider client.
  The structural "zero external calls" test in tests/test_gateway_dry_run.py
  enforces this at the module-import boundary via a subprocess.

Design bindings to ``.planning/phases/57-gateway-core-foundations/57-CONTEXT.md``
- D-03   admissions stream persists rejections (fail-closed audit trail)
- D-13   dry-run synthesizes a populated FrontierReceipt without a provider call
- D-14   zero usage / zero cost / zero latency — no heuristic estimation
- D-15   dry-run is NOT gated by gateway.enabled (this module does not read config)

Phase 57 → 59 boundary
----------------------
The ``dry_run=False`` branch raises ``NotImplementedError`` loudly. v4.5's
DEBT-10 retro explicitly flagged silent fallback as an anti-pattern; a stub
receipt for a real-provider call would be exactly that class of bug.
"""
from __future__ import annotations

import pathlib
from decimal import Decimal
from typing import ClassVar
from uuid import uuid4

from ollarma.escalation import EscalationReceipt, ReasonCode
from ollarma.evidence import canonical_hash
from ollarma.gateway import (
    FrontierReceipt,
    GatewayAdmissionEntry,
    GatewayReasonCode,
    GatewayReceiptStore,
)
from ollarma.gateway_admission import AdmissionPolicy, resolve_virtual_key

# NOTE: ``ollarma.providers.*`` and ``ollarma.providers.base`` pull in ``httpx``,
# and the Phase 57 invariant is that importing ``gateway_client`` alone loads
# NO network / HTTP module (see ``tests/test_gateway_dry_run.py``
# ``test_gateway_client_import_pulls_no_provider_or_http_module``). All
# provider imports in this module are therefore lazy, inside the
# ``submit()``/``_submit_provider`` code path that only fires when a caller
# opts into a non-dry-run provider dispatch. Use ``TYPE_CHECKING`` for the
# annotation types so static type checkers still see the right shapes.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ollarma.providers.base import BaseProvider, ProviderResponse  # noqa: F401


__all__ = [
    "GatewayClient",
    "GatewayError",
    "GatewayInputError",
    "GatewayDisabledError",
]


# ---------------------------------------------------------------------------
# Narrow exception taxonomy (DEBT-10)
# ---------------------------------------------------------------------------

class GatewayError(Exception):
    """Base class for all gateway-originated errors.

    Never raised directly. Subclasses carry a structured ``reason_code`` so
    callers (HTTP layer in 57-03, future rate-cap rejections in Phase 58,
    provider failures in Phase 59) can map to HTTP codes without
    re-classifying strings.
    """

    reason_code: ClassVar[str] = ""


class GatewayInputError(GatewayError):
    """Raised when input to ``GatewayClient.submit`` is not a valid
    ``EscalationReceipt`` instance.

    Maps to HTTP 400 at the transport layer (57-03). Distinct from
    ``GatewayDisabledError``, which is not an error but a well-formed call
    against a disabled feature (D-10).
    """

    reason_code: ClassVar[str] = GatewayReasonCode.REJECT_INVALID_RECEIPT.value


class GatewayDisabledError(GatewayError):
    """Declared here for taxonomy completeness.

    Not raised in Phase 57-02 (the client is enable-agnostic per D-15). Phase
    58 raises it on rate-cap exhaustion paths; 57-03's HTTP handler imports it
    from this single location so downstream phases extend the taxonomy without
    reopening this module.
    """

    reason_code: ClassVar[str] = GatewayReasonCode.GATEWAY_DISABLED.value


# ---------------------------------------------------------------------------
# GatewayClient
# ---------------------------------------------------------------------------

# Phase 57 defaults for the dry-run synthesizer's provider identity. These
# reflect "what the call WOULD have routed to" per D-13 point 4; Phase 58
# derives them from a routing rule + virtual-key registry.
_DEFAULT_PROVIDER: str = "anthropic"
_DEFAULT_MODEL_ID: str = "claude-3-5-sonnet-20241022"


class GatewayClient:
    """Single ingress point for the Phase 57 frontier gateway.

    In Phase 57 only the dry-run lane is implemented; the real-provider lane
    raises ``NotImplementedError`` loudly (Phase 59 fills it in).
    """

    def __init__(
        self,
        repo_root: pathlib.Path,
        *,
        store: GatewayReceiptStore | None = None,
    ) -> None:
        self._repo_root = pathlib.Path(repo_root)
        self._store = store if store is not None else GatewayReceiptStore(self._repo_root)

    # -- Public API -----------------------------------------------------------

    def submit(
        self,
        escalation_receipt: EscalationReceipt,
        *,
        dry_run: bool = True,
        provider: str = _DEFAULT_PROVIDER,
        model_id: str = _DEFAULT_MODEL_ID,
        admission_policy: AdmissionPolicy | None = None,
        virtual_key_id: str | None = None,
        prompt: str | None = None,
        provider_adapter: "BaseProvider | None" = None,
        virtual_key_bytes: bytes | None = None,
    ) -> FrontierReceipt:
        """Submit an escalation receipt to the gateway.

        Parameters
        ----------
        escalation_receipt :
            Must be an ``EscalationReceipt`` instance. Raw dicts / strings /
            ``None`` raise ``GatewayInputError`` — the HTTP boundary in 57-03
            parses dicts into the model before calling this method (two layers
            of defense per T-57-02-02).
        dry_run :
            When True (Phase 57 default), synthesizes a ``FrontierReceipt`` with
            ``status="dry_run"`` and zero usage/cost/latency without any
            provider call. When False, raises ``NotImplementedError`` (Phase 59
            replaces this with the Anthropic adapter).
        provider, model_id :
            Populate the synthesized receipt's provider identity per D-13 —
            "what the call WOULD have routed to". Phase 58 derives these from a
            routing rule; Phase 57 accepts them as explicit kwargs.

        Returns
        -------
        FrontierReceipt
            The materialized receipt (with ``parent_hash`` + ``receipt_hash``
            populated by the store on append).

        Raises
        ------
        GatewayInputError
            The input was not an ``EscalationReceipt`` instance. A reject
            admission entry is written to ``admissions.jsonl`` before the raise
            (D-03: fail-closed admissions still leave an audit trail).
        NotImplementedError
            ``dry_run=False`` was requested. Phase 57 only implements the
            dry-run lane; the real-provider lane lands in Phase 59.
        """
        # 1. Input type guard -- isinstance, NOT bare-except wrapped model_validate.
        if not isinstance(escalation_receipt, EscalationReceipt):
            type_name = type(escalation_receipt).__name__
            self._write_reject_admission(
                type_name=type_name,
            )
            raise GatewayInputError(
                f"Gateway accepts only EscalationReceipt instances; got {type_name}"
            )

        # 1b. Admission precheck (Phase 58-01). When an AdmissionPolicy is
        #     provided, allowlist + virtual-key-registry + Keychain checks run
        #     BEFORE any dispatch. A rejection writes a reject admission entry
        #     and a matching FrontierReceipt with ``status="failed"`` + the
        #     precheck reason_code, preserving the Phase 57 audit-chain
        #     invariant (D-03). Library callers that pass ``None`` keep
        #     Phase 57 behaviour (admission enforcement happens at the HTTP
        #     layer in that case).
        if admission_policy is not None:
            decision = admission_policy.precheck(
                escalation_receipt.project, virtual_key_id,
            )
            if not decision.approved and decision.reason_code is not None:
                return self._submit_admission_reject(
                    escalation_receipt,
                    reason_code=decision.reason_code,
                    reason_detail=decision.reason_detail,
                )

        # 2. Route on dry_run. The real-provider branch runs a provider
        #    adapter in Phase 59; dry-run remains a pure synthesizer.
        if dry_run:
            return self._submit_dry_run(
                escalation_receipt,
                provider=provider,
                model_id=model_id,
            )

        # Phase 59 real-provider dispatch. At least one of:
        #   - ``provider_adapter`` (instance) supplied directly, OR
        #   - ``provider`` (name) resolvable via PROVIDER_REGISTRY.
        if prompt is None or not isinstance(prompt, str) or not prompt:
            raise GatewayInputError(
                "non-dry-run submission requires a non-empty 'prompt' parameter"
            )

        if virtual_key_bytes is None:
            raise GatewayInputError(
                "non-dry-run submission requires 'virtual_key_bytes' "
                "(resolved from Keychain by the admission policy or caller)"
            )

        adapter = provider_adapter
        if adapter is None:
            # Lazy import: keeps ``gateway_client`` import free of ``httpx``
            # so the Phase 57 network-isolation invariant holds. KeyError
            # propagates -- narrow type; caller (service.py) maps to a
            # PROVIDER_AUTH_FAILED receipt or lets it surface as a 500 if
            # the vk registry references an unknown provider.
            from ollarma.providers import get_provider  # noqa: PLC0415
            adapter = get_provider(provider)

        return self._submit_provider(
            escalation_receipt,
            adapter=adapter,
            provider_name=provider,
            model=model_id,
            prompt=prompt,
            virtual_key_bytes=virtual_key_bytes,
        )

    # -- Dry-run synthesizer --------------------------------------------------

    def _submit_dry_run(
        self,
        escalation_receipt: EscalationReceipt,
        *,
        provider: str,
        model_id: str,
    ) -> FrontierReceipt:
        """Synthesize a dry-run FrontierReceipt without any provider call.

        Writes one admission entry (``outcome="dry_run"``) followed by one
        FrontierReceipt (``status="dry_run"``). Both entries enter their
        respective hash-chained streams; the FrontierReceipt's
        ``admission_receipt_hash`` is set to the admission's materialized
        ``receipt_hash`` so Phase 63's trace CLI can walk the linkage.
        """
        # Content-addressed id (determinism: identical inputs → identical id).
        er_payload = escalation_receipt.model_dump(mode="json")
        er_hash = canonical_hash(er_payload)
        escalation_receipt_id = f"er-{er_hash[:16]}"

        # 1. Admission first (D-03 audit trail, even in dry-run).
        admission = GatewayAdmissionEntry(
            admission_id=f"adm-{uuid4().hex}",
            escalation_receipt_id=escalation_receipt_id,
            escalation_receipt_content_hash=er_hash,
            outcome="dry_run",
            reason_code=GatewayReasonCode.DRY_RUN.value,
            reason_detail="dry_run=True; no provider call executed",
            project=escalation_receipt.project,
        )
        admission = self._store.append_admission(admission)

        # 2. FrontierReceipt. Zero tokens / zero cost / zero latency (D-14).
        #    Cross-stream link: admission_receipt_hash = admission.receipt_hash
        #    (Phase 63 trace CLI walks admissions.jsonl → matching receipts line
        #    where FrontierReceipt.admission_receipt_hash == admission.receipt_hash).
        frontier = FrontierReceipt(
            escalation_receipt_id=escalation_receipt_id,
            admission_receipt_hash=admission.receipt_hash,
            provider=provider,
            model_id=model_id,
            prompt_tokens=0,
            response_tokens=0,
            cost_usd=Decimal("0"),
            latency_ms=0,
            status="dry_run",
            reason_code=GatewayReasonCode.DRY_RUN.value,
            context_truncated=False,
            dry_run=True,
        )
        return self._store.append_receipt(frontier)

    # -- Provider dispatch (Phase 59, FRONT-01..FRONT-06) --------------------

    def _submit_provider(
        self,
        escalation_receipt: EscalationReceipt,
        *,
        adapter: "BaseProvider",
        provider_name: str,
        model: str,
        prompt: str,
        virtual_key_bytes: bytes,
    ) -> FrontierReceipt:
        """Call a provider adapter and persist the resulting FrontierReceipt.

        Writes one admission entry (``outcome="accept"``) followed by one
        FrontierReceipt. On provider failure the FrontierReceipt carries
        ``status="failed"`` + the adapter's ``reason_code``; on success it
        carries ``status="succeeded"`` + populated tokens/cost/latency.

        Invariants:
          - No ``except Exception`` — adapter ``submit`` must itself narrow-catch.
          - Raw key bytes never cross into the admission entry or the receipt;
            the bytes are passed by value into ``adapter.submit`` and the
            local reference is cleared before the receipt is appended.
        """
        er_payload = escalation_receipt.model_dump(mode="json")
        er_hash = canonical_hash(er_payload)
        escalation_receipt_id = f"er-{er_hash[:16]}"

        # Admission accept FIRST so the audit chain records the precheck
        # outcome even if the provider call fails downstream.
        admission = self._store.append_admission(
            GatewayAdmissionEntry(
                admission_id=f"adm-{uuid4().hex}",
                escalation_receipt_id=escalation_receipt_id,
                escalation_receipt_content_hash=er_hash,
                outcome="accept",
                reason_code=None,
                reason_detail="admitted; dispatching to provider",
                project=escalation_receipt.project,
            )
        )

        provider_response = adapter.submit(
            prompt=prompt, model=model, virtual_key_bytes=virtual_key_bytes,
        )

        # Defensive key-bytes hygiene: clear the caller's reference window.
        # (The bytes object itself is immutable; this just removes our local
        # binding so GC can reclaim it sooner.)
        del virtual_key_bytes

        # Truncation trio (D-59-04). FrontierReceipt requires both populated
        # together when context_truncated=True.
        truncation_event = provider_response.truncation_event
        if truncation_event is not None:
            original_tokens = int(truncation_event.get("original_estimated_tokens", 0))
            truncated_tokens = int(
                truncation_event.get("truncated_estimated_tokens", 0)
            )
            context_truncated = True
        else:
            original_tokens = None
            truncated_tokens = None
            context_truncated = False

        if provider_response.status == "succeeded":
            frontier = FrontierReceipt(
                escalation_receipt_id=escalation_receipt_id,
                admission_receipt_hash=admission.receipt_hash,
                provider=provider_name,
                model_id=provider_response.model_id,
                provider_request_id=provider_response.provider_request_id,
                prompt_tokens=provider_response.prompt_tokens,
                response_tokens=provider_response.response_tokens,
                cost_usd=provider_response.cost_usd,
                latency_ms=provider_response.latency_ms,
                status="succeeded",
                reason_code=None,
                context_truncated=context_truncated,
                original_tokens=original_tokens,
                truncated_tokens=truncated_tokens,
                dry_run=False,
            )
        else:
            reason_code_enum = (
                provider_response.reason_code
                if provider_response.reason_code is not None
                else GatewayReasonCode.PROVIDER_NETWORK_TIMEOUT
            )
            frontier = FrontierReceipt(
                escalation_receipt_id=escalation_receipt_id,
                admission_receipt_hash=admission.receipt_hash,
                provider=provider_name,
                model_id=provider_response.model_id,
                provider_request_id=None,
                prompt_tokens=0,
                response_tokens=0,
                cost_usd=Decimal("0"),
                latency_ms=provider_response.latency_ms,
                status="failed",
                reason_code=reason_code_enum.value,
                context_truncated=context_truncated,
                original_tokens=original_tokens,
                truncated_tokens=truncated_tokens,
                dry_run=False,
            )
        return self._store.append_receipt(frontier)

    # -- Admission rejection (Phase 58-01, preserves audit chain) ------------

    def _submit_admission_reject(
        self,
        escalation_receipt: EscalationReceipt,
        *,
        reason_code: GatewayReasonCode,
        reason_detail: str,
    ) -> FrontierReceipt:
        """Write admission+receipt pair for a Phase 58 admission rejection.

        Called when ``AdmissionPolicy.precheck`` returned ``approved=False``.
        Writes:
          1. A ``GatewayAdmissionEntry`` with ``outcome="reject"`` + the
             precheck's ``reason_code``.
          2. A ``FrontierReceipt`` with ``status="failed"`` and the same
             reason_code. Provider/model fields are empty strings (no
             routing decision was reached); tokens/cost/latency are zero.

        Returns the materialized FrontierReceipt so the HTTP layer can map
        the reason_code to an HTTP status (403/400/429 per D-58-05).
        """
        er_payload = escalation_receipt.model_dump(mode="json")
        er_hash = canonical_hash(er_payload)
        escalation_receipt_id = f"er-{er_hash[:16]}"

        admission = self._store.append_admission(
            GatewayAdmissionEntry(
                admission_id=f"adm-{uuid4().hex}",
                escalation_receipt_id=escalation_receipt_id,
                escalation_receipt_content_hash=er_hash,
                outcome="reject",
                reason_code=reason_code.value,
                reason_detail=reason_detail[:500],
                project=escalation_receipt.project,
            )
        )

        frontier = FrontierReceipt(
            escalation_receipt_id=escalation_receipt_id,
            admission_receipt_hash=admission.receipt_hash,
            provider="",
            model_id="",
            prompt_tokens=0,
            response_tokens=0,
            cost_usd=Decimal("0"),
            latency_ms=0,
            status="failed",
            reason_code=reason_code.value,
            context_truncated=False,
            dry_run=False,
        )
        return self._store.append_receipt(frontier)

    # -- Reject admission (fail-closed audit trail, D-03) --------------------

    def _write_reject_admission(self, *, type_name: str) -> None:
        """Append a reject admission entry for a malformed input.

        Called before raising ``GatewayInputError`` so the audit stream
        records every rejection (D-03). Uses a sentinel
        ``escalation_receipt_id="unknown"`` and an all-zero hash since the
        input was not a valid EscalationReceipt and therefore has no canonical
        hash to record. The ``reason_detail`` carries the offending type name
        for operator diagnosis.
        """
        entry = GatewayAdmissionEntry(
            admission_id=f"adm-{uuid4().hex}",
            escalation_receipt_id="unknown",
            escalation_receipt_content_hash="0" * 64,
            outcome="reject",
            reason_code=GatewayReasonCode.REJECT_INVALID_RECEIPT.value,
            reason_detail=f"input is {type_name}, not EscalationReceipt",
            project="unknown",
        )
        self._store.append_admission(entry)

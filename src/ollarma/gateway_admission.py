"""gateway_admission.py -- Phase 58-01 admission module.

Phase 58-01 scope: allowlist check + virtual-key registry + Keychain resolution.
Rate caps (GATE-04) land in 58-02; the reason codes for rate caps live in
``GatewayReasonCode`` already (D-58-04) so this module's enum surface stays
stable across the 58-01 / 58-02 boundary.

Exports
-------
- ``AdmissionDecision``   -- dataclass summarizing the precheck outcome.
- ``AdmissionPolicy``     -- wraps a loaded ``features.gateway`` config dict;
  exposes ``precheck(project, virtual_key_id)``.
- ``_load_gateway_config`` -- defensive loader; returns ``{}`` on missing/
  malformed file.
- ``resolve_virtual_key`` -- Keychain lookup seam.
- ``_KEYCHAIN_LOOKUP``    -- module-global callable the test suite monkeypatches
  to inject a mock resolver. Default is the real ``security(1)`` CLI.

Design bindings to ``.planning/phases/58-gateway-admission-rate-caps/58-CONTEXT.md``
- D-58-02   config shape (``features.gateway.allowlist``, ``.virtual_keys``)
- D-58-03   fail-at-first-negative order: allowlist -> vk registry -> keychain
- D-58-04   new reason codes emitted here
- D-58-07   Keychain isolation: bytes never converted to str in this module;
  ``_KEYCHAIN_LOOKUP`` seam exists specifically for deterministic tests.

Failure taxonomy
----------------
No ``except Exception`` -- narrow types only:
``FileNotFoundError``, ``json.JSONDecodeError``, ``OSError``,
``subprocess.CalledProcessError``. Any other exception propagates (DEBT-10).

Raw-key invariant (I-06)
------------------------
The bytes returned by ``_KEYCHAIN_LOOKUP`` flow through ``resolve_virtual_key``
and nowhere else in this module. They are never ``.decode()``-ed, never
``str()``-ed, never formatted into log lines, and never written to receipts or
admission entries. Phase 59's provider adapter is the sole consumer that
converts the bytes to an HTTP header value at call-time.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import warnings
from typing import Any, Callable

from ollarma.gateway import GatewayReasonCode


__all__ = [
    "AdmissionDecision",
    "AdmissionPolicy",
    "RateCapEnforcer",
    "resolve_virtual_key",
]


# ---------------------------------------------------------------------------
# Rate-cap constants (D-58-02 defaults; overridable via features.gateway.rate_caps)
# ---------------------------------------------------------------------------

_DEFAULT_REQ_PER_MIN = 60
_DEFAULT_TOKENS_PER_DAY = 100_000
_REQ_WINDOW_SECONDS = 60.0
_STATE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Keychain lookup seam (D-58-07)
# ---------------------------------------------------------------------------


def _default_keychain_lookup(service_name: str) -> bytes | None:
    """Default Keychain resolver via macOS ``security find-generic-password -w``.

    Returns raw stdout bytes on success (the ``-w`` flag writes only the
    password, one line, no other noise). Returns ``None`` on any expected
    failure mode:

    - Keychain entry not present (``security`` exits non-zero -> CalledProcessError)
    - ``security`` binary unavailable (non-macOS, stripped image -> FileNotFoundError)

    Raw bytes are NEVER converted to str in this function. The caller
    (``resolve_virtual_key``) also preserves bytes-only semantics so I-06 holds
    end-to-end.
    """
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", service_name, "-w"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except subprocess.CalledProcessError:
        return None
    except FileNotFoundError:
        # ``security`` binary missing (non-macOS host, tests in a sandbox).
        return None
    # ``-w`` prints the password followed by a trailing newline. Strip exactly
    # one trailing newline byte if present; do not decode.
    out = completed.stdout
    if out.endswith(b"\n"):
        out = out[:-1]
    if not out:
        return None
    return out


# Module-global callable so tests can monkeypatch without touching the
# ``resolve_virtual_key`` signature. Never log this attribute's value.
_KEYCHAIN_LOOKUP: Callable[[str], bytes | None] = _default_keychain_lookup


def resolve_virtual_key(keychain_service: str) -> bytes | None:
    """Resolve a virtual-key registry entry's Keychain credential.

    Returns raw bytes on hit; ``None`` on miss. The return type is
    intentionally ``bytes | None`` (never ``str``) to discourage accidental
    ``str(key)`` in downstream callers -- the raw-key invariant I-06 is
    enforced by typing, not convention alone.
    """
    return _KEYCHAIN_LOOKUP(keychain_service)


# ---------------------------------------------------------------------------
# Config loader (defensive; fail-conservative)
# ---------------------------------------------------------------------------


def _load_gateway_config(config_path: pathlib.Path) -> dict:
    """Return the ``features.gateway`` sub-dict from ``config.json``.

    Returns ``{}`` if the file is missing, malformed, or lacks the expected
    keys. A ``warnings.warn`` is issued for malformed-but-present config so
    operators see the cause without the admission path raising.

    Never raises -- HTTP admission must be able to build a policy even from a
    partially broken config (the policy will then reject everything with
    ``GATEWAY_NOT_CONFIGURED``, which is the correct fail-closed behavior).
    """
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        warnings.warn(
            f"gateway_admission: config unreadable at {config_path} ({exc})",
            RuntimeWarning,
            stacklevel=2,
        )
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        warnings.warn(
            f"gateway_admission: config JSON invalid at {config_path} ({exc})",
            RuntimeWarning,
            stacklevel=2,
        )
        return {}

    if not isinstance(data, dict):
        warnings.warn(
            f"gateway_admission: config root is not an object at {config_path}",
            RuntimeWarning,
            stacklevel=2,
        )
        return {}

    features = data.get("features")
    if not isinstance(features, dict):
        return {}
    gateway = features.get("gateway")
    if not isinstance(gateway, dict):
        return {}
    return gateway


# ---------------------------------------------------------------------------
# AdmissionDecision
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AdmissionDecision:
    """Outcome of ``AdmissionPolicy.precheck``.

    Attributes
    ----------
    approved :
        ``True`` iff the call may proceed to Phase 57's dispatch path (dry-run
        synth or -- in Phase 59 -- a real provider call).
    reason_code :
        ``None`` when ``approved=True``; otherwise the ``GatewayReasonCode``
        enum member describing why the call was rejected.
    reason_detail :
        Short operator-readable string. Never contains secret material.
    retry_after_seconds :
        Populated by 58-02 rate-cap rejections; always ``None`` in 58-01.
    """

    approved: bool
    reason_code: GatewayReasonCode | None
    reason_detail: str
    retry_after_seconds: int | None = None


# ---------------------------------------------------------------------------
# AdmissionPolicy
# ---------------------------------------------------------------------------


class AdmissionPolicy:
    """Admission policy wrapping a loaded ``features.gateway`` config dict.

    Construction is cheap and side-effect free; the same policy instance can
    be reused across requests within a process. Each ``precheck`` call is a
    pure function of ``(config, project, virtual_key_id, keychain state)``.

    Precheck order (D-58-03, short-circuit on first negative):
      1. ``GATEWAY_NOT_CONFIGURED`` if the config lacks ``features.gateway``
         entirely (reported as approved=False; the HTTP layer treats this as
         ``status=disabled`` per D-11 to preserve audit parity with Phase 57).
      2. ``PROJECT_NOT_ALLOWED`` if ``project`` is not in ``allowlist``.
      3. ``VIRTUAL_KEY_UNKNOWN`` if ``virtual_key_id`` is ``None`` or not in
         the registry.
      4. ``VIRTUAL_KEY_KEYCHAIN_MISS`` if the registry has the vk but the
         Keychain returns ``None``.
      5. approved=True, ``reason_code=None``, detail ``"admitted"``.
    """

    def __init__(self, config: dict) -> None:
        """Build a policy from a loaded ``features.gateway`` dict.

        Missing sub-keys default to empty / most-restrictive values per D-58-02:
          - ``allowlist`` absent -> empty list -> every project rejects.
          - ``virtual_keys`` absent -> empty list -> every vk rejects.
        """
        self._configured = bool(config)
        allowlist = config.get("allowlist") if isinstance(config, dict) else None
        self._allowlist: list[str] = (
            [s for s in allowlist if isinstance(s, str)]
            if isinstance(allowlist, list)
            else []
        )
        vks = config.get("virtual_keys") if isinstance(config, dict) else None
        self._virtual_keys: list[dict] = (
            [v for v in vks if isinstance(v, dict) and isinstance(v.get("id"), str)]
            if isinstance(vks, list)
            else []
        )

    # -- Introspection helpers (test + 58-02 reuse) ---------------------------

    @property
    def configured(self) -> bool:
        """True iff a non-empty ``features.gateway`` block was present."""
        return self._configured

    def _find_vk(self, virtual_key_id: str) -> dict | None:
        for entry in self._virtual_keys:
            if entry.get("id") == virtual_key_id:
                return entry
        return None

    # -- Precheck -------------------------------------------------------------

    def precheck(
        self,
        project: str,
        virtual_key_id: str | None,
    ) -> AdmissionDecision:
        """Run admission checks for a single request.

        ``virtual_key_id`` is optional on the wire (D-58-05); when the gateway
        is configured + enabled, the absence of a vk is itself
        ``VIRTUAL_KEY_UNKNOWN`` because every enabled call must bind to an
        operator-registered credential.
        """
        # 1. Gateway-not-configured guard (D-58-05, HTTP layer folds to disabled).
        if not self._configured:
            return AdmissionDecision(
                approved=False,
                reason_code=GatewayReasonCode.GATEWAY_NOT_CONFIGURED,
                reason_detail="no gateway config found",
            )

        # 2. Allowlist check (GATE-06).
        if project not in self._allowlist:
            return AdmissionDecision(
                approved=False,
                reason_code=GatewayReasonCode.PROJECT_NOT_ALLOWED,
                reason_detail=f"project {project!r} not in allowlist",
            )

        # 3. Virtual-key registry check (GATE-03).
        if virtual_key_id is None:
            return AdmissionDecision(
                approved=False,
                reason_code=GatewayReasonCode.VIRTUAL_KEY_UNKNOWN,
                reason_detail="virtual_key_id missing from request",
            )

        vk_entry = self._find_vk(virtual_key_id)
        if vk_entry is None:
            return AdmissionDecision(
                approved=False,
                reason_code=GatewayReasonCode.VIRTUAL_KEY_UNKNOWN,
                reason_detail=f"virtual_key_id {virtual_key_id!r} not in registry",
            )

        # 4. Keychain resolution (GATE-03 credential isolation).
        service_name = vk_entry.get("keychain_service")
        if not isinstance(service_name, str) or not service_name:
            # Registry entry is malformed (operator error): treat as miss.
            return AdmissionDecision(
                approved=False,
                reason_code=GatewayReasonCode.VIRTUAL_KEY_KEYCHAIN_MISS,
                reason_detail=(
                    f"virtual_key_id {virtual_key_id!r} has no keychain_service"
                ),
            )

        # Bytes-never-str path: we only read the return value's truthiness.
        key_bytes = resolve_virtual_key(service_name)
        if key_bytes is None:
            return AdmissionDecision(
                approved=False,
                reason_code=GatewayReasonCode.VIRTUAL_KEY_KEYCHAIN_MISS,
                reason_detail=(
                    f"virtual_key_id {virtual_key_id!r} registered but "
                    f"Keychain has no entry"
                ),
            )
        # Discard the reference immediately -- admission does not need the key.
        del key_bytes

        # 5. Approved.
        return AdmissionDecision(
            approved=True,
            reason_code=None,
            reason_detail="admitted",
        )


# ---------------------------------------------------------------------------
# Rate-cap enforcement (Phase 58-02, GATE-04, D-58-06)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _RateCapState:
    """Per-project rate-cap counters (in-memory mirror of rate_state.json)."""

    req_window_start: float  # monotonic seconds at window open
    req_count_in_window: int
    tokens_today: int
    today_utc_date: str  # YYYY-MM-DD (UTC)


def _utc_today_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_json(path: pathlib.Path, payload: dict) -> None:
    """Atomic JSON write via tempfile + os.replace (v4.5 pipeline_control precedent).

    Narrow exception handling only — any failure cleans up the tmp file and
    re-raises so the caller can decide whether state persistence failure is
    recoverable. (58-02 callers treat persistence failure as a hard error —
    rate-cap accounting is load-bearing.)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".rate_state.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(payload, sort_keys=True, indent=2).encode("utf-8"))
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class RateCapEnforcer:
    """Per-project rate-cap enforcer with restart-safe state persistence.

    Scope (58-02):
      - ``check(project, estimated_tokens)`` evaluates both caps in D-58-03
        order: req/min first, then tokens/day. Returns an ``AdmissionDecision``
        with ``retry_after_seconds`` on rejection.
      - ``record(project, actual_tokens)`` bumps counters on accepted admission
        and snapshots state to disk atomically.
      - State is keyed by project only (per D-58-06 note: per-vk caps deferred).
      - Thread-safety via a single ``threading.Lock``. Operations are short
        (memory-resident dict + one atomic file replace) so coarse-grained
        locking is acceptable for a single-operator gateway.

    Schema (restart-safe, matches 57.1-02's ``_build_gateway_posture`` reader)::

        {
          "schema_version": 1,
          "updated_at": "<iso8601 UTC>",
          "state": "within_caps" | "degraded",
          "per_project": {
            "<project>": {
              "req_window_start_epoch": <float monotonic-seconds-at-last-save>,
              "req_window_start_wall": <float epoch-seconds-at-last-save>,
              "req_count_in_window": <int>,
              "tokens_today": <int>,
              "today_utc_date": "YYYY-MM-DD"
            }
          }
        }

    Window-reset semantics:
      - ``time.monotonic()`` drives the 60s req/min window. On load from disk
        after process restart the monotonic clock is a fresh origin; we
        persist an additional wall-clock epoch (``req_window_start_wall``)
        and on load compare the delta in wall-clock to treat a stale window
        (>60s) as "reset needed" — preserving the invariant that bursts that
        spanned a restart do not double-count.
      - Tokens/day reset when ``_utc_today_iso()`` advances past the stored
        ``today_utc_date``.
    """

    def __init__(
        self,
        config: dict,
        state_path: pathlib.Path,
        *,
        time_source: Callable[[], float] | None = None,
        wall_time_source: Callable[[], float] | None = None,
        today_source: Callable[[], str] | None = None,
    ) -> None:
        self._state_path = state_path
        self._time = time_source if time_source is not None else time.monotonic
        self._wall = wall_time_source if wall_time_source is not None else time.time
        self._today = today_source if today_source is not None else _utc_today_iso
        self._lock = threading.Lock()

        caps_cfg = config.get("rate_caps") if isinstance(config, dict) else None
        default_caps = (
            caps_cfg.get("default") if isinstance(caps_cfg, dict) else None
        )
        req = (
            default_caps.get("req_per_min")
            if isinstance(default_caps, dict) else None
        )
        tok = (
            default_caps.get("tokens_per_day")
            if isinstance(default_caps, dict) else None
        )
        self._req_per_min: int = int(req) if isinstance(req, int) and req > 0 else _DEFAULT_REQ_PER_MIN
        self._tokens_per_day: int = (
            int(tok) if isinstance(tok, int) and tok > 0 else _DEFAULT_TOKENS_PER_DAY
        )

        self._states: dict[str, _RateCapState] = {}
        self._load_state()

    # -- Introspection --------------------------------------------------------

    @property
    def req_per_min(self) -> int:
        return self._req_per_min

    @property
    def tokens_per_day(self) -> int:
        return self._tokens_per_day

    # -- State I/O ------------------------------------------------------------

    def _load_state(self) -> None:
        """Load persisted state from disk. Missing/invalid -> empty state.

        Narrow excepts only (DEBT-10): FileNotFoundError, json.JSONDecodeError,
        OSError. Any other exception propagates.
        """
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            warnings.warn(
                f"gateway rate-cap: state unreadable at {self._state_path} ({exc})",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            warnings.warn(
                f"gateway rate-cap: state JSON invalid at {self._state_path} ({exc})",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        if not isinstance(data, dict):
            return
        per_project = data.get("per_project")
        if not isinstance(per_project, dict):
            return
        now_mono = self._time()
        now_wall = self._wall()
        today = self._today()
        for project, entry in per_project.items():
            if not isinstance(project, str) or not isinstance(entry, dict):
                continue
            # Saved wall-clock anchor; if absent or stale (>60s elapsed in
            # wall-clock since save), treat the window as expired and reset.
            saved_wall_raw = entry.get("req_window_start_wall")
            saved_wall = (
                float(saved_wall_raw)
                if isinstance(saved_wall_raw, (int, float))
                else None
            )
            wall_delta = (
                now_wall - saved_wall if saved_wall is not None else _REQ_WINDOW_SECONDS + 1.0
            )
            req_count_raw = entry.get("req_count_in_window")
            req_count = int(req_count_raw) if isinstance(req_count_raw, int) else 0
            if wall_delta > _REQ_WINDOW_SECONDS or saved_wall is None:
                # Stale window -> start fresh at now_mono; don't carry count.
                window_start_mono = now_mono
                req_count = 0
            else:
                # Fresh-ish window -> back-date the monotonic origin so the
                # remaining window honors the saved wall-clock position.
                window_start_mono = now_mono - wall_delta
            tokens_raw = entry.get("tokens_today")
            tokens = int(tokens_raw) if isinstance(tokens_raw, int) else 0
            saved_today = entry.get("today_utc_date")
            if not isinstance(saved_today, str) or saved_today != today:
                tokens = 0
                saved_today = today
            self._states[project] = _RateCapState(
                req_window_start=window_start_mono,
                req_count_in_window=req_count,
                tokens_today=tokens,
                today_utc_date=saved_today,
            )

    def _serialize_state(self) -> dict:
        now_wall = self._wall()
        now_mono = self._time()
        per_project: dict[str, dict] = {}
        any_degraded = False
        for project, st in self._states.items():
            # Compute wall-clock anchor for this project's window: derive from
            # how long ago the window started on the monotonic clock.
            window_age = max(0.0, now_mono - st.req_window_start)
            req_window_start_wall = now_wall - window_age
            per_project[project] = {
                "req_window_start_epoch": st.req_window_start,
                "req_window_start_wall": req_window_start_wall,
                "req_count_in_window": st.req_count_in_window,
                "tokens_today": st.tokens_today,
                "today_utc_date": st.today_utc_date,
            }
            if (
                st.req_count_in_window >= self._req_per_min
                or st.tokens_today >= self._tokens_per_day
            ):
                any_degraded = True
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "updated_at": _utc_now_iso(),
            "state": "degraded" if any_degraded else "within_caps",
            "per_project": per_project,
        }

    def _save_state(self) -> None:
        _atomic_write_json(self._state_path, self._serialize_state())

    # -- Window helpers -------------------------------------------------------

    def _get_or_init(self, project: str) -> _RateCapState:
        st = self._states.get(project)
        if st is None:
            st = _RateCapState(
                req_window_start=self._time(),
                req_count_in_window=0,
                tokens_today=0,
                today_utc_date=self._today(),
            )
            self._states[project] = st
        return st

    def _reset_windows_if_needed(self, st: _RateCapState) -> None:
        now_mono = self._time()
        if now_mono - st.req_window_start > _REQ_WINDOW_SECONDS:
            st.req_window_start = now_mono
            st.req_count_in_window = 0
        today = self._today()
        if today != st.today_utc_date:
            st.today_utc_date = today
            st.tokens_today = 0

    # -- Public API -----------------------------------------------------------

    def check(self, project: str, estimated_tokens: int) -> AdmissionDecision:
        """Evaluate both caps in req/min -> tokens/day order. Rejection is free
        (counters are not bumped on reject, per D-58-03)."""
        if estimated_tokens < 0:
            estimated_tokens = 0
        with self._lock:
            st = self._get_or_init(project)
            self._reset_windows_if_needed(st)

            # 1. req/min cap
            if st.req_count_in_window >= self._req_per_min:
                elapsed = self._time() - st.req_window_start
                retry_after = max(1, int(_REQ_WINDOW_SECONDS - elapsed) + 1)
                return AdmissionDecision(
                    approved=False,
                    reason_code=GatewayReasonCode.RATE_CAP_REQ_PER_MIN_EXCEEDED,
                    reason_detail=(
                        f"project {project!r} exceeded req/min cap "
                        f"({self._req_per_min})"
                    ),
                    retry_after_seconds=retry_after,
                )

            # 2. tokens/day cap (consider the estimated_tokens for this request)
            if st.tokens_today + estimated_tokens > self._tokens_per_day:
                # Retry-after for tokens/day: seconds until UTC midnight.
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                tomorrow = (now_utc + datetime.timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                retry_after = max(1, int((tomorrow - now_utc).total_seconds()))
                return AdmissionDecision(
                    approved=False,
                    reason_code=GatewayReasonCode.RATE_CAP_TOKENS_PER_DAY_EXCEEDED,
                    reason_detail=(
                        f"project {project!r} exceeded tokens/day cap "
                        f"({self._tokens_per_day})"
                    ),
                    retry_after_seconds=retry_after,
                )

            return AdmissionDecision(
                approved=True,
                reason_code=None,
                reason_detail="admitted",
            )

    def record(self, project: str, actual_tokens: int) -> None:
        """Bump counters on an accepted admission and persist state atomically.

        ``actual_tokens`` may be 0 (dry-run path) or a provider-reported value
        (Phase 59+). Always bumps the req/min counter by 1.
        """
        if actual_tokens < 0:
            actual_tokens = 0
        with self._lock:
            st = self._get_or_init(project)
            self._reset_windows_if_needed(st)
            st.req_count_in_window += 1
            st.tokens_today += actual_tokens
            self._save_state()


# ---------------------------------------------------------------------------
# Wire rate caps into AdmissionPolicy (D-58-03 ordering)
# ---------------------------------------------------------------------------


def _policy_with_rate_caps_precheck(
    self: AdmissionPolicy,
    project: str,
    virtual_key_id: str | None,
    *,
    estimated_tokens: int = 0,
) -> AdmissionDecision:
    """Extended precheck: runs 58-01 stages first, then 58-02 rate-cap check.

    Kept as a module-level function + monkey-patched method so the diff stays
    minimal to the 58-01 class body while preserving a single precheck entry
    point. Callers that do not pass ``estimated_tokens`` get the 58-01 behavior
    exactly (rate-cap stage is skipped when no enforcer is attached).
    """
    base = _policy_precheck_58_01(self, project, virtual_key_id)
    if not base.approved:
        return base
    enforcer: RateCapEnforcer | None = getattr(self, "_rate_cap_enforcer", None)
    if enforcer is None:
        return base
    return enforcer.check(project, estimated_tokens)


def _policy_attach_enforcer(self: AdmissionPolicy, enforcer: RateCapEnforcer | None) -> None:
    self._rate_cap_enforcer = enforcer  # type: ignore[attr-defined]


# Capture the original 58-01 precheck and install the extended version.
_policy_precheck_58_01 = AdmissionPolicy.precheck  # type: ignore[assignment]
AdmissionPolicy.precheck = _policy_with_rate_caps_precheck  # type: ignore[assignment]
AdmissionPolicy.attach_rate_cap_enforcer = _policy_attach_enforcer  # type: ignore[attr-defined]


_ = Any  # silence unused import in strict linters; kept for 58-02 typing.

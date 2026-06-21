"""swe_bench.py -- SWE-bench Lite ingest + subset selector + local-lane runner.

Phase 61-01 substrate for EXP-1.2 (CONFIRMATORY; pre-registered). Ships the
harness that an operator will use for the live pass@1 run; this module does
NOT perform the live run and does NOT write measured claims to the gsigmad
lab notebook.

Responsibilities (SWE-01, SWE-02, SWE-06):
  - Fetch + cache SWE-bench Lite JSONL to ``.ollarma/benchmarks/swe-bench-lite/``
    with a pinned SHA-256 checksum. Idempotent on re-run.
  - Parse JSONL into ``SWEBenchProblem`` Pydantic records.
  - Resolve subset specifications: ``first-N`` | ``ids=ID1,ID2,...`` |
    ``random-seed=SEED,count=N``.
  - Run each problem through the v4.5 autopilot via ``LocalLaneRunner``,
    capturing a per-problem ``SWEBenchRun`` with hash-chained receipts under
    ``.ollarma/benchmarks/swe-bench-lite/runs/<run_id>/receipts.jsonl``.

Invariants:
  - I-02 (no silent fallback): ``SWAP_DEGRADED`` from autopilot -> record
    ``status="skipped_swap"`` + ``reason_code="SWAP_DEGRADED"``. Never retry,
    never fall back to frontier (Phase 62 adds the frontier lane).
  - Narrow excepts only (no ``except Exception``).
  - Per-problem execution is sandboxed via ``subprocess.run`` with a timeout
    (default 300 s); a runaway test cannot hang the harness.
"""
from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import random
import re
import secrets
import subprocess
import time
import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Literal

import httpx
import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ollarma.escalation import EscalationReceipt, ReasonCode, build_escalation_receipt
from ollarma.evidence import GENESIS_PARENT_HASH, canonical_hash

if TYPE_CHECKING:  # pragma: no cover
    from ollarma.gateway_admission import AdmissionPolicy
    from ollarma.gateway_client import GatewayClient


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATASET_DEFAULT: str = "swe-bench-lite"
"""Default dataset identifier. Substrate ships one dataset; extend later."""

# Pinned HuggingFace raw JSONL URL. The actual SWE-bench Lite dataset lives at
# https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/<sha>/...
# The operator live-run command may override this URL; tests monkeypatch the
# ``_download_bytes`` seam instead of reaching out to the network.
DATASET_URLS: dict[str, str] = {
    "swe-bench-lite": (
        "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/"
        "resolve/main/data/test-00000-of-00001.parquet"
    ),
}

DEFAULT_TIMEOUT_S: int = 300
"""Per-problem sandbox timeout (seconds). Runaway tests cannot hang harness."""

_OUTPUT_EXCERPT_MAX_BYTES: int = 1024
"""Max bytes of stdout+stderr stored on a SWEBenchRun."""

_SUBSET_FIRST_N = re.compile(r"^first-(\d+)$")
_SUBSET_IDS = re.compile(r"^ids=([^,]+(?:,[^,]+)*)$")
_SUBSET_RANDOM = re.compile(r"^random-seed=(-?\d+),count=(\d+)$")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SWEBenchProblem(BaseModel):
    """One SWE-bench Lite problem."""

    model_config = ConfigDict(frozen=True)

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    test_patch: str
    test_cmd: str


SWEBenchStatus = Literal[
    "passed",
    "failed",
    "skipped_swap",
    "skipped_timeout",
    "errored",
]


class SWEBenchRun(BaseModel):
    """Per-problem run record (local or frontier lane)."""

    model_config = ConfigDict(frozen=True)

    instance_id: str
    lane: Literal["local", "frontier"]
    status: SWEBenchStatus
    routing_receipts: list[dict[str, Any]] = Field(default_factory=list)
    frontier_receipts: list[dict[str, Any]] = Field(default_factory=list)
    resource_snapshot: dict[str, Any] = Field(default_factory=dict)
    duration_s: float
    reason_code: str | None = None
    output_excerpt: str = ""
    cost_usd: Decimal = Field(default=Decimal("0"))
    escalation_receipt_id: str | None = None


# ---------------------------------------------------------------------------
# Cache paths
# ---------------------------------------------------------------------------


def _benchmarks_root(repo_root: pathlib.Path, dataset: str) -> pathlib.Path:
    return repo_root / ".ollarma" / "benchmarks" / dataset


def _dataset_jsonl_path(repo_root: pathlib.Path, dataset: str) -> pathlib.Path:
    return _benchmarks_root(repo_root, dataset) / "problems.jsonl"


def _dataset_sha_path(repo_root: pathlib.Path, dataset: str) -> pathlib.Path:
    return _benchmarks_root(repo_root, dataset) / "problems.sha256"


def _runs_root(repo_root: pathlib.Path, dataset: str) -> pathlib.Path:
    return _benchmarks_root(repo_root, dataset) / "runs"


# ---------------------------------------------------------------------------
# Dataset fetch (with a monkeypatch seam)
# ---------------------------------------------------------------------------


def _download_bytes(url: str, *, timeout_s: float = 30.0) -> bytes:
    """Fetch raw bytes over HTTPS.

    Test suite monkeypatches this function to return fixture bytes (see
    ``tests/test_swe_bench_ingest.py``) so pytest never reaches the network.
    """
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def fetch_dataset(
    repo_root: pathlib.Path,
    *,
    dataset: str = DATASET_DEFAULT,
    force: bool = False,
) -> pathlib.Path:
    """Download + cache the dataset JSONL with a pinned SHA-256.

    Returns the path to ``problems.jsonl``. Idempotent: if the cache file
    exists and its SHA matches the sidecar ``problems.sha256`` (and
    ``force=False``), no download happens.

    Decision: direct ``httpx`` instead of the ``datasets`` library. Rationale:
    the ``datasets`` pip package is ~100 MB + pulls pyarrow + torch in many
    environments; for a benchmark harness substrate that only needs JSONL
    records, the added dependency cost is unjustified. See PLAN task 1.

    Raises:
        ValueError: unknown dataset name, or cached SHA mismatch on a force.
        httpx.HTTPError / httpx.TimeoutException: transport failure.
    """
    if dataset not in DATASET_URLS:
        raise ValueError(f"unknown dataset: {dataset!r}")

    bench_dir = _benchmarks_root(repo_root, dataset)
    jsonl_path = _dataset_jsonl_path(repo_root, dataset)
    sha_path = _dataset_sha_path(repo_root, dataset)

    if not force and jsonl_path.exists() and sha_path.exists():
        try:
            recorded = sha_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            recorded = ""
        if recorded and _sha256_of_path(jsonl_path) == recorded:
            return jsonl_path

    bench_dir.mkdir(parents=True, exist_ok=True)

    url = DATASET_URLS[dataset]
    payload = _download_bytes(url)
    jsonl_path.write_bytes(payload)
    sha_path.write_text(hashlib.sha256(payload).hexdigest(), encoding="utf-8")
    return jsonl_path


def _sha256_of_path(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# JSONL load
# ---------------------------------------------------------------------------


def load_problems(
    repo_root: pathlib.Path,
    *,
    dataset: str = DATASET_DEFAULT,
) -> list[SWEBenchProblem]:
    """Parse the cached JSONL into ``SWEBenchProblem`` records.

    Raises:
        FileNotFoundError: dataset not fetched yet.
        ValueError: malformed JSONL or missing required fields.
    """
    jsonl_path = _dataset_jsonl_path(repo_root, dataset)
    if not jsonl_path.exists():
        raise FileNotFoundError(
            f"dataset not fetched: {jsonl_path} (run `ollarma swe-bench fetch`)"
        )

    problems: list[SWEBenchProblem] = []
    with jsonl_path.open("rb") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = orjson.loads(stripped)
            except orjson.JSONDecodeError as exc:
                raise ValueError(
                    f"{jsonl_path}:{lineno}: malformed JSON: {exc}"
                ) from exc
            try:
                problems.append(SWEBenchProblem.model_validate(obj))
            except ValidationError as exc:
                raise ValueError(
                    f"{jsonl_path}:{lineno}: schema mismatch: {exc}"
                ) from exc
    return problems


# ---------------------------------------------------------------------------
# Subset selector (SWE-06)
# ---------------------------------------------------------------------------


def resolve_subset(
    problems: list[SWEBenchProblem],
    spec: str,
) -> list[SWEBenchProblem]:
    """Resolve a subset-selection spec against a problem list.

    Forms:
      - ``first-N`` -> first N problems (N >= 0).
      - ``ids=ID1,ID2,...`` -> explicit instance_ids (preserves spec order;
        raises ValueError if any id is not in ``problems``).
      - ``random-seed=SEED,count=N`` -> reproducible random subset.

    Raises:
        ValueError: malformed spec, unknown id, or count out of range.
    """
    if not spec or not isinstance(spec, str):
        raise ValueError("subset spec must be a non-empty string")

    m = _SUBSET_FIRST_N.match(spec)
    if m is not None:
        n = int(m.group(1))
        return list(problems[:n])

    m = _SUBSET_IDS.match(spec)
    if m is not None:
        wanted = [s for s in m.group(1).split(",") if s]
        by_id = {p.instance_id: p for p in problems}
        missing = [s for s in wanted if s not in by_id]
        if missing:
            raise ValueError(f"unknown instance_ids: {missing}")
        return [by_id[s] for s in wanted]

    m = _SUBSET_RANDOM.match(spec)
    if m is not None:
        seed = int(m.group(1))
        count = int(m.group(2))
        if count < 0:
            raise ValueError("count must be >= 0")
        if count > len(problems):
            raise ValueError(
                f"count {count} exceeds available problem count {len(problems)}"
            )
        rng = random.Random(seed)
        return rng.sample(list(problems), count)

    raise ValueError(
        f"unrecognized subset spec: {spec!r} "
        "(expected first-N | ids=ID1,ID2,... | random-seed=SEED,count=N)"
    )


# ---------------------------------------------------------------------------
# Per-run receipt stream (hash-chained, same primitive as gateway receipts)
# ---------------------------------------------------------------------------


def _new_run_id() -> str:
    """Generate a run_id of the form ``swe-run-<utc-ts>-<hash4>``."""
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(2)
    return f"swe-run-{ts}-{suffix}"


def run_dir(
    repo_root: pathlib.Path,
    run_id: str,
    *,
    dataset: str = DATASET_DEFAULT,
) -> pathlib.Path:
    """Return the run directory for a SWE-bench run id."""
    return _runs_root(repo_root, dataset) / run_id


class _ReceiptAppender:
    """Append-only hash-chained JSONL writer for per-run receipts."""

    def __init__(self, path: pathlib.Path) -> None:
        self._path = path
        self._parent = GENESIS_PARENT_HASH
        self._seq = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    def append(self, body: dict[str, Any]) -> str:
        row_hash = canonical_hash(body)
        header = {
            "sequence": self._seq,
            "parent_hash": self._parent,
            "row_hash": row_hash,
        }
        header["receipt_hash"] = canonical_hash(header)
        line = {"header": header, "body": body}
        with self._path.open("ab") as fh:
            fh.write(orjson.dumps(line, option=orjson.OPT_SORT_KEYS))
            fh.write(b"\n")
        self._parent = header["receipt_hash"]
        self._seq += 1
        return header["receipt_hash"]


# ---------------------------------------------------------------------------
# Local-lane runner (SWE-02)
# ---------------------------------------------------------------------------


# An autopilot dispatch returns (autopilot_report_like, escalation_receipt | None)
AutopilotDispatch = Callable[[SWEBenchProblem, str], tuple[Any, EscalationReceipt | None]]


def _default_autopilot_dispatch(
    problem: SWEBenchProblem,  # noqa: ARG001 -- signature contract
    project: str,  # noqa: ARG001
) -> tuple[Any, EscalationReceipt | None]:
    """Default autopilot dispatch used by the operator live-run command.

    The substrate unit tests inject a mock dispatch via
    ``LocalLaneRunner(autopilot_dispatch=...)``. We do NOT call the real
    autopilot in the test suite (would require a live Ollama + local model +
    swap headroom). The operator live-run entrypoint provides a dispatch
    that calls ``service.submit_autopilot`` and inspects the resulting
    ``AutopilotReport`` for a SWAP_DEGRADED signal.
    """
    raise NotImplementedError(
        "autopilot dispatch not wired; pass autopilot_dispatch= to LocalLaneRunner"
    )


def _run_test_cmd(
    test_cmd: str,
    *,
    cwd: pathlib.Path,
    timeout_s: int,
) -> tuple[int, str, str, str]:
    """Execute the problem's test command in a subprocess.

    Returns ``(exit_code, stdout, stderr, outcome)`` where ``outcome`` is one
    of ``"completed"``, ``"timeout"``, ``"errored"``.

    Never raises; narrow failure taxonomy only:
      - ``subprocess.TimeoutExpired`` -> outcome="timeout"
      - ``FileNotFoundError``         -> outcome="errored"
      - ``OSError``                   -> outcome="errored"
    """
    try:
        completed = subprocess.run(
            ["/bin/sh", "-c", test_cmd],
            cwd=str(cwd),
            timeout=timeout_s,
            check=False,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout.decode("utf-8", "replace") if exc.stdout else "")
        stderr = (exc.stderr.decode("utf-8", "replace") if exc.stderr else "")
        return (-1, stdout, stderr, "timeout")
    except FileNotFoundError as exc:
        return (127, "", f"FileNotFoundError: {exc}", "errored")
    except OSError as exc:
        return (1, "", f"OSError: {exc}", "errored")

    stdout = completed.stdout.decode("utf-8", "replace") if completed.stdout else ""
    stderr = completed.stderr.decode("utf-8", "replace") if completed.stderr else ""
    return (completed.returncode, stdout, stderr, "completed")


def _truncate_excerpt(stdout: str, stderr: str) -> str:
    combined = (stdout + "\n" + stderr).strip()
    if len(combined.encode("utf-8")) <= _OUTPUT_EXCERPT_MAX_BYTES:
        return combined
    encoded = combined.encode("utf-8")[:_OUTPUT_EXCERPT_MAX_BYTES]
    return encoded.decode("utf-8", "replace")


class LocalLaneRunner:
    """Runs SWE-bench problems through the local autopilot lane."""

    def __init__(
        self,
        *,
        repo_root: pathlib.Path,
        run_id: str | None = None,
        dataset: str = DATASET_DEFAULT,
        autopilot_dispatch: AutopilotDispatch | None = None,
        work_dir: pathlib.Path | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.dataset = dataset
        self.run_id = run_id or _new_run_id()
        self._dispatch = autopilot_dispatch or _default_autopilot_dispatch
        self._work_dir = work_dir or repo_root
        self._run_dir = run_dir(repo_root, self.run_id, dataset=dataset)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._receipts = _ReceiptAppender(self._run_dir / "receipts.jsonl")

    @property
    def receipts_path(self) -> pathlib.Path:
        return self._run_dir / "receipts.jsonl"

    def run(
        self,
        problem: SWEBenchProblem,
        project: str,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> SWEBenchRun:
        """Dispatch a single problem. Returns a ``SWEBenchRun`` record.

        SWAP_DEGRADED handling: if the autopilot dispatch returns an
        escalation receipt with ``reason_code=SWAP_DEGRADED``, we record
        ``status="skipped_swap"`` and copy the receipt into
        ``routing_receipts``. No retry, no frontier fallback (invariant I-02;
        Phase 62 adds the frontier lane as a separate runner).
        """
        started = time.monotonic()
        routing_receipts: list[dict[str, Any]] = []
        resource_snapshot: dict[str, Any] = {}

        try:
            report, escalation = self._dispatch(problem, project)
        except NotImplementedError as exc:
            duration = time.monotonic() - started
            body = self._receipt_body(
                problem, status="errored", reason_code="DISPATCH_UNAVAILABLE",
                duration_s=duration, excerpt=str(exc),
            )
            self._receipts.append(body)
            return SWEBenchRun(
                instance_id=problem.instance_id,
                lane="local",
                status="errored",
                routing_receipts=routing_receipts,
                resource_snapshot=resource_snapshot,
                duration_s=duration,
                reason_code="DISPATCH_UNAVAILABLE",
                output_excerpt=str(exc)[:_OUTPUT_EXCERPT_MAX_BYTES],
            )

        # Invariant I-02: SWAP_DEGRADED is never retried silently.
        if escalation is not None and _is_swap_degraded(escalation):
            duration = time.monotonic() - started
            receipt_dict = escalation.model_dump(mode="json")
            routing_receipts.append(receipt_dict)
            resource_snapshot = dict(escalation.resource_snapshot or {})
            body = self._receipt_body(
                problem, status="skipped_swap", reason_code="SWAP_DEGRADED",
                duration_s=duration, excerpt=escalation.reason_detail,
            )
            self._receipts.append(body)
            return SWEBenchRun(
                instance_id=problem.instance_id,
                lane="local",
                status="skipped_swap",
                routing_receipts=routing_receipts,
                resource_snapshot=resource_snapshot,
                duration_s=duration,
                reason_code="SWAP_DEGRADED",
                output_excerpt=escalation.reason_detail[:_OUTPUT_EXCERPT_MAX_BYTES],
            )

        # Execute the problem's test command in a sandbox.
        exit_code, stdout, stderr, outcome = _run_test_cmd(
            problem.test_cmd, cwd=self._work_dir, timeout_s=timeout_s,
        )
        duration = time.monotonic() - started
        excerpt = _truncate_excerpt(stdout, stderr)

        if outcome == "timeout":
            status: SWEBenchStatus = "skipped_timeout"
            reason_code: str | None = "SANDBOX_TIMEOUT"
        elif outcome == "errored":
            status = "errored"
            reason_code = "EXEC_ERROR"
        elif exit_code == 0:
            status = "passed"
            reason_code = None
        else:
            status = "failed"
            reason_code = f"EXIT_{exit_code}"

        # Attach any receipts the autopilot report carried.
        report_receipts = _extract_report_receipts(report)
        routing_receipts.extend(report_receipts)

        body = self._receipt_body(
            problem, status=status, reason_code=reason_code,
            duration_s=duration, excerpt=excerpt,
        )
        self._receipts.append(body)

        return SWEBenchRun(
            instance_id=problem.instance_id,
            lane="local",
            status=status,
            routing_receipts=routing_receipts,
            resource_snapshot=resource_snapshot,
            duration_s=duration,
            reason_code=reason_code,
            output_excerpt=excerpt,
        )

    def _receipt_body(
        self,
        problem: SWEBenchProblem,
        *,
        status: str,
        reason_code: str | None,
        duration_s: float,
        excerpt: str,
    ) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "instance_id": problem.instance_id,
            "lane": "local",
            "status": status,
            "reason_code": reason_code,
            "duration_s": round(duration_s, 6),
            "excerpt": excerpt[:_OUTPUT_EXCERPT_MAX_BYTES],
        }


def _is_swap_degraded(escalation: EscalationReceipt) -> bool:
    """Return True if the escalation receipt's reason_code is SWAP_DEGRADED.

    ``EscalationReceipt`` is built with ``use_enum_values=True`` so the field
    is a string literal at runtime; handle both string and enum just in case
    a caller bypasses the helper.
    """
    rc = escalation.reason_code
    if isinstance(rc, ReasonCode):
        return rc is ReasonCode.SWAP_DEGRADED
    return rc == ReasonCode.SWAP_DEGRADED.value


def _extract_report_receipts(report: Any) -> list[dict[str, Any]]:
    """Pull routing/escalation receipts off an autopilot report-shape object.

    The substrate does not depend on a specific shape here; the production
    operator dispatch may pass an ``AutopilotReport`` or a plain dict. We
    look for a ``receipts`` attribute/key and, failing that, return [].
    """
    if report is None:
        return []
    if isinstance(report, dict):
        raw = report.get("receipts", [])
    else:
        raw = getattr(report, "receipts", [])
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append(item)
        elif hasattr(item, "model_dump"):
            out.append(item.model_dump(mode="json"))
    return out


# ---------------------------------------------------------------------------
# Frontier-lane runner (Phase 62-01; SWE-03)
# ---------------------------------------------------------------------------


class FrontierLaneRunner:
    """Runs SWE-bench problems through the gateway frontier lane.

    Builds an :class:`EscalationReceipt` per problem, dispatches via
    :class:`GatewayClient.submit` with ``dry_run=False``, captures the
    resulting :class:`FrontierReceipt` + provider response, and records a
    :class:`SWEBenchRun` with ``lane="frontier"``.

    Invariant I-02 (no silent fallback): if the provider call returns
    ``status="failed"``, the SWEBenchRun records ``status="failed"`` with the
    provider's reason code -- the runner never re-dispatches on the local
    lane.
    """

    def __init__(
        self,
        *,
        repo_root: pathlib.Path,
        gateway_client: "GatewayClient",
        admission_policy: "AdmissionPolicy | None" = None,
        run_id: str | None = None,
        dataset: str = DATASET_DEFAULT,
        virtual_key_bytes: bytes | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.dataset = dataset
        self.run_id = run_id or _new_run_id()
        self._client = gateway_client
        self._admission_policy = admission_policy
        self._virtual_key_bytes = virtual_key_bytes
        self._run_dir = run_dir(repo_root, self.run_id, dataset=dataset)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._receipts = _ReceiptAppender(self._run_dir / "receipts.jsonl")

    @property
    def receipts_path(self) -> pathlib.Path:
        return self._run_dir / "receipts.jsonl"

    def run(
        self,
        problem: SWEBenchProblem,
        project: str,
        *,
        virtual_key_id: str,
        model: str,
        timeout_s: int = DEFAULT_TIMEOUT_S,  # noqa: ARG002 -- timeout flows to adapter
    ) -> SWEBenchRun:
        """Dispatch one problem via the frontier gateway.

        Returns a :class:`SWEBenchRun` whose ``status`` reflects the provider
        outcome. The run's ``frontier_receipts`` carries the FrontierReceipt
        model dump (exactly one entry per run).
        """
        started = time.monotonic()

        escalation = build_escalation_receipt(
            project=project,
            lane="swe-bench-frontier",
            task_class="code",
            reason_code=ReasonCode.RESOURCE_BUDGET_EXCEEDED,
            reason_detail=(
                f"SWE-bench {problem.instance_id} routed to frontier"
            ),
            next_action="frontier_or_human",
            resource_snapshot={"instance_id": problem.instance_id},
        )

        try:
            receipt = self._client.submit(
                escalation,
                dry_run=False,
                provider="anthropic",
                model_id=model,
                prompt=problem.problem_statement,
                admission_policy=self._admission_policy,
                virtual_key_id=virtual_key_id,
                virtual_key_bytes=self._virtual_key_bytes,
            )
        except NotImplementedError as exc:
            duration = time.monotonic() - started
            body = self._receipt_body(
                problem, status="errored", reason_code="GATEWAY_UNAVAILABLE",
                duration_s=duration, excerpt=str(exc),
                escalation_receipt_id=None,
            )
            self._receipts.append(body)
            return SWEBenchRun(
                instance_id=problem.instance_id,
                lane="frontier",
                status="errored",
                duration_s=duration,
                reason_code="GATEWAY_UNAVAILABLE",
                output_excerpt=str(exc)[:_OUTPUT_EXCERPT_MAX_BYTES],
            )

        duration = time.monotonic() - started
        receipt_dict = receipt.model_dump(mode="json")

        if receipt.status == "succeeded":
            status: SWEBenchStatus = "passed"
            reason_code: str | None = None
            excerpt = ""
        else:
            # Invariant I-02: record failed; never re-dispatch on local lane.
            status = "failed"
            reason_code = receipt.reason_code or "PROVIDER_FAILED"
            excerpt = (
                f"frontier lane {receipt.status}; "
                f"reason_code={receipt.reason_code or 'unknown'}"
            )

        body = self._receipt_body(
            problem,
            status=status,
            reason_code=reason_code,
            duration_s=duration,
            excerpt=excerpt,
            escalation_receipt_id=receipt.escalation_receipt_id,
        )
        self._receipts.append(body)

        return SWEBenchRun(
            instance_id=problem.instance_id,
            lane="frontier",
            status=status,
            frontier_receipts=[receipt_dict],
            duration_s=duration,
            reason_code=reason_code,
            output_excerpt=excerpt[:_OUTPUT_EXCERPT_MAX_BYTES],
            cost_usd=receipt.cost_usd,
            escalation_receipt_id=receipt.escalation_receipt_id,
        )

    def _receipt_body(
        self,
        problem: SWEBenchProblem,
        *,
        status: str,
        reason_code: str | None,
        duration_s: float,
        excerpt: str,
        escalation_receipt_id: str | None,
    ) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "instance_id": problem.instance_id,
            "lane": "frontier",
            "status": status,
            "reason_code": reason_code,
            "duration_s": round(duration_s, 6),
            "excerpt": excerpt[:_OUTPUT_EXCERPT_MAX_BYTES],
            "escalation_receipt_id": escalation_receipt_id,
        }


# ---------------------------------------------------------------------------
# Leaderboard artifact + verifier (Phase 62-01; SWE-04, SWE-05)
# ---------------------------------------------------------------------------


class LeaderboardArtifact(BaseModel):
    """Aggregated dual-lane SWE-bench leaderboard.

    Machine-readable summary of one benchmark run. Persisted via
    :func:`write_leaderboard` as ``results/swe-bench-lite-<run_id>.json``;
    Markdown companion sits alongside for human consumption.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    run_id: str
    created_at: str
    subset_spec: str
    total_problems: int
    per_lane_pass_at_1: dict[str, str]
    per_lane_cost_usd: dict[str, str]
    per_problem: list[dict[str, Any]]
    receipt_chain_heads: dict[str, str]


@dataclasses.dataclass(frozen=True)
class VerifyRow:
    """One row in the leaderboard chain verify output."""

    stream: str  # "local" | "frontier"
    lines_checked: int
    head_hash: str
    status: str  # "clean" | "broken"
    detail: str


@dataclasses.dataclass(frozen=True)
class VerifyResult:
    """Aggregate result of :func:`verify_leaderboard_chain`."""

    passed: bool
    rows: list[VerifyRow]
    first_break_detail: str | None


def build_leaderboard(
    local_runs: list[SWEBenchRun],
    frontier_runs: list[SWEBenchRun],
    *,
    subset_spec: str,
    run_id: str,
    local_chain_head: str = GENESIS_PARENT_HASH,
    frontier_chain_head: str = GENESIS_PARENT_HASH,
) -> LeaderboardArtifact:
    """Build a :class:`LeaderboardArtifact` from two lanes' run records.

    ``total_problems`` is the union of distinct ``instance_id``s across both
    lanes. ``per_lane_pass_at_1`` is ``passed / total_attempts`` rendered as
    a string (Decimal) -- stable across repeated runs of the same harness.
    """
    local_by_id = {r.instance_id: r for r in local_runs}
    frontier_by_id = {r.instance_id: r for r in frontier_runs}
    all_ids = sorted(set(local_by_id.keys()) | set(frontier_by_id.keys()))

    def _pass_at_1(runs: list[SWEBenchRun]) -> Decimal:
        attempts = len(runs)
        if attempts == 0:
            return Decimal("0")
        passes = sum(1 for r in runs if r.status == "passed")
        # Five-decimal quantization -- deterministic + human-readable.
        return (Decimal(passes) / Decimal(attempts)).quantize(Decimal("0.00001"))

    def _total_cost(runs: list[SWEBenchRun]) -> Decimal:
        total = Decimal("0")
        for r in runs:
            total += r.cost_usd
        return total

    per_problem: list[dict[str, Any]] = []
    for instance_id in all_ids:
        local = local_by_id.get(instance_id)
        frontier = frontier_by_id.get(instance_id)
        per_problem.append({
            "instance_id": instance_id,
            "local_status": local.status if local is not None else None,
            "frontier_status": frontier.status if frontier is not None else None,
            "frontier_cost_usd": (
                str(frontier.cost_usd) if frontier is not None else "0"
            ),
        })

    created_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return LeaderboardArtifact(
        run_id=run_id,
        created_at=created_at,
        subset_spec=subset_spec,
        total_problems=len(all_ids),
        per_lane_pass_at_1={
            "local": str(_pass_at_1(local_runs)),
            "frontier": str(_pass_at_1(frontier_runs)),
        },
        per_lane_cost_usd={
            "local": str(_total_cost(local_runs)),
            "frontier": str(_total_cost(frontier_runs)),
        },
        per_problem=per_problem,
        receipt_chain_heads={
            "local": local_chain_head,
            "frontier": frontier_chain_head,
        },
    )


def write_leaderboard(
    artifact: LeaderboardArtifact,
    repo_root: pathlib.Path,
    *,
    dataset: str = DATASET_DEFAULT,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Write leaderboard JSON + Markdown to ``results/``.

    Returns ``(json_path, md_path)``. JSON uses orjson with stable key
    ordering + indent-2 so repeated runs from identical inputs produce
    byte-identical files (modulo ``created_at``).
    """
    results_dir = repo_root / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / f"{dataset}-{artifact.run_id}.json"
    md_path = results_dir / f"{dataset}-{artifact.run_id}.md"

    payload = artifact.model_dump(mode="json")
    json_path.write_bytes(
        orjson.dumps(payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2)
    )

    lines: list[str] = []
    lines.append(f"# SWE-bench leaderboard — {artifact.run_id}")
    lines.append("")
    lines.append(f"- Subset: `{artifact.subset_spec}`")
    lines.append(f"- Created: {artifact.created_at}")
    lines.append(f"- Total problems: {artifact.total_problems}")
    lines.append("")
    lines.append("## pass@1")
    lines.append("")
    lines.append("| lane | pass@1 | cost_usd |")
    lines.append("|------|--------|----------|")
    for lane in ("local", "frontier"):
        lines.append(
            f"| {lane} | "
            f"{artifact.per_lane_pass_at_1.get(lane, '0')} | "
            f"{artifact.per_lane_cost_usd.get(lane, '0')} |"
        )
    lines.append("")
    lines.append("## per-problem")
    lines.append("")
    lines.append("| instance_id | local | frontier | frontier_cost_usd |")
    lines.append("|-------------|-------|----------|-------------------|")
    for row in artifact.per_problem:
        lines.append(
            f"| {row['instance_id']} | "
            f"{row.get('local_status') or '-'} | "
            f"{row.get('frontier_status') or '-'} | "
            f"{row.get('frontier_cost_usd', '0')} |"
        )
    lines.append("")
    lines.append("## receipt chain heads")
    lines.append("")
    for stream, head in sorted(artifact.receipt_chain_heads.items()):
        lines.append(f"- {stream}: `{head}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (json_path, md_path)


def load_leaderboard(
    run_id: str,
    repo_root: pathlib.Path,
    *,
    dataset: str = DATASET_DEFAULT,
) -> LeaderboardArtifact:
    """Load a persisted leaderboard by run_id."""
    path = repo_root / "results" / f"{dataset}-{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"leaderboard not found: {path}")
    payload = orjson.loads(path.read_bytes())
    return LeaderboardArtifact.model_validate(payload)


def _verify_per_run_receipt_stream(
    path: pathlib.Path,
) -> tuple[int, str, str | None]:
    """Walk a LocalLaneRunner/FrontierLaneRunner receipt stream.

    Returns ``(lines_checked, head_hash, break_detail_or_None)``. A clean
    stream has ``break_detail=None`` and the head is the last line's
    ``receipt_hash`` (or ``GENESIS_PARENT_HASH`` for an empty/missing file).
    """
    if not path.exists():
        return (0, GENESIS_PARENT_HASH, None)
    parent = GENESIS_PARENT_HASH
    lines_checked = 0
    try:
        with path.open("rb") as fh:
            for lineno, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    record = orjson.loads(stripped)
                except orjson.JSONDecodeError as exc:
                    return (
                        lines_checked,
                        parent,
                        f"line {lineno}: not valid JSON: {exc}",
                    )
                header = record.get("header")
                body = record.get("body")
                if not isinstance(header, dict) or not isinstance(body, dict):
                    return (
                        lines_checked,
                        parent,
                        f"line {lineno}: missing header/body",
                    )
                if header.get("parent_hash") != parent:
                    return (
                        lines_checked,
                        parent,
                        (
                            f"line {lineno}: parent_hash mismatch "
                            f"(expected {parent[:16]}..., got "
                            f"{str(header.get('parent_hash'))[:16]}...)"
                        ),
                    )
                expected_row_hash = canonical_hash(body)
                if header.get("row_hash") != expected_row_hash:
                    return (
                        lines_checked,
                        parent,
                        (
                            f"line {lineno}: row_hash mismatch "
                            f"(recomputed {expected_row_hash[:16]}..., stored "
                            f"{str(header.get('row_hash'))[:16]}...)"
                        ),
                    )
                expected_receipt_hash = canonical_hash(
                    {
                        "sequence": header.get("sequence"),
                        "parent_hash": header.get("parent_hash"),
                        "row_hash": header.get("row_hash"),
                    }
                )
                if header.get("receipt_hash") != expected_receipt_hash:
                    return (
                        lines_checked,
                        parent,
                        (
                            f"line {lineno}: receipt_hash mismatch "
                            f"(recomputed {expected_receipt_hash[:16]}..., "
                            f"stored "
                            f"{str(header.get('receipt_hash'))[:16]}...)"
                        ),
                    )
                parent = header["receipt_hash"]
                lines_checked += 1
    except OSError as exc:
        return (lines_checked, parent, f"read error: {exc}")
    return (lines_checked, parent, None)


def verify_leaderboard_chain(
    run_id: str,
    repo_root: pathlib.Path,
    *,
    dataset: str = DATASET_DEFAULT,
    frontier_run_id: str | None = None,
) -> VerifyResult:
    """Verify both receipt streams referenced by a leaderboard run.

    Walks:
      1. ``.ollarma/benchmarks/<dataset>/runs/<run_id>/receipts.jsonl``
         (local lane). Recomputes every ``row_hash`` / ``receipt_hash`` /
         ``parent_hash`` linkage.
      2. ``.ollarma/benchmarks/<dataset>/runs/<frontier_run_id>/receipts.jsonl``
         if provided; otherwise tries the same ``run_id`` dir as (1).
      3. The gateway streams under ``.ollarma/gateway/`` via
         :class:`GatewayReceiptStore.verify_chain`.

    Any break sets ``passed=False`` and ``first_break_detail`` to the first
    problem encountered. Streams that simply do not exist (e.g., the frontier
    lane was not exercised) are recorded as ``clean`` with ``lines_checked=0``.
    """
    from ollarma.gateway import (  # noqa: PLC0415
        GatewayReceiptStore, GatewayStoreError,
    )

    rows: list[VerifyRow] = []
    first_break: str | None = None

    local_path = run_dir(repo_root, run_id, dataset=dataset) / "receipts.jsonl"
    local_checked, local_head, local_break = _verify_per_run_receipt_stream(
        local_path
    )
    rows.append(
        VerifyRow(
            stream="local",
            lines_checked=local_checked,
            head_hash=local_head,
            status="broken" if local_break is not None else "clean",
            detail=local_break or f"local chain clean at {local_head[:16]}...",
        )
    )
    if local_break is not None and first_break is None:
        first_break = f"local: {local_break}"

    frontier_effective_run_id = frontier_run_id or run_id
    frontier_path = (
        run_dir(repo_root, frontier_effective_run_id, dataset=dataset)
        / "receipts.jsonl"
    )
    if frontier_path == local_path:
        # Same file -- already verified above. Still surface a row for
        # symmetry with the leaderboard artifact shape.
        rows.append(
            VerifyRow(
                stream="frontier",
                lines_checked=local_checked,
                head_hash=local_head,
                status="broken" if local_break is not None else "clean",
                detail=(
                    local_break
                    or f"frontier chain shares local run dir at {local_head[:16]}..."
                ),
            )
        )
    else:
        frontier_checked, frontier_head, frontier_break = (
            _verify_per_run_receipt_stream(frontier_path)
        )
        rows.append(
            VerifyRow(
                stream="frontier",
                lines_checked=frontier_checked,
                head_hash=frontier_head,
                status="broken" if frontier_break is not None else "clean",
                detail=(
                    frontier_break
                    or f"frontier chain clean at {frontier_head[:16]}..."
                ),
            )
        )
        if frontier_break is not None and first_break is None:
            first_break = f"frontier: {frontier_break}"

    # Gateway streams (admissions.jsonl, receipts.jsonl). Any break here is
    # also a leaderboard-level failure because frontier_receipts point into
    # the gateway stream by escalation_receipt_id.
    store = GatewayReceiptStore(repo_root)
    for stream in ("admissions", "receipts"):
        try:
            head = store.verify_chain(stream)  # type: ignore[arg-type]
        except GatewayStoreError as exc:
            rows.append(
                VerifyRow(
                    stream=f"gateway:{stream}",
                    lines_checked=-1,
                    head_hash="",
                    status="broken",
                    detail=str(exc),
                )
            )
            if first_break is None:
                first_break = f"gateway:{stream}: {exc}"
        else:
            rows.append(
                VerifyRow(
                    stream=f"gateway:{stream}",
                    lines_checked=-1,
                    head_hash=head,
                    status="clean",
                    detail=f"gateway {stream} chain clean at {head[:16]}...",
                )
            )

    return VerifyResult(
        passed=first_break is None,
        rows=rows,
        first_break_detail=first_break,
    )


__all__ = [
    "DATASET_DEFAULT",
    "DATASET_URLS",
    "DEFAULT_TIMEOUT_S",
    "FrontierLaneRunner",
    "LeaderboardArtifact",
    "LocalLaneRunner",
    "SWEBenchProblem",
    "SWEBenchRun",
    "VerifyResult",
    "VerifyRow",
    "build_leaderboard",
    "fetch_dataset",
    "load_leaderboard",
    "load_problems",
    "resolve_subset",
    "run_dir",
    "verify_leaderboard_chain",
    "write_leaderboard",
]

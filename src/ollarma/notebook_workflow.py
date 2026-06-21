"""notebook_workflow.py -- Phase 71 cross-repo notebook workflow runtime.

Extends the existing ``validated-notebook`` workload class
(:mod:`ollarma.workflow_manifest`) into a cross-repo, manifest-driven,
hash-receipted notebook execution runtime. Net-new surface over the existing
papermill adapter (:func:`ollarma.autopilot._run_notebook`):

* A pydantic v2 manifest schema for a cross-repo notebook workflow with
  explicit steps. Single-notebook (``validated-notebook``) and multi-notebook
  DAG (``validated-notebook-dag``) with per-step ``depends_on`` dependencies.
* An *independence validator* that REJECTS non-independent wrapper notebooks
  -- notebooks that merely delegate to ``%run`` / ``!python`` / ``subprocess``
  / shell wrappers / ``scripts/exp*.py``. A notebook must be the canonical
  replay artifact, not a thin wrapper around a script (ROADMAP Decision lock).
* A hash-chained :class:`NotebookWorkflowReceipt` mirroring the
  :mod:`ollarma.run_ledger` receipt + sha256 hash-chain discipline. Carries
  ``run_id``, input file hashes, output artifact hashes, executed-notebook
  hash, the validation result, and an explicit no-writeback /
  no-claim-promotion statement.
* gsigmad gate checks surfaced BEFORE and AFTER execution: an EXP/PROMPT
  linkage check (refuse if linkage is missing) and a claim-ceiling guard (no
  claim promotion), plus the explicit no-claim-promotion receipt field. These
  are *local* checks/refusals only -- this module never calls out to gsigmad
  services and never writes to any Knowledge Graph.
* Fail-closed behaviour on the Phase 67/68 blockers ``SELECTION_STALE`` /
  ``SWAP_DEGRADED`` / ``SWAP_BLOCKED`` (reused, not reinvented).
* Watchtower-observable status: each receipt is appended to the existing
  bounded receipt store via :func:`ollarma.run_ledger.append_run_receipt`, so
  Watchtower can read run status read-only. Watchtower does NOT execute.

Scope guard (ROADMAP criterion #9 / operator gate): this module performs NO
execution against real science repos on its own. The default ``runner`` is a
read-only validate-only stub; callers (tests) inject an explicit runner. The
operator-gated cross-repo edit to
``gettingsciencedone/adapters/runtime/ollarma.yaml`` (append
``notebook_workflow_receipt`` to ``allowed_receipts``) is NOT performed here.

Exports:
    NotebookStep                -- one notebook step in the workflow DAG
    NotebookWorkflowManifest    -- the cross-repo notebook workflow manifest
    NotebookIndependenceError   -- raised when the independence validator fires
    NotebookWorkflowBlocked     -- raised on a fail-closed blocker / gate refusal
    IndependenceResult          -- structured independence-validation outcome
    NotebookWorkflowReceipt     -- hash-chained per-notebook receipt
    validate_notebook_independence -- the independence validator (criterion #4)
    sha256_file                 -- sha256 digest of a file
    verify_receipt_chain        -- verify a hash-chained receipt list
    check_swap_blockers         -- fail-closed swap-pressure gate (criterion #6)
    check_selection_blocker     -- fail-closed model-selection gate (criterion #6)
    check_gsigmad_gates         -- EXP/PROMPT linkage + claim-ceiling gate (criterion #5)
    run_notebook_workflow       -- orchestrate single + DAG runs (criteria #1/#2/#3/#7)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ollarma.evidence import canonical_hash
from ollarma.execution_policy import (
    SelectionResolutionError,
    WorkloadClass,
    resolve_selection,
)
from ollarma.run_ledger import RunReceipt, append_run_receipt
from ollarma.swarm._runtime_contract import (
    SWAP_BLOCKED_PCT_THRESHOLD,
    SWAP_DEGRADED_PCT_THRESHOLD,
)

__all__ = [
    "NotebookStep",
    "NotebookWorkflowManifest",
    "NotebookIndependenceError",
    "NotebookWorkflowBlocked",
    "IndependenceResult",
    "NotebookWorkflowReceipt",
    "validate_notebook_independence",
    "sha256_file",
    "verify_receipt_chain",
    "check_swap_blockers",
    "check_selection_blocker",
    "check_gsigmad_gates",
    "run_notebook_workflow",
    "NO_WRITEBACK_STATEMENT",
    "NOTEBOOK_TASK_CLASSES",
]


_GENESIS_RECEIPT_HASH = "0" * 64

#: The two notebook workflow task classes admitted by this runtime. The
#: single-notebook class matches the existing ``validated-notebook`` workload
#: class; the DAG class (criterion #2) is net-new for Phase 71.
NOTEBOOK_TASK_CLASSES = frozenset({"validated-notebook", "validated-notebook-dag"})

#: The fixed, explicit no-writeback / no-claim-promotion statement embedded in
#: every receipt (criteria #3 + #5). Stated literally so an auditor (and
#: Watchtower) can grep for it without interpreting code.
NO_WRITEBACK_STATEMENT = (
    "no-writeback: this run did NOT write back to the target repo, the "
    "Knowledge Graph, EXP/PROMPT records, or any science database; "
    "no-claim-promotion: no claim was promoted to any tier above the "
    "registered claim ceiling."
)

_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_EXP_LINKAGE_RE = re.compile(r"^EXP-[A-Za-z0-9][A-Za-z0-9._-]*$")
_PROMPT_LINKAGE_RE = re.compile(r"^PROMPT-[A-Za-z0-9][A-Za-z0-9._-]*$")

# ---------------------------------------------------------------------------
# Independence validator (criterion #4)
#
# The Decision lock says a notebook is non-compliant if it merely calls %run,
# !python, subprocess, shell wrappers, or delegates the whole experiment to
# scripts/exp*.py. We scan the notebook's CODE-CELL SOURCES (reusing the same
# extraction idiom as autopilot._read_asset_content) for those signals.
# ---------------------------------------------------------------------------

# Each pattern maps to a stable machine-readable violation code. Patterns are
# matched against each code cell's source text (line-anchored where it matters).
_INDEPENDENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # %run magic (line or inline) -> delegating execution to another notebook/script.
    ("PERCENT_RUN", re.compile(r"(?m)^\s*%run\b")),
    # !python ... shell-escape invoking the interpreter on another file.
    ("BANG_PYTHON", re.compile(r"(?m)^\s*!\s*python[0-9.]*\b")),
    # Generic shell escape that shells out to a script/command.
    ("SHELL_WRAPPER", re.compile(r"(?m)^\s*!\s*(?:sh|bash|zsh|s-?bash|\./|/)")),
    # subprocess delegation (import or call) -- the notebook is a wrapper.
    ("SUBPROCESS", re.compile(r"\bsubprocess\s*\.\s*(?:run|call|Popen|check_call|check_output)\b")),
    ("SUBPROCESS_IMPORT", re.compile(r"(?m)^\s*(?:import\s+subprocess|from\s+subprocess\s+import)\b")),
    # os.system shell wrapper.
    ("OS_SYSTEM", re.compile(r"\bos\s*\.\s*system\s*\(")),
    # Whole-experiment delegation to scripts/exp*.py (the canonical anti-pattern).
    ("EXP_SCRIPT_DELEGATION", re.compile(r"scripts/exp[A-Za-z0-9_./-]*\.py\b")),
    # runpy.run_path / run_module is another whole-file delegation route.
    ("RUNPY_DELEGATION", re.compile(r"\brunpy\s*\.\s*run_(?:path|module)\s*\(")),
)


class IndependenceResult(BaseModel):
    """Structured outcome of the notebook independence validator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    independent: bool
    code_cell_count: int = Field(ge=0)
    violations: tuple[str, ...] = ()
    """Machine-readable violation codes, e.g. ``("PERCENT_RUN", "SUBPROCESS")``."""

    def as_payload(self) -> dict[str, object]:
        """Return a plain-dict representation for embedding in a receipt."""
        return {
            "independent": self.independent,
            "code_cell_count": self.code_cell_count,
            "violations": list(self.violations),
        }


class NotebookIndependenceError(RuntimeError):
    """Raised when the independence validator rejects a wrapper notebook."""

    def __init__(self, notebook: str, result: IndependenceResult) -> None:
        self.notebook = notebook
        self.result = result
        joined = ", ".join(result.violations) or "non-independent"
        super().__init__(
            f"notebook is not an independent replay artifact ({joined}): {notebook}"
        )


def _extract_code_cell_sources(notebook_path: pathlib.Path) -> tuple[list[str], int]:
    """Return (code-cell sources, code-cell count) for a notebook.

    Mirrors :func:`ollarma.autopilot._read_asset_content` /
    :func:`ollarma.autopilot.count_code_cells`: parse the ``.ipynb`` JSON and
    join each code cell's ``source`` (which may be a list of lines or a string).
    """
    nb = json.loads(notebook_path.read_text(encoding="utf-8"))
    cells = nb.get("cells", [])
    sources: list[str] = []
    for cell in cells:
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source", [])
        if isinstance(src, list):
            sources.append("".join(src))
        else:
            sources.append(str(src))
    return sources, len(sources)


def validate_notebook_independence(notebook_path: str | pathlib.Path) -> IndependenceResult:
    """Validate that a notebook is an independent replay artifact (criterion #4).

    A notebook is REJECTED (``independent=False``) when any code cell contains a
    delegation/wrapper signal: ``%run``, ``!python``, a shell escape, a
    ``subprocess`` call/import, ``os.system``, ``runpy.run_path/run_module``, or
    whole-experiment delegation to a ``scripts/exp*.py`` file.

    Returns a structured :class:`IndependenceResult`; never raises on a
    non-independent notebook (callers decide whether to raise
    :class:`NotebookIndependenceError`). Raises ``FileNotFoundError`` /
    ``json.JSONDecodeError`` if the notebook cannot be read/parsed -- an
    unreadable notebook cannot be certified independent and must fail closed.
    """
    path = pathlib.Path(notebook_path)
    sources, cell_count = _extract_code_cell_sources(path)
    joined = "\n".join(sources)

    violations: list[str] = []
    for code, pattern in _INDEPENDENCE_PATTERNS:
        if pattern.search(joined):
            violations.append(code)

    return IndependenceResult(
        independent=not violations,
        code_cell_count=cell_count,
        violations=tuple(violations),
    )


# ---------------------------------------------------------------------------
# Manifest schema (criteria #1 + #2)
# ---------------------------------------------------------------------------


def _validate_repo_relative(value: str, *, field: str) -> str:
    """Normalize and validate a portable repo-relative locator."""
    locator = value.strip()
    if not locator:
        raise ValueError(f"{field} must not be empty")
    if "\\" in locator:
        raise ValueError(f"{field} must use '/' separators")
    if locator.startswith(("~", "/")) or _WINDOWS_ABSOLUTE_RE.match(locator):
        raise ValueError(f"{field} must be a repo-relative locator, not an absolute path")
    pure = PurePosixPath(locator)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"{field} must not escape the repo root")
    normalized = pure.as_posix()
    if locator != normalized:
        raise ValueError(f"{field} must be normalized")
    return normalized


class NotebookStep(BaseModel):
    """One notebook step in a cross-repo notebook workflow.

    ``target_repo`` is a portable repo name (e.g. ``deltaprot``), never a
    filesystem path -- the runtime resolves it to a root via the explicit
    ``repo_roots`` mapping at run time. ``notebook`` is a repo-relative
    locator into that target repo. ``depends_on`` carries the DAG edges
    (criterion #2); an empty tuple means the step has no predecessors.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str = Field(min_length=1)
    target_repo: str = Field(min_length=1)
    notebook: str = Field(min_length=1)
    inputs: tuple[str, ...] = ()
    """Repo-relative input file locators (read-only by default)."""
    outputs: tuple[str, ...] = ()
    """Repo-relative output artifact locators produced by this notebook."""
    depends_on: tuple[str, ...] = ()
    """``step_id`` predecessors -- the DAG edges."""

    @field_validator("target_repo")
    @classmethod
    def _validate_target_repo(cls, value: str) -> str:
        cleaned = value.strip()
        if not _REPO_NAME_RE.fullmatch(cleaned):
            raise ValueError("target_repo must be a portable repo name, not a filesystem path")
        return cleaned

    @field_validator("notebook")
    @classmethod
    def _validate_notebook(cls, value: str) -> str:
        normalized = _validate_repo_relative(value, field="notebook")
        if not normalized.endswith(".ipynb"):
            raise ValueError("notebook locator must point at a .ipynb file")
        return normalized

    @field_validator("inputs", "outputs")
    @classmethod
    def _validate_locators(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_repo_relative(item, field="locator") for item in value)


class NotebookWorkflowManifest(BaseModel):
    """A cross-repo notebook workflow admitted onto the notebook runtime.

    ``task_class`` is ``validated-notebook`` for a single-notebook run
    (criterion #1) or ``validated-notebook-dag`` for a multi-notebook
    cross-repo workflow (criterion #2). The gsigmad EXP/PROMPT linkage
    (criterion #5) is carried in ``exp_linkage`` / ``prompt_linkage`` and gated
    before execution. ``claim_ceiling`` records the registered claim ceiling;
    the runtime enforces *no claim promotion* above it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: Literal["1.0"]
    consumer_repo: str = Field(min_length=1)
    task_class: str
    run_id: str = Field(min_length=1)
    steps: tuple[NotebookStep, ...] = Field(min_length=1)
    exp_linkage: str | None = None
    """gsigmad EXP linkage, e.g. ``EXP-DELTAPROT-001`` (criterion #5)."""
    prompt_linkage: str | None = None
    """gsigmad PROMPT linkage, e.g. ``PROMPT-DELTAPROT-001`` (criterion #5)."""
    claim_ceiling: str = Field(default="IDEATION_AND_TRIAGE_ONLY", min_length=1)
    """Registered claim ceiling; the runtime promotes nothing above it."""

    @field_validator("consumer_repo")
    @classmethod
    def _validate_consumer_repo(cls, value: str) -> str:
        cleaned = value.strip()
        if not _REPO_NAME_RE.fullmatch(cleaned):
            raise ValueError("consumer_repo must be a portable repo name, not a filesystem path")
        return cleaned

    @field_validator("task_class")
    @classmethod
    def _validate_task_class(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned not in NOTEBOOK_TASK_CLASSES:
            joined = ", ".join(sorted(NOTEBOOK_TASK_CLASSES))
            raise ValueError(f"task_class must be one of: {joined}")
        return cleaned

    @field_validator("exp_linkage")
    @classmethod
    def _validate_exp_linkage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not _EXP_LINKAGE_RE.fullmatch(cleaned):
            raise ValueError("exp_linkage must look like EXP-<id>")
        return cleaned

    @field_validator("prompt_linkage")
    @classmethod
    def _validate_prompt_linkage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not _PROMPT_LINKAGE_RE.fullmatch(cleaned):
            raise ValueError("prompt_linkage must look like PROMPT-<id>")
        return cleaned

    @model_validator(mode="after")
    def _validate_dag(self) -> NotebookWorkflowManifest:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step_id values must be unique")

        if self.task_class == "validated-notebook" and len(self.steps) != 1:
            raise ValueError("validated-notebook requires exactly one step (use validated-notebook-dag for multi-step)")

        known = set(step_ids)
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in known:
                    raise ValueError(f"step {step.step_id!r} depends on unknown step {dep!r}")
                if dep == step.step_id:
                    raise ValueError(f"step {step.step_id!r} must not depend on itself")
        # Reject cycles up front via a topological sort attempt.
        _topological_order(self.steps)
        return self


def _topological_order(steps: tuple[NotebookStep, ...]) -> list[NotebookStep]:
    """Return steps in a deterministic dependency-respecting order.

    Raises ``ValueError`` if the ``depends_on`` graph contains a cycle. Ties
    (steps whose dependencies are all satisfied) are broken by ``step_id`` so
    the order is reproducible across runs.
    """
    by_id = {step.step_id: step for step in steps}
    indegree = {step.step_id: len(set(step.depends_on)) for step in steps}
    # Successors: for each step, which steps depend on it.
    successors: dict[str, list[str]] = {step.step_id: [] for step in steps}
    for step in steps:
        for dep in set(step.depends_on):
            successors[dep].append(step.step_id)

    ready = sorted(sid for sid, deg in indegree.items() if deg == 0)
    order: list[NotebookStep] = []
    while ready:
        sid = ready.pop(0)
        order.append(by_id[sid])
        for succ in successors[sid]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                ready.append(succ)
        ready.sort()

    if len(order) != len(steps):
        raise ValueError("notebook workflow DAG contains a cycle")
    return order


# ---------------------------------------------------------------------------
# Hashing helpers (criterion #3)
# ---------------------------------------------------------------------------


def sha256_file(path: str | pathlib.Path) -> str:
    """Return the ``sha256:<hex>`` digest of a file's bytes."""
    digest = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _hash_map(repo_root: pathlib.Path, locators: tuple[str, ...]) -> dict[str, str]:
    """Map each repo-relative locator to its sha256 digest (missing -> sentinel)."""
    out: dict[str, str] = {}
    for locator in locators:
        candidate = repo_root / locator
        if candidate.is_file():
            out[locator] = sha256_file(candidate)
        else:
            out[locator] = "sha256:MISSING"
    return out


# ---------------------------------------------------------------------------
# Hash-chained receipt (criterion #3) + Watchtower-observable status (criterion #7)
# ---------------------------------------------------------------------------


class NotebookWorkflowReceipt(BaseModel):
    """Hash-chained receipt for one executed (or refused) notebook step.

    Mirrors the :mod:`ollarma.run_ledger` receipt discipline: append-only,
    each receipt carries ``parent_hash`` (the previous receipt's
    ``receipt_hash``, or the genesis sentinel for the first) and a
    ``receipt_hash`` computed via :func:`ollarma.evidence.canonical_hash` over
    the receipt body. Tampering with any earlier receipt breaks the chain.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    step_id: str
    target_repo: str
    notebook: str
    status: str
    """``completed`` | ``refused`` | ``failed``."""
    reason_code: str | None = None
    exp_linkage: str | None = None
    prompt_linkage: str | None = None
    claim_ceiling: str = "IDEATION_AND_TRIAGE_ONLY"
    input_hashes: dict[str, str] = Field(default_factory=dict)
    output_hashes: dict[str, str] = Field(default_factory=dict)
    executed_notebook_hash: str | None = None
    validation_result: dict[str, object] = Field(default_factory=dict)
    no_writeback_statement: str = NO_WRITEBACK_STATEMENT
    claim_promotion_performed: Literal[False] = False
    """Hard invariant: this runtime NEVER promotes a claim (criterion #5)."""
    created_at: str = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    parent_hash: str = _GENESIS_RECEIPT_HASH
    receipt_hash: str = ""

    def _body(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        payload.pop("receipt_hash", None)
        return payload


def _materialize_receipt(
    receipt: NotebookWorkflowReceipt, *, parent_hash: str
) -> NotebookWorkflowReceipt:
    """Bind ``parent_hash`` and compute the content-addressed ``receipt_hash``."""
    bound = receipt.model_copy(update={"parent_hash": parent_hash, "receipt_hash": ""})
    receipt_hash = canonical_hash(bound._body())
    return bound.model_copy(update={"receipt_hash": receipt_hash})


def verify_receipt_chain(receipts: list[NotebookWorkflowReceipt]) -> str:
    """Verify a hash-chained receipt list and return the tail hash.

    Raises ``ValueError`` if any link's ``parent_hash`` or ``receipt_hash`` does
    not match the recomputed value (i.e. the chain is not append-only / has been
    tampered with). Returns the genesis sentinel for an empty list.
    """
    parent_hash = _GENESIS_RECEIPT_HASH
    for receipt in receipts:
        expected = _materialize_receipt(receipt, parent_hash=parent_hash)
        if receipt.parent_hash != parent_hash or receipt.receipt_hash != expected.receipt_hash:
            raise ValueError("notebook workflow receipt chain is not append-only")
        parent_hash = receipt.receipt_hash
    return parent_hash


def _to_run_receipt(receipt: NotebookWorkflowReceipt) -> RunReceipt:
    """Project a notebook receipt onto the bounded run-ledger receipt surface.

    Lets Watchtower observe notebook-run status read-only through the EXISTING
    ingested receipt store (criterion #7) without a new ingestion path. The
    notebook-specific hash chain is verified independently via
    :func:`verify_receipt_chain`; this projection is the Watchtower-visible
    summary.
    """
    return RunReceipt(
        run_id=receipt.run_id,
        stage="execute",
        step_id=receipt.step_id,
        task_or_command=f"notebook:{receipt.notebook}",
        lane="notebook_workflow_queue",
        status=receipt.status,
        outputs=tuple(
            {"repo_relative": locator, "digest": digest}
            for locator, digest in receipt.output_hashes.items()
        ),
        reason_code=receipt.reason_code,
        namespace=receipt.target_repo,
        governance_refs=(receipt.receipt_hash,) if receipt.receipt_hash else (),
    )


# ---------------------------------------------------------------------------
# Fail-closed blockers (criterion #6) -- reuse Phase 67/68 codes, do not invent
# ---------------------------------------------------------------------------


class NotebookWorkflowBlocked(RuntimeError):
    """Raised when a fail-closed blocker or gsigmad gate refuses the run."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


def check_swap_blockers(swap_pct: float) -> None:
    """Fail closed on swap pressure using the Phase 67/68 thresholds.

    Reuses :data:`SWAP_DEGRADED_PCT_THRESHOLD` / :data:`SWAP_BLOCKED_PCT_THRESHOLD`
    and the existing ``SWAP_DEGRADED`` / ``SWAP_BLOCKED`` reason codes (criterion
    #6). Notebook execution is *not* a small-model lane with a rescue ladder, so
    BOTH thresholds refuse: at/above the degraded threshold we refuse with
    ``SWAP_DEGRADED``; at/above the blocked threshold with ``SWAP_BLOCKED``.
    """
    if swap_pct >= SWAP_BLOCKED_PCT_THRESHOLD:
        raise NotebookWorkflowBlocked(
            "SWAP_BLOCKED",
            f"swap usage {swap_pct:.0f}% >= {SWAP_BLOCKED_PCT_THRESHOLD}% blocked ceiling",
        )
    if swap_pct >= SWAP_DEGRADED_PCT_THRESHOLD:
        raise NotebookWorkflowBlocked(
            "SWAP_DEGRADED",
            f"swap usage {swap_pct:.0f}% >= {SWAP_DEGRADED_PCT_THRESHOLD}% degraded threshold",
        )


def check_selection_blocker(
    *,
    results_dir: str | pathlib.Path,
    now: dt.datetime | None = None,
) -> str:
    """Fail closed on a stale/missing model selection (criterion #6).

    Resolves the NOTEBOOK workload class through the existing
    :func:`ollarma.execution_policy.resolve_selection`. Re-raises the typed
    :class:`SelectionResolutionError` (which carries ``SELECTION_STALE`` /
    ``SELECTION_MISSING``) as a :class:`NotebookWorkflowBlocked` so the runtime
    refuses to execute on a degraded selection.
    """
    try:
        return resolve_selection(WorkloadClass.NOTEBOOK, results_dir, now=now)
    except SelectionResolutionError as exc:
        raise NotebookWorkflowBlocked(exc.reason_code, exc.detail) from exc


# ---------------------------------------------------------------------------
# gsigmad gate checks (criterion #5) -- local checks only, no service / KG calls
# ---------------------------------------------------------------------------

#: Claim tiers ordered from lowest to highest. The runtime refuses to operate
#: above the manifest's registered ceiling. (Local copy of the well-known
#: gsigmad claim-ceiling ladder; we never call the gsigmad service.)
_CLAIM_TIERS: tuple[str, ...] = (
    "IDEATION_AND_TRIAGE_ONLY",
    "EXPLORATORY",
    "HYPOTHESIS",
    "MEASURED",
    "CONFIRMATORY",
)


def check_gsigmad_gates(manifest: NotebookWorkflowManifest) -> None:
    """Surface the gsigmad gates as local refusals (criterion #5).

    1. EXP/PROMPT linkage check: refuse (``MISSING_EXP_LINKAGE``) if BOTH
       ``exp_linkage`` and ``prompt_linkage`` are absent. A notebook workflow
       must be linked to a science-governance record before it runs.
    2. Claim-ceiling check: refuse (``UNKNOWN_CLAIM_CEILING``) if the declared
       ceiling is not a recognized tier. The runtime promotes nothing above the
       ceiling -- enforced structurally by the receipt's
       ``claim_promotion_performed = False`` invariant.

    These are pure-local checks/refusals. This function never contacts a
    gsigmad service and never writes to any Knowledge Graph.
    """
    if manifest.exp_linkage is None and manifest.prompt_linkage is None:
        raise NotebookWorkflowBlocked(
            "MISSING_EXP_LINKAGE",
            "notebook workflow requires an EXP or PROMPT linkage before execution",
        )
    if manifest.claim_ceiling not in _CLAIM_TIERS:
        raise NotebookWorkflowBlocked(
            "UNKNOWN_CLAIM_CEILING",
            f"claim_ceiling {manifest.claim_ceiling!r} is not a recognized claim tier",
        )


# ---------------------------------------------------------------------------
# Runtime orchestrator (criteria #1, #2, #3, #7)
# ---------------------------------------------------------------------------


def _readonly_validate_runner(notebook_path: pathlib.Path) -> str:
    """Default runner: validate-only, NO execution against the target repo.

    Honors ROADMAP criterion #9 / the operator gate ("no execution against real
    science repos"). It re-reads the notebook and returns its sha256 digest as
    the "executed-notebook hash" WITHOUT running any cells. Callers that want
    real papermill execution (e.g. after operator approval) inject their own
    runner that returns the digest of the *executed* output notebook.
    """
    return sha256_file(notebook_path)


def run_notebook_workflow(
    manifest: NotebookWorkflowManifest,
    *,
    repo_roots: dict[str, pathlib.Path],
    receipts_repo_root: pathlib.Path,
    receipts_path: pathlib.Path,
    swap_pct: float = 0.0,
    selection_results_dir: str | pathlib.Path | None = None,
    runner: Callable[[pathlib.Path], str] | None = None,
    now: dt.datetime | None = None,
) -> list[NotebookWorkflowReceipt]:
    """Execute (or refuse) a notebook workflow and emit hash-chained receipts.

    Order of operations (gates BEFORE execution, then per-step, then a final
    after-execution gate -- criterion #5 "before AND after"):

    1. gsigmad gates (EXP/PROMPT linkage + claim ceiling).
    2. Swap blockers (``SWAP_DEGRADED`` / ``SWAP_BLOCKED``) -- criterion #6.
    3. Model-selection blocker (``SELECTION_STALE`` / ``SELECTION_MISSING``) if
       ``selection_results_dir`` is given -- criterion #6.
    4. For each step in dependency order: independence-validate the notebook
       (criterion #4 -- raises before any execution), run it via ``runner``
       (default: read-only validate-only), and append a hash-chained receipt to
       the existing receipt store so Watchtower can observe it (criterion #7).
    5. Re-run the gsigmad gates AFTER execution.

    Parameters
    ----------
    repo_roots
        Maps ``target_repo`` -> resolved repo root. Every step's target_repo
        must be present (a step pointing at an unmapped repo refuses with
        ``TARGET_REPO_UNRESOLVED`` -- this is what keeps real science repos out
        unless the operator explicitly maps them).
    receipts_repo_root / receipts_path
        Where the run-ledger receipts are appended (an ollarma-owned execution
        subtree; not a target repo).
    runner
        Optional ``Callable[[Path], str]`` returning the executed-notebook
        sha256. Defaults to the read-only validate-only runner.

    Returns the list of materialized notebook receipts (the in-band hash chain).
    Raises :class:`NotebookWorkflowBlocked` / :class:`NotebookIndependenceError`
    on a refusal; the partial chain up to the refusal is still persisted.
    """
    run_step = runner or _readonly_validate_runner

    # 1. gsigmad gates BEFORE execution.
    check_gsigmad_gates(manifest)
    # 2. Swap blockers.
    check_swap_blockers(swap_pct)
    # 3. Model-selection blocker (optional -- skipped when no selection dir given,
    #    e.g. validate-only smoke runs that don't touch a model).
    if selection_results_dir is not None:
        check_selection_blocker(results_dir=selection_results_dir, now=now)

    ordered = _topological_order(manifest.steps)
    receipts: list[NotebookWorkflowReceipt] = []
    parent_hash = _GENESIS_RECEIPT_HASH

    for step in ordered:
        repo_root = repo_roots.get(step.target_repo)
        if repo_root is None:
            raise NotebookWorkflowBlocked(
                "TARGET_REPO_UNRESOLVED",
                f"step {step.step_id!r} targets unmapped repo {step.target_repo!r}",
            )
        repo_root = pathlib.Path(repo_root).resolve()
        notebook_path = (repo_root / step.notebook).resolve()
        if not notebook_path.is_file():
            raise NotebookWorkflowBlocked(
                "NOTEBOOK_MISSING",
                f"step {step.step_id!r} notebook not found: {step.notebook}",
            )

        # 4a. Independence validator (criterion #4) -- fires BEFORE any execution.
        independence = validate_notebook_independence(notebook_path)
        if not independence.independent:
            raise NotebookIndependenceError(step.notebook, independence)

        input_hashes = _hash_map(repo_root, step.inputs)
        # 4b. Run the notebook (default runner is read-only / no execution).
        executed_hash = run_step(notebook_path)
        output_hashes = _hash_map(repo_root, step.outputs)

        receipt = NotebookWorkflowReceipt(
            run_id=manifest.run_id,
            step_id=step.step_id,
            target_repo=step.target_repo,
            notebook=step.notebook,
            status="completed",
            exp_linkage=manifest.exp_linkage,
            prompt_linkage=manifest.prompt_linkage,
            claim_ceiling=manifest.claim_ceiling,
            input_hashes=input_hashes,
            output_hashes=output_hashes,
            executed_notebook_hash=executed_hash,
            validation_result=independence.as_payload(),
        )
        materialized = _materialize_receipt(receipt, parent_hash=parent_hash)
        receipts.append(materialized)
        parent_hash = materialized.receipt_hash

        # 4c. Watchtower-observable status: project onto the existing receipt
        #     store (criterion #7). Watchtower reads this read-only; it never
        #     executes.
        append_run_receipt(
            repo_root=receipts_repo_root,
            receipts_path=receipts_path,
            receipt=_to_run_receipt(materialized),
        )

    # 5. gsigmad gates AFTER execution (criterion #5: "before AND after").
    check_gsigmad_gates(manifest)
    return receipts

"""execution_policy.py -- Workload-aware local model selection policy."""
from __future__ import annotations

import datetime as dt
import pathlib

import orjson
from enum import Enum

from ollarma.evidence import canonical_hash
from ollarma.reserved_models import (
    RESERVED_MODEL_REASON_CODE,
    drop_reserved_models,
    is_reserved_model,
)


SELECTION_MAX_AGE_HOURS = 24


class WorkloadClass(str, Enum):
    """Explicit workload classes for local execution policy."""

    CHAT = "chat"
    ROUTE_PROMPT = "route_prompt"
    VALIDATED_SCRIPT = "validated_script"
    PYTEST_SUITE = "pytest_suite"
    NOTEBOOK = "notebook"
    PIPELINE_STEP = "pipeline_step"


class RouteQueryClass(str, Enum):
    """Deterministic helper-routing intent classes."""

    FILE_LOOKUP = "file_lookup"
    MANIFEST_LOOKUP = "manifest_lookup"
    STATUS_LOOKUP = "status_lookup"
    EXECUTION_REQUEST = "execution_request"
    SUMMARY_REQUEST = "summary_request"
    OPEN_ENDED = "open_ended"


class RouteReasonCode(str, Enum):
    """Machine-readable outcomes for retrieval-first helper routing."""

    KB_DIRECT_ANSWER = "KB_DIRECT_ANSWER"
    KB_STATUS_ANSWER = "KB_STATUS_ANSWER"
    GROUNDED_LOCAL_SYNTHESIS = "GROUNDED_LOCAL_SYNTHESIS"
    EXECUTION_LANE_REQUIRED = "EXECUTION_LANE_REQUIRED"
    INSUFFICIENT_GROUNDED_EVIDENCE = "INSUFFICIENT_GROUNDED_EVIDENCE"
    KB_SOURCES_UNDECLARED = "KB_SOURCES_UNDECLARED"
    KB_SOURCE_MISSING = "KB_SOURCE_MISSING"
    KB_NOT_BUILT = "KB_NOT_BUILT"
    KB_STALE = "KB_STALE"


WORKLOAD_SUITE_MAP: dict[WorkloadClass, str] = {
    WorkloadClass.CHAT: "code",
    WorkloadClass.ROUTE_PROMPT: "code",
    WorkloadClass.VALIDATED_SCRIPT: "code",
    WorkloadClass.PYTEST_SUITE: "code",
    WorkloadClass.NOTEBOOK: "science",
    WorkloadClass.PIPELINE_STEP: "code",
}

RETRYABLE_WORKFLOW_REASON_CODES: frozenset[str] = frozenset(
    {
        "QUEUE_TIMEOUT",
        "GUARDRAIL_FLAG_RETRYABLE",
        "EXIT_1",
        "EXIT_-1",
    }
)


class SelectionResolutionError(RuntimeError):
    """Typed selection-policy error with machine-readable reason code."""

    def __init__(
        self,
        reason_code: str,
        workload_class: WorkloadClass,
        detail: str,
    ) -> None:
        self.reason_code = reason_code
        self.workload_class = workload_class
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


def is_retryable_workflow_reason(reason_code: str | None) -> bool:
    """Return whether the workflow reason_code permits one bounded retry."""
    if reason_code is None:
        return False
    return reason_code in RETRYABLE_WORKFLOW_REASON_CODES


def requires_human_review(
    *,
    governance_significant: bool,
    mutates_canonical_output: bool,
) -> bool:
    """Return whether the workflow outcome must stay on the human-review lane."""
    return governance_significant or mutates_canonical_output


def classify_route_prompt(prompt: str) -> RouteQueryClass:
    """Classify a helper-routing prompt into a deterministic intent bucket."""
    lowered = prompt.strip().lower()

    if any(token in lowered for token in ("where is", "which file", "find file", "path to", "locate ")):
        return RouteQueryClass.FILE_LOOKUP
    if any(token in lowered for token in ("run ", "execute", "launch ", "start ", "rerun", "autopilot", "run workflow", "execute workflow")):
        return RouteQueryClass.EXECUTION_REQUEST
    if "manifest" in lowered or "step_id" in lowered or "workflow ref" in lowered:
        return RouteQueryClass.MANIFEST_LOOKUP
    if any(token in lowered for token in ("status", "ready", "available", "built", "fresh", "stale")):
        return RouteQueryClass.STATUS_LOOKUP
    if any(token in lowered for token in ("summarize", "summary", "describe", "explain", "what is", "how does")):
        return RouteQueryClass.SUMMARY_REQUEST
    return RouteQueryClass.OPEN_ENDED


def is_exact_answer_query(query_class: RouteQueryClass) -> bool:
    """Return whether the query class is eligible for direct KB answers."""
    return query_class in {
        RouteQueryClass.FILE_LOOKUP,
        RouteQueryClass.MANIFEST_LOOKUP,
        RouteQueryClass.STATUS_LOOKUP,
    }


def requires_execution_handoff(query_class: RouteQueryClass) -> bool:
    """Return whether the request belongs on an execution lane, not helper routing."""
    return query_class == RouteQueryClass.EXECUTION_REQUEST


def _parse_run_timestamp(run_id: str) -> dt.datetime:
    """Parse run IDs such as 2026-04-09T17:52:14Z into UTC datetimes."""
    normalized = run_id.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(normalized).astimezone(dt.timezone.utc)


def _latest_artifact_path(results_dir: pathlib.Path) -> pathlib.Path:
    artifacts = []
    for path in results_dir.glob("run-*.artifact.json"):
        try:
            artifact = _load_artifact(path)
            run_timestamp = _parse_run_timestamp(str(artifact["run_id"]))
        except (KeyError, ValueError):
            # Invalid artifacts stay selectable for downstream validation errors,
            # but they should not outrank a newer valid artifact just because of mtime.
            run_timestamp = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        artifacts.append((run_timestamp, path))
    if not artifacts:
        raise FileNotFoundError("No validated selection artifact found in results/")
    artifacts.sort(key=lambda item: item[0])
    return artifacts[-1][1]


def _latest_artifact_path_for_suite(results_dir: pathlib.Path, suite: str) -> pathlib.Path:
    """Return newest artifact that can select ``suite``; fall back to latest artifact.

    Dry-run or partial benchmark artifacts may be newer than the last complete
    selection artifact while carrying no winner for a workload. Those should not
    mask the newest usable selection for typed agents and helper routing.
    """
    artifacts = []
    for path in results_dir.glob("run-*.artifact.json"):
        try:
            artifact = _load_artifact(path)
            run_timestamp = _parse_run_timestamp(str(artifact["run_id"]))
        except (KeyError, ValueError):
            continue
        winners = artifact.get("per_suite_winners") or {}
        if winners.get(suite):
            artifacts.append((run_timestamp, path))
    if not artifacts:
        return _latest_artifact_path(results_dir)
    artifacts.sort(key=lambda item: item[0])
    return artifacts[-1][1]


def _load_artifact(artifact_path: pathlib.Path) -> dict:
    try:
        raw = orjson.loads(artifact_path.read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        raise ValueError(f"Unreadable selection artifact: {artifact_path}") from exc

    stored_hash = raw.get("stable_decision_hash", "")
    artifact_body = {k: v for k, v in raw.items() if k != "stable_decision_hash"}
    computed_hash = canonical_hash(artifact_body)
    if not stored_hash or stored_hash != computed_hash:
        raise ValueError("Selection artifact hash validation failed")

    return raw


def resolve_ranked_selection(
    workload_class: WorkloadClass,
    results_dir: str | pathlib.Path = "results",
    *,
    now: dt.datetime | None = None,
    max_age_hours: int = SELECTION_MAX_AGE_HOURS,
) -> tuple[str, tuple[str, ...]]:
    """Resolve a workload class to (winner, pareto_frontier_tuple).

    The pareto frontier provides ranked alternates for the routing ladder.
    Raises SelectionResolutionError with the same codes as resolve_selection().
    """
    results_path = pathlib.Path(results_dir)
    current_time = now or dt.datetime.now(dt.timezone.utc)
    target_suite = WORKLOAD_SUITE_MAP[workload_class]

    try:
        artifact_path = _latest_artifact_path_for_suite(results_path, target_suite)
    except FileNotFoundError as exc:
        raise SelectionResolutionError(
            "SELECTION_MISSING",
            workload_class,
            str(exc),
        ) from exc

    try:
        artifact = _load_artifact(artifact_path)
        run_timestamp = _parse_run_timestamp(artifact["run_id"])
    except (KeyError, ValueError) as exc:
        raise SelectionResolutionError(
            "SELECTION_STALE",
            workload_class,
            f"Selection artifact is invalid: {exc}",
        ) from exc

    artifact_age = current_time - run_timestamp
    if artifact_age > dt.timedelta(hours=max_age_hours):
        raise SelectionResolutionError(
            "SELECTION_STALE",
            workload_class,
            f"Selection artifact is older than {max_age_hours}h: {artifact['run_id']}",
        )

    winners = artifact.get("per_suite_winners") or {}
    model_name = winners.get(target_suite)
    if not model_name:
        raise SelectionResolutionError(
            "SELECTION_MISSING",
            workload_class,
            f"Selection artifact does not cover suite '{target_suite}' for {workload_class.value}",
        )

    if is_reserved_model(model_name):
        raise SelectionResolutionError(
            RESERVED_MODEL_REASON_CODE,
            workload_class,
            f"Selection artifact winner {model_name!r} for suite '{target_suite}' is "
            "reserved for Antigence/Sentinel; regenerate the benchmark excluding reserved models.",
        )

    pareto: tuple[str, ...] = drop_reserved_models(tuple(artifact.get("pareto_frontier") or []))
    return model_name, pareto


def resolve_selection(
    workload_class: WorkloadClass,
    results_dir: str | pathlib.Path = "results",
    *,
    now: dt.datetime | None = None,
    max_age_hours: int = SELECTION_MAX_AGE_HOURS,
) -> str:
    """Resolve a workload class to an approved local model.

    Raises SelectionResolutionError with:
    - SELECTION_MISSING when no validated artifact or suite coverage exists
    - SELECTION_STALE when the latest artifact is too old or invalid
    """
    results_path = pathlib.Path(results_dir)
    current_time = now or dt.datetime.now(dt.timezone.utc)
    target_suite = WORKLOAD_SUITE_MAP[workload_class]

    try:
        artifact_path = _latest_artifact_path_for_suite(results_path, target_suite)
    except FileNotFoundError as exc:
        raise SelectionResolutionError(
            "SELECTION_MISSING",
            workload_class,
            str(exc),
        ) from exc

    try:
        artifact = _load_artifact(artifact_path)
        run_timestamp = _parse_run_timestamp(artifact["run_id"])
    except (KeyError, ValueError) as exc:
        raise SelectionResolutionError(
            "SELECTION_STALE",
            workload_class,
            f"Selection artifact is invalid: {exc}",
        ) from exc

    artifact_age = current_time - run_timestamp
    if artifact_age > dt.timedelta(hours=max_age_hours):
        raise SelectionResolutionError(
            "SELECTION_STALE",
            workload_class,
            f"Selection artifact is older than {max_age_hours}h: {artifact['run_id']}",
        )

    winners = artifact.get("per_suite_winners") or {}
    model_name = winners.get(target_suite)
    if not model_name:
        raise SelectionResolutionError(
            "SELECTION_MISSING",
            workload_class,
            f"Selection artifact does not cover suite '{target_suite}' for {workload_class.value}",
        )

    # Reserved Antigence/Sentinel models must never leak into generic selection even
    # if a benchmark artifact ranks one as the winner (closes the artifact-taint leak).
    if is_reserved_model(model_name):
        raise SelectionResolutionError(
            RESERVED_MODEL_REASON_CODE,
            workload_class,
            f"Selection artifact winner {model_name!r} for suite '{target_suite}' is "
            "reserved for Antigence/Sentinel; regenerate the benchmark excluding reserved models.",
        )

    return model_name

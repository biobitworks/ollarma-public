"""receipts_trace.py -- Chain walk: escalation -> admission -> FrontierReceipt.

Phase 57.1-03 (OBS-59). Closes F-04: gives operators a CLI that walks the
gateway chain end-to-end and validates hash linkage.

Public surface
--------------
- ``TraceRow``     -- frozen dataclass; one row per stage (escalation/admission/frontier).
- ``TraceResult``  -- frozen dataclass; list of rows + aggregate chain-intact flag
  + exit code (0 on valid chain, 1 on mismatch / missing required step).
- ``trace(escalation_receipt_id, repo_root)`` -- pure scan + compute; no stdout.
- ``render_table(result, use_tty)`` -- Rich for TTY, plain ASCII otherwise.

Linkage semantics
-----------------
Per 57-CONTEXT D-06 / D-15:

  admission.escalation_receipt_content_hash
    == canonical_hash(escalation_receipt.model_dump(mode="json"))

  frontier.admission_receipt_hash == admission.receipt_hash  (cross-stream bind)

If the escalation receipt payload cannot be located locally (scribe / incidents)
but the admission + frontier are present, the escalation row is flagged
``EXTERNAL_ESCALATION`` and the chain is still considered intact (exit 0). This
is the legitimate cross-project case — the caller's ``.ollarma/`` may not be
the same dir as the gateway's.

Failure taxonomy
----------------
Narrow excepts only (FileNotFoundError, orjson.JSONDecodeError, json.JSONDecodeError,
OSError). No bare ``except Exception``.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any, Literal

import orjson
import pydantic

from ollarma.escalation import EscalationReceipt
from ollarma.evidence import canonical_hash


__all__ = [
    "TraceRow",
    "TraceResult",
    "trace",
    "render_table",
]


LinkageStatus = Literal[
    "MATCH",
    "MISMATCH",
    "EXTERNAL_ESCALATION",
    "MISSING_ADMISSION",
    "MISSING_FRONTIER",
    "N/A",
]


@dataclasses.dataclass(frozen=True)
class TraceRow:
    """One row in the chain-walk output table."""

    stage: Literal["escalation", "admission", "frontier"]
    timestamp: str  # ISO-8601 UTC or "(external)" / "(missing)"
    hash: str  # canonical_hash or store receipt_hash; empty if missing
    linkage_status: LinkageStatus
    linkage_detail: str


@dataclasses.dataclass(frozen=True)
class TraceResult:
    """Aggregate result of a chain-walk."""

    rows: list[TraceRow]
    exit_code: int
    chain_intact: bool


# ---------------------------------------------------------------------------
# Scan helpers (no side effects beyond reading files)
# ---------------------------------------------------------------------------

def _iter_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    """Return parsed JSONL records; skip malformed lines; empty if missing."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, "rb") as f:
            for raw in f:
                stripped = raw.rstrip(b"\n")
                if not stripped:
                    continue
                try:
                    out.append(orjson.loads(stripped))
                except orjson.JSONDecodeError:
                    # Malformed lines do not break trace; they simply do not match.
                    continue
    except (OSError, FileNotFoundError):
        return []
    return out


def _scan_escalation_payload(
    escalation_receipt_id: str, repo_root: pathlib.Path,
) -> dict[str, Any] | None:
    """Attempt to locate the escalation receipt payload by id.

    Strategy (all sources are scanned; first match wins):
      1. ``.ollarma/escalations/<id>.json`` (optional future file layout)
      2. ``.ollarma/session-log.jsonl`` -- scan notes + decisions + artifacts
         for a JSON-embedded escalation receipt; match when the computed
         ``er-<hash[:16]>`` id matches the target.
      3. ``.ollarma/incidents/*.json`` -- recovery packets that may embed an
         escalation receipt in a nested ``escalation_receipt`` field.

    Returns the parsed dict payload, or ``None`` if not found locally.
    """
    ollarma_dir = repo_root / ".ollarma"

    # (1) Direct file lookup.
    direct = ollarma_dir / "escalations" / f"{escalation_receipt_id}.json"
    if direct.exists():
        try:
            return orjson.loads(direct.read_bytes())
        except (orjson.JSONDecodeError, OSError):
            pass

    # (2) Scribe session log.
    session_log = ollarma_dir / "session-log.jsonl"
    for entry in _iter_jsonl(session_log):
        for field in ("notes", "next_action"):
            text = entry.get(field, "")
            if not isinstance(text, str) or escalation_receipt_id not in text:
                continue
            # Try to extract a JSON object containing the id.
            payload = _try_extract_receipt_payload(text, escalation_receipt_id)
            if payload is not None:
                return payload
        # Decisions can be a list of strings.
        for dec in entry.get("decisions", []) or []:
            if not isinstance(dec, str) or escalation_receipt_id not in dec:
                continue
            payload = _try_extract_receipt_payload(dec, escalation_receipt_id)
            if payload is not None:
                return payload

    # (3) Incidents directory.
    incidents_dir = ollarma_dir / "incidents"
    if incidents_dir.exists():
        try:
            for incident_file in sorted(incidents_dir.glob("*.json")):
                try:
                    content = orjson.loads(incident_file.read_bytes())
                except (orjson.JSONDecodeError, OSError):
                    continue
                if not isinstance(content, dict):
                    continue
                er = content.get("escalation_receipt")
                if isinstance(er, dict) and _matches_er_id(er, escalation_receipt_id):
                    return er
        except OSError:
            pass

    return None


def _matches_er_id(payload: dict[str, Any], escalation_receipt_id: str) -> bool:
    """Return True when canonical_hash(payload)[:16] matches the id suffix."""
    try:
        h = canonical_hash(payload)
    except (TypeError, ValueError):
        return False
    return escalation_receipt_id == f"er-{h[:16]}"


def _try_extract_receipt_payload(
    text: str, escalation_receipt_id: str,
) -> dict[str, Any] | None:
    """Find a JSON object in ``text`` whose canonical-hash id matches the target.

    Uses a lightweight brace-balancing scan to handle JSON embedded inside
    narrative notes. Falls back to None when parsing fails.
    """
    start = 0
    while True:
        open_idx = text.find("{", start)
        if open_idx == -1:
            return None
        depth = 0
        end_idx = -1
        in_str = False
        esc = False
        for i in range(open_idx, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        if end_idx == -1:
            return None
        candidate = text[open_idx:end_idx]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            start = open_idx + 1
            continue
        if isinstance(parsed, dict) and _matches_er_id(parsed, escalation_receipt_id):
            return parsed
        start = end_idx


# ---------------------------------------------------------------------------
# Public trace
# ---------------------------------------------------------------------------

def trace(
    escalation_receipt_id: str, repo_root: pathlib.Path,
) -> TraceResult:
    """Walk escalation -> admission -> frontier for a given id.

    Deterministic. Reads only; never writes. Output row order is fixed:
    escalation, admission, frontier.
    """
    root = pathlib.Path(repo_root).resolve()
    gateway_dir = root / ".ollarma" / "gateway"
    admissions_path = gateway_dir / "admissions.jsonl"
    receipts_path = gateway_dir / "receipts.jsonl"

    admissions = _iter_jsonl(admissions_path)
    receipts = _iter_jsonl(receipts_path)

    # Locate admission + frontier by escalation_receipt_id.
    matching_admissions = [
        a for a in admissions
        if a.get("escalation_receipt_id") == escalation_receipt_id
    ]
    matching_frontiers = [
        r for r in receipts
        if r.get("escalation_receipt_id") == escalation_receipt_id
    ]

    admission = matching_admissions[0] if matching_admissions else None
    frontier = matching_frontiers[0] if matching_frontiers else None

    # Try to find the escalation receipt payload locally.
    er_payload = _scan_escalation_payload(escalation_receipt_id, root)

    rows: list[TraceRow] = []
    chain_intact = True

    # --- Escalation row --------------------------------------------------------
    if er_payload is not None:
        try:
            # Validate it's a proper EscalationReceipt shape before trusting.
            EscalationReceipt.model_validate(er_payload)
            validated = True
        except pydantic.ValidationError:
            validated = False
        if validated:
            er_hash = canonical_hash(er_payload)
            ts = er_payload.get("created_at", "")
            rows.append(
                TraceRow(
                    stage="escalation",
                    timestamp=ts if isinstance(ts, str) else "",
                    hash=er_hash,
                    linkage_status="N/A",
                    linkage_detail=f"id={escalation_receipt_id}",
                )
            )
        else:
            # Treat unparseable payload as external so we do not silently pass.
            er_payload = None

    if er_payload is None:
        # Escalation row is external / missing locally.
        if admission is not None or frontier is not None:
            rows.append(
                TraceRow(
                    stage="escalation",
                    timestamp="(external)",
                    hash="",
                    linkage_status="EXTERNAL_ESCALATION",
                    linkage_detail=(
                        "escalation payload not found in local .ollarma/; "
                        "gateway knows the id (legitimate for sibling-project submits)"
                    ),
                )
            )
        else:
            # Nothing found anywhere for this id -> chain is not intact.
            rows.append(
                TraceRow(
                    stage="escalation",
                    timestamp="(missing)",
                    hash="",
                    linkage_status="EXTERNAL_ESCALATION",
                    linkage_detail=(
                        f"no admission, frontier, or local escalation record "
                        f"for id={escalation_receipt_id}"
                    ),
                )
            )
            chain_intact = False

    # --- Admission row --------------------------------------------------------
    if admission is None:
        rows.append(
            TraceRow(
                stage="admission",
                timestamp="(missing)",
                hash="",
                linkage_status="MISSING_ADMISSION",
                linkage_detail=(
                    f"no admission entry in admissions.jsonl for "
                    f"escalation_receipt_id={escalation_receipt_id}"
                ),
            )
        )
        chain_intact = False
    else:
        admission_hash = admission.get("receipt_hash", "") or ""
        admission_ts = admission.get("created_at", "") or ""
        content_hash_on_admission = admission.get(
            "escalation_receipt_content_hash", ""
        )
        # Compute linkage: admission's content hash vs computed ER content hash.
        if er_payload is None:
            linkage_status: LinkageStatus = "EXTERNAL_ESCALATION"
            linkage_detail = (
                f"admission.escalation_receipt_content_hash="
                f"{str(content_hash_on_admission)[:16]}..."
                f" (escalation payload not available locally to cross-check)"
            )
        else:
            computed_er_hash = canonical_hash(er_payload)
            if computed_er_hash == content_hash_on_admission:
                linkage_status = "MATCH"
                linkage_detail = (
                    f"admission.escalation_receipt_content_hash "
                    f"== canonical_hash(escalation) [{computed_er_hash[:16]}...]"
                )
            else:
                linkage_status = "MISMATCH"
                linkage_detail = (
                    f"HASH_MISMATCH: admission recorded "
                    f"{str(content_hash_on_admission)[:16]}... but escalation "
                    f"canonical_hash is {computed_er_hash[:16]}..."
                )
                chain_intact = False
        rows.append(
            TraceRow(
                stage="admission",
                timestamp=admission_ts,
                hash=admission_hash,
                linkage_status=linkage_status,
                linkage_detail=linkage_detail,
            )
        )

    # --- Frontier row ---------------------------------------------------------
    if frontier is None:
        rows.append(
            TraceRow(
                stage="frontier",
                timestamp="(missing)",
                hash="",
                linkage_status="MISSING_FRONTIER",
                linkage_detail=(
                    f"no frontier receipt in receipts.jsonl for "
                    f"escalation_receipt_id={escalation_receipt_id}"
                ),
            )
        )
        chain_intact = False
    else:
        frontier_hash = frontier.get("receipt_hash", "") or ""
        frontier_ts = frontier.get("created_at", "") or ""
        admission_ref = frontier.get("admission_receipt_hash", "") or ""
        if admission is None:
            linkage_status_f: LinkageStatus = "MISSING_ADMISSION"
            linkage_detail_f = (
                f"frontier.admission_receipt_hash={admission_ref[:16]}... but "
                f"no admission entry matches escalation_receipt_id"
            )
            chain_intact = False
        else:
            admission_hash = admission.get("receipt_hash", "") or ""
            if admission_ref == admission_hash and admission_ref != "":
                linkage_status_f = "MATCH"
                linkage_detail_f = (
                    f"frontier.admission_receipt_hash == admission.receipt_hash "
                    f"[{admission_ref[:16]}...]"
                )
            else:
                linkage_status_f = "MISMATCH"
                linkage_detail_f = (
                    f"HASH_MISMATCH: frontier references "
                    f"{admission_ref[:16]}... but admission.receipt_hash is "
                    f"{admission_hash[:16]}..."
                )
                chain_intact = False
        rows.append(
            TraceRow(
                stage="frontier",
                timestamp=frontier_ts,
                hash=frontier_hash,
                linkage_status=linkage_status_f,
                linkage_detail=linkage_detail_f,
            )
        )

    exit_code = 0 if chain_intact else 1
    return TraceResult(rows=rows, exit_code=exit_code, chain_intact=chain_intact)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_COL_HEADERS = ("Stage", "Timestamp (UTC)", "Hash", "Linkage", "Detail")


def _short_hash(h: str) -> str:
    if not h:
        return "-"
    return h[:16] + "..." if len(h) > 16 else h


def _render_plain(result: TraceResult) -> str:
    rows = [
        (
            r.stage,
            r.timestamp or "-",
            _short_hash(r.hash),
            r.linkage_status,
            r.linkage_detail,
        )
        for r in result.rows
    ]
    cols = list(zip(_COL_HEADERS, *rows)) if rows else [(h,) for h in _COL_HEADERS]
    widths = [max(len(str(cell)) for cell in col) for col in cols]
    lines = []
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    header_cells = [f" {h:<{w}} " for h, w in zip(_COL_HEADERS, widths)]
    lines.append(sep)
    lines.append("|" + "|".join(header_cells) + "|")
    lines.append(sep)
    for r in rows:
        cells = [f" {str(c):<{w}} " for c, w in zip(r, widths)]
        lines.append("|" + "|".join(cells) + "|")
    lines.append(sep)
    footer = (
        f"chain_intact={result.chain_intact}  exit_code={result.exit_code}"
    )
    lines.append(footer)
    return "\n".join(lines)


def _render_rich(result: TraceResult) -> str:
    # Lazy import so non-TTY callers do not pay Rich import cost.
    from rich.console import Console  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415
    import io  # noqa: PLC0415

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=140)
    t = Table(title="Gateway Receipt Chain", show_lines=False)
    for h in _COL_HEADERS:
        t.add_column(h)
    for r in result.rows:
        style = None
        if r.linkage_status == "MATCH":
            style = "green"
        elif r.linkage_status in ("MISMATCH", "MISSING_ADMISSION", "MISSING_FRONTIER"):
            style = "red"
        elif r.linkage_status == "EXTERNAL_ESCALATION":
            style = "yellow"
        t.add_row(
            r.stage,
            r.timestamp or "-",
            _short_hash(r.hash),
            r.linkage_status,
            r.linkage_detail,
            style=style,
        )
    console.print(t)
    console.print(
        f"chain_intact={result.chain_intact}  exit_code={result.exit_code}"
    )
    return buf.getvalue()


def render_table(result: TraceResult, use_tty: bool) -> str:
    """Render a trace result. Rich table for TTY, plain ASCII otherwise."""
    if use_tty:
        return _render_rich(result)
    return _render_plain(result)

"""scribe.py -- Session scribe for token-loss resilience.

Append-only structured progress log that survives Claude/ChatGPT token limits.
Claude agents call scribe_progress() during work (not after). Each entry is
written to disk immediately as JSONL + a human-readable RESUME.md summary.

Design:
  - Append-only JSONL at {project_root}/.ollarma/session-log.jsonl
  - Human-readable RESUME.md regenerated on each write
  - No local model inference required (pure file I/O for speed)
  - Optional: local model summarization via separate endpoint
"""
from __future__ import annotations

import datetime
import logging
import pathlib
from typing import Any, Literal

import orjson
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIBE_DIR = ".ollarma"
SESSION_LOG = "session-log.jsonl"
RESUME_FILE = "RESUME.md"
MAX_LOG_ENTRIES = 500  # Rotate after this many entries per project


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ScribeEntry(BaseModel):
    """Single progress entry written by a Claude/ChatGPT agent."""

    timestamp: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    project: str  # e.g., "ollarma", "cellico-bio", "watchtower"
    phase: str = ""  # e.g., "39" or "v5.0-research"
    task: str = ""  # e.g., "39-01-T2" or "requirements definition"
    state: Literal["started", "in_progress", "completed", "blocked", "paused"] = "in_progress"
    artifacts: list[str] = Field(default_factory=list)  # Files created/modified
    notes: str = ""  # Freeform context for the next session
    decisions: list[str] = Field(default_factory=list)  # Key decisions made
    next_action: str = ""  # What to do next if session dies


class ScribeResult(BaseModel):
    """Result of a scribe_progress call."""

    written: bool
    log_path: str
    resume_path: str
    entry_count: int
    message: str


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def _ensure_dir(project_root: pathlib.Path) -> pathlib.Path:
    """Ensure .ollarma/ directory exists under project_root."""
    scribe_dir = project_root / SCRIBE_DIR
    scribe_dir.mkdir(parents=True, exist_ok=True)
    return scribe_dir


def scribe_progress(
    project_root: str | pathlib.Path,
    entry: ScribeEntry,
) -> ScribeResult:
    """Append a progress entry to the session log and regenerate RESUME.md.

    This is the hot path — called during work, not after. Must be fast
    (no model inference, pure file I/O).
    """
    root = pathlib.Path(project_root)
    scribe_dir = _ensure_dir(root)
    log_path = scribe_dir / SESSION_LOG
    resume_path = scribe_dir / RESUME_FILE

    # Append JSONL entry (atomic-ish: write + flush)
    raw = orjson.dumps(entry.model_dump(), option=orjson.OPT_SORT_KEYS)
    with open(log_path, "ab") as f:
        f.write(raw + b"\n")

    # Count entries
    entry_count = sum(1 for _ in open(log_path, "rb"))

    # Regenerate RESUME.md from recent entries
    _regenerate_resume(log_path, resume_path, entry.project)

    logger.info("scribe: %s phase=%s task=%s state=%s", entry.project, entry.phase, entry.task, entry.state)

    return ScribeResult(
        written=True,
        log_path=str(log_path),
        resume_path=str(resume_path),
        entry_count=entry_count,
        message=f"Progress logged for {entry.project} ({entry.state})",
    )


def read_resume(project_root: str | pathlib.Path) -> str:
    """Read the current RESUME.md for a project. Returns empty string if none."""
    resume_path = pathlib.Path(project_root) / SCRIBE_DIR / RESUME_FILE
    if resume_path.exists():
        return resume_path.read_text()
    return ""


def read_session_log(
    project_root: str | pathlib.Path,
    last_n: int = 20,
) -> list[dict[str, Any]]:
    """Read the last N entries from the session log."""
    log_path = pathlib.Path(project_root) / SCRIBE_DIR / SESSION_LOG
    if not log_path.exists():
        return []
    lines = log_path.read_bytes().strip().split(b"\n")
    entries = []
    for line in lines[-last_n:]:
        if line.strip():
            entries.append(orjson.loads(line))
    return entries


# ---------------------------------------------------------------------------
# RESUME.md generation (pure text, no LLM)
# ---------------------------------------------------------------------------

def _regenerate_resume(
    log_path: pathlib.Path,
    resume_path: pathlib.Path,
    project: str,
) -> None:
    """Regenerate RESUME.md from the session log.

    Groups by phase, shows latest state per task, lists decisions and
    next actions. Pure string formatting — no model inference.
    """
    entries = []
    for line in log_path.read_bytes().strip().split(b"\n"):
        if line.strip():
            entries.append(orjson.loads(line))

    if not entries:
        return

    # Group by phase, latest entry per task wins
    phases: dict[str, dict[str, dict]] = {}
    all_decisions: list[str] = []
    latest_next_action = ""

    for e in entries:
        phase = e.get("phase", "(no phase)")
        task = e.get("task", "(no task)")
        if phase not in phases:
            phases[phase] = {}
        phases[phase][task] = e
        for d in e.get("decisions", []):
            if d and d not in all_decisions:
                all_decisions.append(d)
        if e.get("next_action"):
            latest_next_action = e["next_action"]

    # Find the most recent entry overall
    latest = entries[-1]
    last_ts = latest.get("timestamp", "unknown")

    # Build markdown
    lines = [
        f"# Session Resume — {project}",
        "",
        f"**Last update:** {last_ts}",
        f"**State:** {latest.get('state', 'unknown')}",
        f"**Entries:** {len(entries)}",
        "",
    ]

    if latest_next_action:
        lines.extend([
            "## Next Action",
            "",
            latest_next_action,
            "",
        ])

    for phase_name, tasks in phases.items():
        lines.extend([f"## Phase: {phase_name}", ""])
        lines.append("| Task | State | Notes |")
        lines.append("|------|-------|-------|")
        for task_name, task_data in tasks.items():
            state = task_data.get("state", "?")
            notes = task_data.get("notes", "").replace("\n", " ")[:80]
            lines.append(f"| {task_name} | {state} | {notes} |")
        lines.append("")

        # Artifacts from latest entries in this phase
        artifacts = set()
        for task_data in tasks.values():
            for a in task_data.get("artifacts", []):
                artifacts.add(a)
        if artifacts:
            lines.append("**Artifacts:**")
            for a in sorted(artifacts):
                lines.append(f"- `{a}`")
            lines.append("")

    if all_decisions:
        lines.extend(["## Decisions Made", ""])
        for d in all_decisions[-10:]:  # Last 10 decisions
            lines.append(f"- {d}")
        lines.append("")

    lines.extend([
        "---",
        f"*Auto-generated by ollarma scribe. {len(entries)} entries in session-log.jsonl*",
    ])

    resume_path.write_text("\n".join(lines) + "\n")

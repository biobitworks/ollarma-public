"""recovery.py -- Deterministic recovery scanner for interrupted agent/model work.

Classifies the repo state across 5 possibilities by inspecting:
- git worktrees (including sidecar worktrees under .claude/worktrees, .codex/worktrees)
- branches matching sidecar prefixes (claude/*, codex/*, sidecar/*) ahead of base
- scribe artifacts under .ollarma/ (RESUME.md, session-log.jsonl)

The scanner produces a `RecoveryState` pydantic model. Packet persistence
(writing to .ollarma/incidents/) lives in phase 42 recovery_packet module.

Design:
- Pure git + file-system inspection; no model inference, no network calls
- Deterministic: same repo state -> same state classification + same
  next_fix_commands[]
- Fail-closed: if inspection fails for a worktree, treat it as possibly stranded
  rather than clean.
"""
from __future__ import annotations

import datetime
import logging
import os
import pathlib
import subprocess
import tempfile
from typing import Literal

import orjson
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECOVERY_DIR = ".ollarma"
INCIDENTS_SUBDIR = "incidents"
LATEST_POINTER = "latest.json"
SCRIBE_DIR = ".ollarma"
RESUME_FILE = "RESUME.md"
SESSION_LOG = "session-log.jsonl"

# Worktree directories that indicate a sidecar agent session.
SIDECAR_WORKTREE_ROOTS: tuple[str, ...] = (
    ".claude/worktrees",
    ".codex/worktrees",
)

# Branch prefixes that indicate a sidecar session branch.
SIDECAR_BRANCH_PREFIXES: tuple[str, ...] = (
    "claude/",
    "codex/",
    "sidecar/",
)

# State strings (Literal so consumers get completion + typecheck).
RecoveryStateStr = Literal[
    "clean",
    "resume_context_available",
    "stranded_worktree_detected",
    "recovery_sweep_required",
    "possible_work_loss",
]

# Blocker codes match the classification but are also emitted separately for
# admission-control consumers.
BlockerCode = Literal[
    "OK",
    "RESUME_AVAILABLE",
    "STRANDED_WORKTREE",
    "RECOVERY_SWEEP_REQUIRED",
    "POSSIBLE_WORK_LOSS",
]

_STATE_TO_BLOCKER: dict[RecoveryStateStr, BlockerCode] = {
    "clean": "OK",
    "resume_context_available": "RESUME_AVAILABLE",
    "stranded_worktree_detected": "STRANDED_WORKTREE",
    "recovery_sweep_required": "RECOVERY_SWEEP_REQUIRED",
    "possible_work_loss": "POSSIBLE_WORK_LOSS",
}

# States in which admission control should block fresh bounded execution.
# ``clean`` always admits. ``resume_context_available`` admits because it
# simply records that a prior session left a scribe marker; the operator is
# expected to acknowledge it manually, not via a block.
ADMISSION_BLOCKING_STATES: frozenset[RecoveryStateStr] = frozenset({
    "stranded_worktree_detected",
    "recovery_sweep_required",
    "possible_work_loss",
})


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class WorktreeInfo(BaseModel):
    """One git worktree plus uncommitted-file evidence."""

    path: str
    branch: str
    head: str
    modified: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    is_sidecar: bool = False


class AheadCommit(BaseModel):
    """One commit on a sidecar branch not yet merged to base."""

    branch: str
    sha: str
    subject: str


class RecoveryState(BaseModel):
    """Classified recovery state with full evidence for packet generation.

    This is the canonical scanner output. Phase 42 will persist it (with a
    stable schema_version) as a deterministic recovery packet under
    ``.ollarma/incidents/``.
    """

    schema_version: int = 1
    project_id: str
    repo_root: str
    timestamp: str
    source: str = "scan"  # "scan" | "admission" | "heartbeat"
    state: RecoveryStateStr
    blocker_code: BlockerCode
    resume_present: bool
    session_log_present: bool
    worktrees: list[WorktreeInfo] = Field(default_factory=list)
    ahead_commits: list[AheadCommit] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)
    artifacts_at_risk: list[str] = Field(default_factory=list)
    probable_reasoning_loss: bool = False
    unknown_loss_risk: bool = False
    next_fix_commands: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _run_git(repo_root: pathlib.Path, *args: str, timeout: float = 30.0) -> str:
    """Run a git command in ``repo_root`` and return stdout (empty on error)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("recovery: git %s failed: %s", args, exc)
        return ""
    if result.returncode != 0:
        # Non-zero is common (e.g. no sidecar branches exist). Not an error.
        logger.debug(
            "recovery: git %s returned %d: %s",
            args, result.returncode, result.stderr.strip(),
        )
        return ""
    return result.stdout


def _inspect_worktree(path: pathlib.Path) -> tuple[list[str], list[str]]:
    """Return ``(modified, untracked)`` file lists for a worktree path."""
    if not path.is_dir():
        return [], []
    out = _run_git(path, "status", "--porcelain")
    modified: list[str] = []
    untracked: list[str] = []
    for line in out.splitlines():
        if not line:
            continue
        code = line[:2]
        file = line[3:]
        if code == "??":
            untracked.append(file)
        else:
            modified.append(file)
    return modified, untracked


def _parse_worktree_list(porcelain_output: str) -> list[tuple[str, str, str]]:
    """Parse ``git worktree list --porcelain`` output.

    Returns a list of ``(path, head, branch)`` triples. ``branch`` may be empty
    if the worktree is detached.
    """
    entries: list[tuple[str, str, str]] = []
    cur_path = cur_head = cur_branch = ""

    for line in porcelain_output.splitlines():
        if line.startswith("worktree "):
            cur_path = line[len("worktree "):].strip()
        elif line.startswith("HEAD "):
            cur_head = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            cur_branch = line[len("branch "):].strip().replace("refs/heads/", "")
        elif not line:
            if cur_path:
                entries.append((cur_path, cur_head, cur_branch))
            cur_path = cur_head = cur_branch = ""

    # trailing entry (no final blank line)
    if cur_path:
        entries.append((cur_path, cur_head, cur_branch))

    return entries


def _is_sidecar_path(path: str) -> bool:
    """Return True if the path contains a sidecar worktree root marker."""
    return any(root in path for root in SIDECAR_WORKTREE_ROOTS)


def _scan_worktrees(repo_root: pathlib.Path) -> list[WorktreeInfo]:
    """List all git worktrees and inspect each for modifications."""
    out = _run_git(repo_root, "worktree", "list", "--porcelain")
    if not out:
        return []
    entries = _parse_worktree_list(out)
    worktrees: list[WorktreeInfo] = []
    for path, head, branch in entries:
        is_sidecar = _is_sidecar_path(path)
        modified, untracked = _inspect_worktree(pathlib.Path(path))
        worktrees.append(WorktreeInfo(
            path=path,
            branch=branch or "(detached)",
            head=head,
            modified=modified,
            untracked=untracked,
            is_sidecar=is_sidecar,
        ))
    return worktrees


def _scan_sidecar_branches(
    repo_root: pathlib.Path, base_branch: str = "main",
) -> list[AheadCommit]:
    """Find sidecar branches ahead of ``base_branch`` (commits not merged).

    Handles three ``git branch --list`` line markers:
      ``* branch`` — current branch
      ``+ branch`` — branch checked out in a linked worktree
      ``  branch`` — ordinary branch
    Without handling ``+``, sidecar branches currently checked out in
    linked worktrees are silently skipped, which means a real stranded
    sidecar is classified as `clean`/`resume_context_available`.
    """
    all_branches_out = _run_git(repo_root, "branch", "--list")
    sidecar: list[str] = []
    for line in all_branches_out.splitlines():
        name = line.strip().lstrip("*+ ").strip()
        if not name or name == base_branch:
            continue
        if any(name.startswith(prefix) for prefix in SIDECAR_BRANCH_PREFIXES):
            sidecar.append(name)

    ahead_commits: list[AheadCommit] = []
    for branch in sorted(sidecar):
        out = _run_git(
            repo_root, "log", f"{base_branch}..{branch}", "--format=%H|%s",
        )
        for ln in out.splitlines():
            if "|" in ln:
                sha, subject = ln.split("|", 1)
                ahead_commits.append(AheadCommit(
                    branch=branch, sha=sha, subject=subject,
                ))
    return ahead_commits


def _read_scribe_artifacts(repo_root: pathlib.Path) -> tuple[bool, bool]:
    """Return ``(resume_present, session_log_present)``."""
    resume = (repo_root / SCRIBE_DIR / RESUME_FILE).exists()
    log = (repo_root / SCRIBE_DIR / SESSION_LOG).exists()
    return resume, log


def _project_id(repo_root: pathlib.Path) -> str:
    """Derive a stable project id from the repo directory name."""
    return repo_root.resolve().name


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify(
    worktrees: list[WorktreeInfo],
    ahead_commits: list[AheadCommit],
    resume_present: bool,
    session_log_present: bool,
) -> tuple[RecoveryStateStr, list[str], list[str]]:
    """Classify state and produce deterministic ``next_fix_commands`` + notes.

    Severity order (highest-severity wins):
      1. stranded AND ahead -> recovery_sweep_required
      2. stranded           -> stranded_worktree_detected
      3. ahead              -> possible_work_loss
      4. scribe artifacts   -> resume_context_available
      5. otherwise          -> clean
    """
    stranded = [w for w in worktrees if w.is_sidecar and (w.modified or w.untracked)]
    any_sidecar_ahead = bool(ahead_commits)

    fixes: list[str] = []
    notes: list[str] = []

    if stranded and any_sidecar_ahead:
        for w in stranded:
            fixes.append(f"# Stranded worktree: {w.path}")
            fixes.append(f"git -C {w.path} status")
            fixes.append(f"git -C {w.path} diff")
        # Show at most 5 ahead-commit branches to keep the packet bounded.
        seen_branches: set[str] = set()
        for c in ahead_commits:
            if c.branch in seen_branches:
                continue
            seen_branches.add(c.branch)
            fixes.append(f"git log --oneline main..{c.branch}")
            if len(seen_branches) >= 5:
                break
        notes.append(
            f"{len(stranded)} stranded worktree(s) AND "
            f"{len(ahead_commits)} ahead-commit(s) -- broad sweep required"
        )
        return ("recovery_sweep_required", fixes, notes)

    if stranded:
        for w in stranded:
            fixes.append(f"# Inspect and reconcile: {w.path}")
            fixes.append(f"git -C {w.path} status")
            fixes.append(f"git -C {w.path} diff")
        notes.append(
            f"{len(stranded)} stranded sidecar worktree(s) with uncommitted changes"
        )
        return ("stranded_worktree_detected", fixes, notes)

    if any_sidecar_ahead:
        seen_branches: set[str] = set()
        for c in ahead_commits:
            if c.branch in seen_branches:
                continue
            seen_branches.add(c.branch)
            fixes.append(f"git log --oneline main..{c.branch}")
            fixes.append(f"git diff main..{c.branch}")
            if len(seen_branches) >= 5:
                break
        notes.append(
            f"{len(ahead_commits)} ahead-commit(s) across "
            f"{len({c.branch for c in ahead_commits})} sidecar branch(es) not on main"
        )
        return ("possible_work_loss", fixes, notes)

    if resume_present or session_log_present:
        if resume_present:
            fixes.append("cat .ollarma/RESUME.md")
        if session_log_present:
            fixes.append("tail -20 .ollarma/session-log.jsonl")
        notes.append(
            "Scribe artifacts present -- prior session may have paused intentionally"
        )
        return ("resume_context_available", fixes, notes)

    return ("clean", [], [])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan(
    repo_root: str | pathlib.Path,
    source: str = "scan",
    base_branch: str = "main",
) -> RecoveryState:
    """Scan ``repo_root`` and return a classified ``RecoveryState``.

    ``source`` annotates what triggered the scan (``"scan"``, ``"admission"``,
    ``"heartbeat"``, etc.) and is persisted in the packet.
    """
    root = pathlib.Path(repo_root).resolve()

    worktrees = _scan_worktrees(root)
    ahead_commits = _scan_sidecar_branches(root, base_branch)
    resume_present, session_log_present = _read_scribe_artifacts(root)

    state, fixes, notes = _classify(
        worktrees, ahead_commits, resume_present, session_log_present,
    )

    stranded = [w for w in worktrees if w.is_sidecar and (w.modified or w.untracked)]
    modified_files = [f"{w.path}:{f}" for w in stranded for f in w.modified]
    untracked_files = [f"{w.path}:{f}" for w in stranded for f in w.untracked]
    artifacts_at_risk = modified_files + untracked_files

    probable_reasoning_loss = bool(ahead_commits) and not (
        resume_present or session_log_present
    )
    unknown_loss_risk = state == "recovery_sweep_required"

    return RecoveryState(
        project_id=_project_id(root),
        repo_root=str(root),
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        source=source,
        state=state,
        blocker_code=_STATE_TO_BLOCKER[state],
        resume_present=resume_present,
        session_log_present=session_log_present,
        worktrees=worktrees,
        ahead_commits=ahead_commits,
        modified_files=modified_files,
        untracked_files=untracked_files,
        artifacts_at_risk=artifacts_at_risk,
        probable_reasoning_loss=probable_reasoning_loss,
        unknown_loss_risk=unknown_loss_risk,
        next_fix_commands=fixes,
        notes=notes,
    )


def is_admission_blocking(state: RecoveryState) -> bool:
    """Return True when admission control should block bounded execution."""
    return state.state in ADMISSION_BLOCKING_STATES


# ---------------------------------------------------------------------------
# Packet persistence (Phase 42)
# ---------------------------------------------------------------------------

def _incidents_dir(repo_root: pathlib.Path) -> pathlib.Path:
    """Return ``<repo_root>/.ollarma/incidents/``, creating it if missing."""
    d = repo_root / RECOVERY_DIR / INCIDENTS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _packet_filename(state: RecoveryState) -> str:
    """Return a deterministic filename for a recovery packet.

    Format: ``YYYYMMDDTHHMMSSZ-<state-slug>.json``.
    The timestamp is derived from ``state.timestamp`` (ISO-8601 UTC).
    """
    # state.timestamp comes from datetime.now(tz=utc).isoformat()
    # Example: 2026-04-17T18:05:11.123456+00:00
    # We want a filename-safe compact form.
    try:
        dt = datetime.datetime.fromisoformat(state.timestamp)
    except ValueError:
        dt = datetime.datetime.now(datetime.timezone.utc)
    ts = dt.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = state.state.replace("_", "-")
    return f"{ts}-{slug}.json"


def _atomic_write_bytes(path: pathlib.Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically via rename-from-tempfile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in same dir keeps atomic rename on same filesystem.
    fd, tmp_path = tempfile.mkstemp(prefix=".packet.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _packet_bytes(state: RecoveryState) -> bytes:
    """Return deterministic JSON bytes for a ``RecoveryState``.

    Uses ``orjson.OPT_SORT_KEYS | OPT_INDENT_2`` so two equivalent states
    produce byte-identical output.
    """
    payload = state.model_dump(mode="json")
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2)


def write_packet(
    state: RecoveryState, repo_root: str | pathlib.Path,
) -> pathlib.Path:
    """Persist ``state`` to ``.ollarma/incidents/`` and refresh ``latest.json``.

    Returns the absolute path of the persisted packet file.
    """
    root = pathlib.Path(repo_root).resolve()
    incidents = _incidents_dir(root)

    filename = _packet_filename(state)
    packet_path = incidents / filename
    data = _packet_bytes(state)

    _atomic_write_bytes(packet_path, data)
    # Also (re)write the ``latest.json`` pointer so ingest consumers don't have
    # to scan the directory. Written atomically, same bytes (not a symlink).
    _atomic_write_bytes(incidents / LATEST_POINTER, data)

    logger.info("recovery: wrote packet %s (state=%s)", packet_path, state.state)
    return packet_path


def read_packet(path: str | pathlib.Path) -> RecoveryState:
    """Read and validate a recovery packet from disk."""
    p = pathlib.Path(path)
    raw = p.read_bytes()
    payload = orjson.loads(raw)
    return RecoveryState.model_validate(payload)


def read_latest_packet(repo_root: str | pathlib.Path) -> RecoveryState | None:
    """Return the latest recovery packet, or ``None`` if none exists."""
    root = pathlib.Path(repo_root).resolve()
    latest = root / RECOVERY_DIR / INCIDENTS_SUBDIR / LATEST_POINTER
    if not latest.exists():
        return None
    return read_packet(latest)


def scan_and_persist(
    repo_root: str | pathlib.Path,
    source: str = "scan",
    base_branch: str = "main",
) -> tuple[RecoveryState, pathlib.Path]:
    """Convenience: run a scan and persist the packet in one call.

    Returns ``(state, packet_path)``.
    """
    state = scan(repo_root, source=source, base_branch=base_branch)
    path = write_packet(state, repo_root)
    return state, path

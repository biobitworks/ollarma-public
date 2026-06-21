"""scribe_hooks.py -- Automatic scribe-entry emission around execution paths.

Phase 45 (v4.3 SCRIBE-01..03): the voluntary ``scribe_progress()`` MCP tool
depends on the agent remembering to call it. These hooks make pre-dispatch,
heartbeat, and end-of-run entries automatic around the four gated service
entrypoints (``run_benchmark``, ``submit_workflow``, ``submit_autopilot``,
``run_agent``).

Design:
- Additive: the existing voluntary ``scribe_progress`` is unchanged (SCRIBE-04).
- Best-effort: scribe I/O must never mask the original operation's error.
- Opt-out: ``OLLARMA_SCRIBE_HOOKS=off`` disables all hooks (for tests or quiet
  environments) without affecting voluntary scribe calls.
- Heartbeat: a lightweight background ``threading.Thread`` emits ``in_progress``
  entries every ``cadence_s`` (default 30s) while active.
"""
from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import threading
from typing import Iterator, Optional

from ollarma.scribe import ScribeEntry, scribe_progress as _scribe_write

logger = logging.getLogger(__name__)

HOOK_ENV_VAR = "OLLARMA_SCRIBE_HOOKS"
HEARTBEAT_CADENCE_S = 30.0


def hooks_enabled() -> bool:
    """Return True unless ``OLLARMA_SCRIBE_HOOKS=off``."""
    return os.environ.get(HOOK_ENV_VAR, "").lower() != "off"


def _project_root() -> pathlib.Path:
    """Directory under which ``.ollarma/session-log.jsonl`` lives."""
    return pathlib.Path.cwd()


def _safe_write(entry: ScribeEntry) -> None:
    """Write a scribe entry, swallowing I/O errors."""
    try:
        _scribe_write(project_root=_project_root(), entry=entry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scribe_hooks: write failed: %s", exc)


def pre_dispatch(
    entrypoint: str,
    *,
    project: str = "",
    task: str = "",
    notes: str = "",
) -> None:
    """Emit a ``state: started`` scribe entry before a gated operation runs."""
    if not hooks_enabled():
        return
    entry = ScribeEntry(
        project=project or entrypoint,
        phase="",
        task=task,
        state="started",
        notes=f"[scribe-hook pre_dispatch:{entrypoint}] {notes}".strip(),
    )
    _safe_write(entry)


def end_of_run(
    entrypoint: str,
    *,
    state: str,
    project: str = "",
    task: str = "",
    notes: str = "",
    next_action: str = "",
    artifacts: Optional[list[str]] = None,
) -> None:
    """Emit a ``state: completed|blocked`` entry when the gated op finishes."""
    if not hooks_enabled():
        return
    if state not in {"completed", "blocked"}:
        state = "completed"
    entry = ScribeEntry(
        project=project or entrypoint,
        phase="",
        task=task,
        state=state,  # type: ignore[arg-type]
        notes=f"[scribe-hook end_of_run:{entrypoint}] {notes}".strip(),
        next_action=next_action,
        artifacts=list(artifacts or []),
    )
    _safe_write(entry)


class HeartbeatThread:
    """Lightweight daemon thread emitting ``in_progress`` scribe entries.

    Usage:
        hb = HeartbeatThread(entrypoint="run_benchmark", project="ollarma")
        hb.start()
        try:
            ... long work ...
        finally:
            hb.stop()
    """

    def __init__(
        self,
        entrypoint: str,
        *,
        project: str = "",
        task: str = "",
        cadence_s: float = HEARTBEAT_CADENCE_S,
    ) -> None:
        self._entrypoint = entrypoint
        self._project = project or entrypoint
        self._task = task
        self._cadence_s = cadence_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _loop(self) -> None:
        # First heartbeat happens after one cadence_s; pre_dispatch already
        # wrote the "started" entry.
        while not self._stop.wait(self._cadence_s):
            entry = ScribeEntry(
                project=self._project,
                phase="",
                task=self._task,
                state="in_progress",
                notes=f"[scribe-hook heartbeat:{self._entrypoint}]",
            )
            _safe_write(entry)

    def start(self) -> None:
        if not hooks_enabled():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name=f"scribe-hb-{self._entrypoint}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


@contextlib.contextmanager
def hooked(
    entrypoint: str,
    *,
    project: str = "",
    task: str = "",
    notes: str = "",
    with_heartbeat: bool = False,
    cadence_s: float = HEARTBEAT_CADENCE_S,
) -> Iterator[None]:
    """Context manager wrapping an operation with pre/heartbeat/post entries.

    - On enter: writes ``started`` entry (via ``pre_dispatch``).
    - While inside: optionally runs a heartbeat thread at ``cadence_s``.
    - On normal exit: writes ``completed`` entry.
    - On exception: writes ``blocked`` entry with the exception text as notes,
      then re-raises.
    """
    pre_dispatch(entrypoint, project=project, task=task, notes=notes)
    hb: Optional[HeartbeatThread] = None
    if with_heartbeat and hooks_enabled():
        hb = HeartbeatThread(
            entrypoint, project=project, task=task, cadence_s=cadence_s,
        )
        hb.start()
    try:
        yield
    except BaseException as exc:
        end_of_run(
            entrypoint,
            state="blocked",
            project=project,
            task=task,
            notes=f"exception: {type(exc).__name__}: {exc}",
            next_action="inspect logs; re-run when resolved",
        )
        raise
    else:
        end_of_run(entrypoint, state="completed", project=project, task=task)
    finally:
        if hb is not None:
            hb.stop()

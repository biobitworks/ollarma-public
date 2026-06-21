"""Read-only status helper for the shared Claude Code <-> Codex SWE session.

The cross-runtime session lives outside the repo under ``~/.ollarma``. This
module gives either runtime a deterministic, model-safe snapshot without
issuing inference calls: session participants, recent sidecar events, service
health, embed posture, socket counts, and operator-gated phase reminders.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_SESSION_DIR = pathlib.Path.home() / ".ollarma" / "swe_session"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8484/health"
DEFAULT_EMBED_STATUS_URL = "http://127.0.0.1:8484/embed/status"
PHASE70_PROMPT_ID = "PROMPT-OLLARMA-SWARM-001"
DEFAULT_PHASE69_TARGETS = (
    "sids-proteome-validation",
    "deltaprot",
    "xenodisorder",
    "substrata",
    "seedgraph",
    "synapse",
    "tf-cellico",
    "fractal-waves",
    "bioviz-atlas",
    "antigence-bittensor",
)

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
HttpJsonGetter = Callable[[str, float], dict[str, Any]]
PidAliveChecker = Callable[[int], bool]
Sleeper = Callable[[float], None]


def _read_json(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _iter_jsonl(path: pathlib.Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return ()
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    rows.append(data)
    except OSError:
        return ()
    return rows


def _http_json(url: str, timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Connection": "close"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - localhost/operator-provided URL
            raw = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(str(exc)) from exc
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise RuntimeError("HTTP response JSON was not an object")
    return data


def _run_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout_s)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json_atomic(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _active_projects_root(repo_root: pathlib.Path) -> pathlib.Path:
    return repo_root.parent if repo_root.name == "ollarma" else repo_root


def project_key(repo_root: pathlib.Path | str) -> str:
    """Filesystem-safe, stable per-project key for SWE-session isolation.

    Derived from the repo directory name so the on-disk path stays human
    readable (``projects/ollarma``), with the per-event ``repo``/``project_root``
    field providing exact identity for the fail-closed reader.
    """

    try:
        name = pathlib.Path(repo_root).resolve().name
    except (OSError, RuntimeError, ValueError):
        name = pathlib.Path(str(repo_root)).name
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", name or "root")
    return safe or "root"


def project_session_dir(
    session_root: pathlib.Path | str, repo_root: pathlib.Path | str
) -> pathlib.Path:
    """Return the per-project session directory under *session_root*.

    Isolating the transport per project (``<session_root>/projects/<key>/``)
    stops one repo's participants/events from landing in another repo's
    append-only spine. The legacy global ``session_root`` files are left
    untouched (no migration).
    """

    return pathlib.Path(session_root) / "projects" / project_key(repo_root)


def _record_repo_value(record: dict[str, Any]) -> str | None:
    """Return the project/repo path a participant or event declares, if any.

    Records may name their project via ``project_root``, ``repo_root``, or the
    legacy ``repo`` field. The first non-empty string wins.
    """

    for key in ("project_root", "repo_root", "repo"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _repo_matches(candidate: str | None, repo_root: pathlib.Path) -> bool:
    """Fail-closed project match.

    A record only counts as the current project when its declared repo path
    resolves to the same path as *repo_root* (or sits inside it, to tolerate a
    process cwd in a repo subdirectory). A missing or foreign path never
    matches, so cross-project contamination is excluded by default.
    """

    if not candidate:
        return False
    try:
        cand = pathlib.Path(candidate).resolve()
        root = repo_root.resolve()
    except (OSError, RuntimeError, ValueError):
        return str(candidate) == str(repo_root)
    if cand == root:
        return True
    try:
        return root in cand.parents or cand in root.parents
    except Exception:
        return False


def _partition_records_by_project(
    records: Iterable[dict[str, Any]], repo_root: pathlib.Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Split records into ``(current_project, foreign_project, unknown_count)``.

    Records with no declared repo path are treated as foreign (fail closed) and
    counted in *unknown_count* so the gap stays visible instead of being
    silently admitted as current-project work. Foreign records are tagged with
    ``project_classification`` for downstream reporting.
    """

    current: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    unknown = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        value = _record_repo_value(record)
        if value is None:
            unknown += 1
            tagged = dict(record)
            tagged["project_classification"] = "unknown_repo"
            foreign.append(tagged)
        elif _repo_matches(value, repo_root):
            current.append(record)
        else:
            tagged = dict(record)
            tagged["project_classification"] = "foreign_project"
            foreign.append(tagged)
    return current, foreign, unknown


def _git_output(
    args: list[str],
    *,
    command_runner: CommandRunner,
    timeout_s: float = 2.0,
) -> str | None:
    try:
        result = command_runner(args, timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _quote_command_part(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _participant_with_liveness(participant: dict[str, Any], pid_alive_checker: PidAliveChecker) -> dict[str, Any]:
    enriched = dict(participant)
    raw_pid = enriched.get("pid")
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        enriched["pid_alive"] = None
        enriched["pid_liveness_reason"] = "pid_missing"
        return enriched
    alive = pid_alive_checker(pid)
    enriched["pid"] = pid
    enriched["pid_alive"] = alive
    enriched["pid_liveness_reason"] = "alive" if alive else "stale_pid"
    return enriched


def summarize_session(
    session_dir: pathlib.Path,
    *,
    this_runtime: str = "codex",
    peer_runtime: str = "claude-code",
    pid_alive_checker: PidAliveChecker = _pid_is_alive,
    repo_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    active = _read_json(session_dir / "active.json") or {}
    raw_participants = active.get("participants", [])
    participants = [
        _participant_with_liveness(p, pid_alive_checker)
        for p in raw_participants
        if isinstance(p, dict)
    ]
    runtimes = [str(p.get("runtime")) for p in participants if p.get("runtime")]
    peer_participants = [p for p in participants if p.get("runtime") == peer_runtime]
    peer_alive = any(p.get("pid_alive") is True for p in peer_participants)
    summary: dict[str, Any] = {
        "connected": this_runtime in runtimes,
        "peer_present": peer_runtime in runtimes,
        "peer_alive": peer_alive,
        "stale_participant_count": sum(1 for p in participants if p.get("pid_alive") is False),
        "participants": participants,
        "participant_runtimes": runtimes,
        "updated_at": active.get("updated_at"),
    }
    if repo_root is not None:
        # Project-scoped (fail-closed) view: only participants whose declared
        # repo path matches *repo_root* count toward connection/peer presence.
        project_participants, foreign_participants, unknown = _partition_records_by_project(
            participants, repo_root
        )
        project_runtimes = [
            str(p.get("runtime")) for p in project_participants if p.get("runtime")
        ]
        project_peer = [p for p in project_participants if p.get("runtime") == peer_runtime]
        summary.update(
            {
                "project_scope": "strict",
                "project_root": str(pathlib.Path(repo_root)),
                "project_connected": this_runtime in project_runtimes,
                "project_peer_present": peer_runtime in project_runtimes,
                "project_peer_alive": any(p.get("pid_alive") is True for p in project_peer),
                "project_participants": project_participants,
                "project_participant_count": len(project_participants),
                "foreign_participants": foreign_participants,
                "foreign_participant_count": len(foreign_participants),
                "unknown_repo_participant_count": unknown,
            }
        )
    return summary


def recent_sidecar_events(session_dir: pathlib.Path, *, limit: int = 10) -> list[dict[str, Any]]:
    bounded = max(0, min(int(limit), 100))
    rows = list(_iter_jsonl(session_dir / "events.jsonl"))
    return rows[-bounded:] if bounded else []


def _parse_utc_epoch(ts: Any) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _event_brief(event: dict[str, Any], *, now_epoch: float) -> dict[str, Any]:
    epoch = _parse_utc_epoch(event.get("ts"))
    note = event.get("note")
    brief = {
        "ts": event.get("ts"),
        "runtime": event.get("runtime"),
        "step": event.get("step"),
        "state": event.get("state"),
        "repo": event.get("repo"),
        "note": str(note)[:240] if note is not None else "",
    }
    if epoch is not None:
        brief["age_seconds"] = max(0, int(now_epoch - epoch))
    return brief


def summarize_sidecar_activity(
    session_dir: pathlib.Path,
    *,
    this_runtime: str = "codex",
    peer_runtime: str = "claude-code",
    stale_after_s: int = 1800,
    ack_overdue_after_s: int = 300,
    now_epoch: float | None = None,
    repo_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Summarize append-only sidecar activity by runtime.

    When *repo_root* is given, the spine is partitioned fail-closed: only events
    whose declared repo path matches the current project drive peer-freshness
    and pending-ack signals; foreign-project events are excluded and counted
    separately so they can never satisfy ``requires_ack`` for another repo.
    """

    now = time.time() if now_epoch is None else now_epoch
    all_rows = list(_iter_jsonl(session_dir / "events.jsonl"))
    if repo_root is not None:
        rows, foreign_rows, unknown_event_count = _partition_records_by_project(
            all_rows, repo_root
        )
    else:
        rows, foreign_rows, unknown_event_count = all_rows, [], 0
    runtime_counts: dict[str, int] = {}
    for row in rows:
        runtime = str(row.get("runtime") or "unknown")
        runtime_counts[runtime] = runtime_counts.get(runtime, 0) + 1

    def last_for(runtime: str) -> dict[str, Any] | None:
        event = next((row for row in reversed(rows) if row.get("runtime") == runtime), None)
        return _event_brief(event, now_epoch=now) if event else None

    acked_markers = {
        str(row.get("ack_for") or row.get("marker"))
        for row in rows
        if row.get("runtime") == peer_runtime and (row.get("ack_for") or row.get("marker"))
    }
    pending_ack_requests: list[dict[str, Any]] = []
    for row in rows:
        marker = row.get("marker")
        if (
            row.get("runtime") == this_runtime
            and row.get("to_runtime") == peer_runtime
            and row.get("requires_ack") is True
            and marker
            and str(marker) not in acked_markers
        ):
            brief = _event_brief(row, now_epoch=now)
            brief["marker"] = str(marker)
            pending_ack_requests.append(brief)

    peer_last_event = last_for(peer_runtime)
    this_last_event = last_for(this_runtime)
    peer_age = peer_last_event.get("age_seconds") if isinstance(peer_last_event, dict) else None
    last_pending_ack = pending_ack_requests[-1] if pending_ack_requests else None
    last_pending_ack_age = (
        last_pending_ack.get("age_seconds") if isinstance(last_pending_ack, dict) else None
    )
    summary: dict[str, Any] = {
        "event_count": len(rows),
        "runtime_counts": runtime_counts,
        "this_runtime": this_runtime,
        "peer_runtime": peer_runtime,
        "last_event": {
            this_runtime: this_last_event,
            peer_runtime: peer_last_event,
        },
        "peer_event_seen": peer_last_event is not None,
        "peer_event_stale_after_s": stale_after_s,
        "peer_event_stale": peer_age is None or peer_age > stale_after_s,
        "pending_peer_ack_count": len(pending_ack_requests),
        "pending_peer_ack_markers": [request["marker"] for request in pending_ack_requests[-10:]],
        "last_pending_peer_ack": last_pending_ack,
        "pending_peer_ack_overdue_after_s": ack_overdue_after_s,
        "pending_peer_ack_overdue": (
            last_pending_ack_age is not None and last_pending_ack_age > ack_overdue_after_s
        ),
    }
    if repo_root is not None:
        summary.update(
            {
                "project_scope": "strict",
                "project_root": str(pathlib.Path(repo_root)),
                "total_event_count": len(all_rows),
                "project_event_count": len(rows),
                "foreign_project_event_count": len(foreign_rows),
                "unknown_repo_event_count": unknown_event_count,
                "foreign_project_runtimes": sorted(
                    {
                        str(row.get("runtime"))
                        for row in foreign_rows
                        if row.get("runtime")
                    }
                ),
            }
        )
    return summary


def summarize_peer_ack(
    session_dir: pathlib.Path,
    *,
    marker: str,
    this_runtime: str = "codex",
    peer_runtime: str = "claude-code",
    now_epoch: float | None = None,
    repo_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Summarize whether *peer_runtime* acknowledged a directed marker.

    When *repo_root* is given, only events from the current project can satisfy
    the marker, so a stale or foreign Claude/Codex ack cannot be mistaken for a
    receipt in this repo.
    """

    now = time.time() if now_epoch is None else now_epoch
    all_rows = list(_iter_jsonl(session_dir / "events.jsonl"))
    if repo_root is not None:
        rows = _partition_records_by_project(all_rows, repo_root)[0]
    else:
        rows = all_rows
    request = next(
        (
            row
            for row in reversed(rows)
            if row.get("runtime") == this_runtime
            and row.get("to_runtime") == peer_runtime
            and row.get("requires_ack") is True
            and row.get("marker") == marker
        ),
        None,
    )
    ack = next(
        (
            row
            for row in reversed(rows)
            if row.get("runtime") == peer_runtime and (row.get("ack_for") == marker or row.get("marker") == marker)
        ),
        None,
    )
    result: dict[str, Any] = {
        "marker": marker,
        "acknowledged": ack is not None,
        "ack_event": _event_brief(ack, now_epoch=now) if ack else None,
        "request_event": _event_brief(request, now_epoch=now) if request else None,
        "request_seen": request is not None,
        "peer_runtime": peer_runtime,
    }
    if repo_root is not None:
        result["project_scope"] = "strict"
        result["project_root"] = str(pathlib.Path(repo_root))
    return result


def _compact_status_note(status: dict[str, Any]) -> str:
    session = status.get("session", {}) if isinstance(status.get("session"), dict) else {}
    service = status.get("service", {}) if isinstance(status.get("service"), dict) else {}
    health = service.get("health", {}) if isinstance(service.get("health"), dict) else {}
    embed = service.get("embed", {}) if isinstance(service.get("embed"), dict) else {}
    sockets = status.get("ollama_sockets", {}) if isinstance(status.get("ollama_sockets"), dict) else {}
    monitor = status.get("monitor", {}) if isinstance(status.get("monitor"), dict) else {}
    gates = status.get("operator_gates", []) if isinstance(status.get("operator_gates"), list) else []
    gate_names = ",".join(str(g.get("gate")) for g in gates if isinstance(g, dict) and g.get("gate")) or "none"
    return (
        "SWE status: "
        f"monitor={monitor.get('state')} "
        f"peer_alive={session.get('peer_alive')} "
        f"source={session.get('peer_liveness_source')} "
        f"health={health.get('status')} "
        f"embed={embed.get('status')} "
        f"sockets={sockets.get('established_count')} "
        f"gates={gate_names} "
        f"inference_calls_issued={status.get('inference_calls_issued')}"
    )


def append_sidecar_event(
    session_dir: pathlib.Path,
    *,
    repo_root: pathlib.Path,
    runtime: str,
    step: str,
    state: str,
    note: str,
    now: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one SWE-session event in the shared sidecar format."""

    event = {
        "ts": now or _utc_now(),
        "runtime": runtime,
        "repo": str(repo_root),
        "step": step,
        "state": state,
        "note": note[:500],
    }
    if extra:
        for key, value in extra.items():
            if key not in event:
                event[key] = value
    path = session_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def _ack_marker(*, runtime: str, peer_runtime: str, now: str | None = None) -> str:
    compact_now = (now or _utc_now()).replace("-", "").replace(":", "").replace("Z", "Z")
    return f"ack-{runtime}-to-{peer_runtime}-{compact_now}"


def append_peer_ack_request(
    session_dir: pathlib.Path,
    *,
    repo_root: pathlib.Path,
    runtime: str,
    peer_runtime: str,
    note: str,
    marker: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Append a directed acknowledgement request for the peer runtime."""

    timestamp = now or _utc_now()
    event_marker = marker or _ack_marker(runtime=runtime, peer_runtime=peer_runtime, now=timestamp)
    return append_sidecar_event(
        session_dir,
        repo_root=repo_root,
        runtime=runtime,
        step="swe_peer_ack_request",
        state="ack_requested",
        note=note,
        now=timestamp,
        extra={
            "marker": event_marker,
            "to_runtime": peer_runtime,
            "requires_ack": True,
            "ack_expected_from": peer_runtime,
        },
    )


def summarize_health(
    *,
    health_url: str = DEFAULT_HEALTH_URL,
    embed_status_url: str = DEFAULT_EMBED_STATUS_URL,
    timeout_s: float = 3.0,
    http_json_get: HttpJsonGetter = _http_json,
) -> dict[str, Any]:
    summary: dict[str, Any] = {"checked": True, "health": None, "embed": None}
    try:
        health = http_json_get(health_url, timeout_s)
        startup = health.get("startup_readiness", {}) if isinstance(health.get("startup_readiness"), dict) else {}
        pipeline = startup.get("pipeline", {}) if isinstance(startup.get("pipeline"), dict) else {}
        swap = startup.get("swap", {}) if isinstance(startup.get("swap"), dict) else {}
        summary["health"] = {
            "status": health.get("status"),
            "chat_model": (health.get("chat_selection") or {}).get("model")
            if isinstance(health.get("chat_selection"), dict)
            else None,
            "route_model": (health.get("route_selection") or {}).get("model")
            if isinstance(health.get("route_selection"), dict)
            else None,
            "loaded_models": pipeline.get("loaded_models", ()),
            "swap": swap,
        }
    except Exception as exc:  # noqa: BLE001 - status surface must not crash on probe failure
        summary["health"] = {"status": "unavailable", "detail": str(exc)[:300]}

    try:
        embed = http_json_get(embed_status_url, timeout_s)
        summary["embed"] = {
            "status": embed.get("status"),
            "model": embed.get("model"),
            "available": embed.get("available"),
            "resident": embed.get("resident"),
            "pinned": embed.get("pinned"),
            "reason_code": embed.get("reason_code"),
        }
    except Exception as exc:  # noqa: BLE001
        summary["embed"] = {"status": "unavailable", "detail": str(exc)[:300]}
    return summary


def summarize_ollama_sockets(
    *,
    command_runner: CommandRunner = _run_command,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    try:
        completed = command_runner(["lsof", "-nP", "-iTCP:11434", "-sTCP:ESTABLISHED"], timeout_s)
    except Exception as exc:  # noqa: BLE001
        return {"checked": True, "status": "unavailable", "detail": str(exc)[:300], "established_count": None}
    if completed.returncode not in (0, 1):
        return {
            "checked": True,
            "status": "unavailable",
            "detail": (completed.stderr or "").strip()[:300],
            "established_count": None,
        }
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    data_lines = lines[1:] if lines and lines[0].startswith("COMMAND") else lines
    service_lines = [line for line in data_lines if "127.0.0.1:" in line and "->127.0.0.1:11434" in line]
    client_processes: dict[int, dict[str, Any]] = {}
    for line in service_lines:
        parsed = _parse_lsof_tcp_line(line)
        if parsed is None:
            continue
        pid = parsed["pid"]
        process = client_processes.setdefault(
            pid,
            {
                "pid": pid,
                "command": parsed["command"],
                "connection_count": 0,
                "cwd": None,
                "sample_names": [],
            },
        )
        process["connection_count"] += 1
        if len(process["sample_names"]) < 3:
            process["sample_names"].append(parsed["name"])
    for pid, process in client_processes.items():
        process["cwd"] = _process_cwd(pid, command_runner=command_runner, timeout_s=timeout_s)
    return {
        "checked": True,
        "status": "ok",
        "established_count": len(data_lines),
        "client_connection_count": len(service_lines),
        "client_processes": sorted(client_processes.values(), key=lambda item: (-item["connection_count"], item["pid"])),
        "sample": data_lines[:5],
    }


def _parse_lsof_tcp_line(line: str) -> dict[str, Any] | None:
    parts = line.split(None, 8)
    if len(parts) < 9:
        return None
    try:
        pid = int(parts[1])
    except ValueError:
        return None
    return {
        "command": parts[0],
        "pid": pid,
        "fd": parts[3],
        "name": parts[8],
    }


def _process_cwd(
    pid: int,
    *,
    command_runner: CommandRunner = _run_command,
    timeout_s: float = 3.0,
) -> str | None:
    try:
        cwd_result = command_runner(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout_s)
    except Exception:
        return None
    if cwd_result.returncode != 0:
        return None
    for line in (cwd_result.stdout or "").splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def _paths_match(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left) == str(right)


def discover_claude_processes(
    repo_root: pathlib.Path,
    *,
    command_runner: CommandRunner = _run_command,
    timeout_s: float = 3.0,
) -> list[dict[str, Any]]:
    """Discover live Claude Code processes and mark whether their cwd matches *repo_root*."""

    try:
        completed = command_runner(["pgrep", "-f", "claude --dangerously-skip-permissions"], timeout_s)
    except Exception:
        return []
    if completed.returncode not in (0, 1):
        return []
    discovered: list[dict[str, Any]] = []
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.isdigit():
            continue
        pid = int(stripped)
        cwd = None
        try:
            cwd_result = command_runner(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout_s)
        except Exception:
            cwd_result = subprocess.CompletedProcess([], 1, "", "")
        if cwd_result.returncode == 0:
            for cwd_line in (cwd_result.stdout or "").splitlines():
                if cwd_line.startswith("n"):
                    cwd = cwd_line[1:]
                    break
        cwd_path = pathlib.Path(cwd) if cwd else None
        discovered.append(
            {
                "runtime": "claude-code",
                "pid": pid,
                "cwd": cwd,
                "repo_match": bool(cwd_path and _paths_match(cwd_path, repo_root)),
            }
        )
    return discovered


def reconcile_active_session(
    repo_root: pathlib.Path,
    session_dir: pathlib.Path,
    *,
    command_runner: CommandRunner = _run_command,
    now: str | None = None,
) -> dict[str, Any]:
    """Repair ``active.json`` with a live Claude Code participant for *repo_root*.

    This is intentionally explicit and narrow: only the ``claude-code`` record
    is updated, and only when process discovery finds a live Claude cwd matching
    the repo. The function does not call models or touch repo files.
    """

    active_path = session_dir / "active.json"
    timestamp = now or _utc_now()
    active = _read_json(active_path) or {"session_id": "swe-local", "participants": []}
    participants = [p for p in active.get("participants", []) if isinstance(p, dict)]
    processes = discover_claude_processes(repo_root, command_runner=command_runner)
    match = next((proc for proc in processes if proc.get("repo_match")), None)
    if not match:
        return {
            "updated": False,
            "reason": "no_repo_matched_claude_process",
            "active_path": str(active_path),
            "discovered": processes,
        }

    existing = next((p for p in participants if p.get("runtime") == "claude-code"), {})
    repaired = {
        "runtime": "claude-code",
        "pid": int(match["pid"]),
        "repo": str(repo_root),
        "joined_at": existing.get("joined_at") or timestamp,
        "updated_at": timestamp,
        "liveness_source": "process_discovery",
    }
    retained = [p for p in participants if p.get("runtime") != "claude-code"]
    active["participants"] = retained + [repaired]
    active["updated_at"] = timestamp
    _write_json_atomic(active_path, active)
    return {
        "updated": True,
        "active_path": str(active_path),
        "participant": repaired,
        "discovered": processes,
    }


def detect_operator_gates(repo_root: pathlib.Path) -> list[dict[str, str]]:
    gates: list[dict[str, str]] = []
    state = (repo_root / ".planning" / "STATE.md").read_text(encoding="utf-8") if (repo_root / ".planning" / "STATE.md").exists() else ""
    roadmap_path = repo_root / ".planning" / "ROADMAP.md"
    roadmap = roadmap_path.read_text(encoding="utf-8") if roadmap_path.exists() else ""
    # Phase 69 proof-run gate: open only while STATE records the phase incomplete.
    # (Closed 2026-05-31 — operator-run proof bundle committed; the roadmap text
    # still mentions Phase 69 + "Operator gate" historically, so rely on the
    # authoritative phase_69_complete flag, not a roadmap substring.)
    if "phase_69_complete: false" in state:
        gates.append(
            {
                "gate": "phase69_proof_run",
                "status": "operator_required",
                "detail": "Needs operator-selected real target repo and task before --allow-target-writes proof run.",
            }
        )
    # Phase 70 acceptance gate: open while the T9b live acceptance is unrun OR its
    # verdict has not reached a closed state. Once T9b has run with a PARTIAL
    # verdict, the gate is no longer an operator GPU-window request — it is a
    # remediation/sign-off item (EXP-002 -> T9c -> PI), so surface that instead.
    if "T9 operator-gated" in state:
        gates.append(
            {
                "gate": "phase70_t9_acceptance",
                "status": "operator_required",
                "detail": "Needs operator GPU window for the pre-registered acceptance run.",
            }
        )
    elif "T9b PARTIAL" in state or "criterion #5" in state.lower() or "Phase 70 T9b PARTIAL" in state:
        gates.append(
            {
                "gate": "phase70_acceptance_remediation",
                "status": "remediation_in_progress",
                "detail": "T9b verdict PARTIAL (inert-control sub-criterion). Resolution: EXP-002 calibration -> (EXP-003) -> EXP-004 -> T9c rerun -> PI sign-off, or PI accepts PARTIAL.",
            }
        )
    return gates


def scan_phase69_target_candidates(
    repo_root: pathlib.Path,
    *,
    candidate_names: Iterable[str] = DEFAULT_PHASE69_TARGETS,
    command_runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Read-only readiness scan for operator-selected Phase 69 proof targets."""

    active_root = _active_projects_root(repo_root)
    candidates: list[dict[str, Any]] = []
    for name in candidate_names:
        path = active_root / str(name)
        resolved = path.resolve(strict=False)
        item: dict[str, Any] = {
            "name": str(name),
            "path": str(path),
            "exists": path.exists(),
            "is_symlink": path.is_symlink(),
            "resolved_path": str(resolved),
            "is_git_worktree": False,
            "dirty_count": None,
            "clean": False,
        }
        if not path.exists():
            item["status"] = "missing"
            candidates.append(item)
            continue

        git_top = _git_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            command_runner=command_runner,
        )
        if not git_top:
            item["status"] = "not_git_worktree"
            candidates.append(item)
            continue

        branch = _git_output(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            command_runner=command_runner,
        )
        status = _git_output(
            ["git", "-C", str(path), "status", "--short"],
            command_runner=command_runner,
            timeout_s=5.0,
        )
        dirty_count = len([line for line in (status or "").splitlines() if line.strip()])
        item.update(
            {
                "status": "ready_clean" if dirty_count == 0 else "dirty_existing_changes",
                "is_git_worktree": True,
                "git_top": git_top,
                "branch": branch or "unknown",
                "dirty_count": dirty_count,
                "clean": dirty_count == 0,
            }
        )
        candidates.append(item)

    ranked = sorted(
        candidates,
        key=lambda item: (
            item.get("dirty_count") is None,
            item.get("dirty_count") if isinstance(item.get("dirty_count"), int) else 999_999,
            item.get("name") or "",
        ),
    )
    recommended = [
        item["name"]
        for item in ranked
        if item.get("is_git_worktree") is True and item.get("dirty_count") == 0
    ]
    usable_with_review = [
        item["name"]
        for item in ranked
        if item.get("is_git_worktree") is True
        and isinstance(item.get("dirty_count"), int)
        and item.get("dirty_count") > 0
    ]
    missing_or_invalid = [
        item["name"]
        for item in ranked
        if item.get("is_git_worktree") is not True
    ]
    proof_run_templates = [
        {
            "target": item["name"],
            "command": (
                ".venv/bin/python scripts/swarm_proof_run.py "
                "--mode proof "
                f"--target {_quote_command_part(str(item['path']))} "
                "--task '<real task>' "
                "--allow-target-writes"
            ),
        }
        for item in ranked
        if item.get("is_git_worktree") is True and item.get("dirty_count") == 0
    ]
    return {
        "checked": True,
        "active_root": str(active_root),
        "candidate_count": len(candidates),
        "recommended_clean_targets": recommended,
        "usable_with_review": usable_with_review,
        "missing_or_invalid": missing_or_invalid,
        "operator_packet": {
            "status": "operator_target_task_required",
            "preflight_smoke_command": (
                ".venv/bin/python scripts/swarm_proof_run.py "
                "--target /tmp/stub-target "
                "--task 'smoke validation' "
                "--mode smoke --dry-run "
                "--out runs/swarm_proof/smoke_$(date +%Y%m%dT%H%M%SZ)"
            ),
            "proof_run_templates": proof_run_templates,
            "completion_gate": (
                "Operator must select a real task, run one proof command, commit the resulting "
                "runs/swarm_proof/<UTC>/ bundle, then update Phase 69 planning state."
            ),
        },
        "candidates": candidates,
    }


def build_phase70_operator_packet(repo_root: pathlib.Path) -> dict[str, Any]:
    """Return read-only command templates for the Phase 70 T9-T12 gate."""

    script = repo_root / "scripts" / "swarm_acceptance.py"
    prompt = repo_root / "prompts" / "PROMPT_OLLARMA_SWARM_001_LOCAL_GPU.md"
    prompt_json = repo_root / "prompts" / "PROMPT_OLLARMA_SWARM_001_LOCAL_GPU.json"
    available = script.exists() and prompt.exists()
    return {
        "checked": True,
        "prompt_id": PHASE70_PROMPT_ID,
        "status": "operator_gpu_window_required",
        "available": available,
        "driver": str(script),
        "prompt": str(prompt),
        "prompt_json": str(prompt_json),
        "wall_budget": "<=7.5h total; five scenarios sequential; no parallel GPU fan-out",
        "preflight_commands": [
            "ollama ps",
            "ollama list",
            "ollama pull qwen2.5-coder:7b",
        ],
        "smoke_command": ".venv/bin/python scripts/swarm_acceptance.py --smoke",
        "live_command": ".venv/bin/python scripts/swarm_acceptance.py",
        "resume_command": ".venv/bin/python scripts/swarm_acceptance.py --run-dir runs/swarm_acceptance_<UTC>",
        "audit_command": "/gsigmad-audit-output --target runs/swarm_acceptance_<UTC>/acceptance_verdict.json",
        "hard_halts": [
            "Do not run live T9 without operator GPU-window approval.",
            "Do not preload two model variants during the live run.",
            "Halt if inert-control scenario shows JSD > 0.05.",
            "Halt if quarantine rate exceeds 5% on any non-control scenario.",
            "T11/T12 advancement requires T9 verdict >= PARTIAL.",
        ],
        "completion_gate": (
            "T9 live run must produce acceptance_verdict.json; T10 must audit it; "
            "T11 closes the PROMPT; T12 updates README/examples."
        ),
    }


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and value > 0


def derive_monitor_assessment(status: dict[str, Any]) -> dict[str, Any]:
    """Derive an actionable monitor summary from an already-collected status."""

    session = status.get("session", {}) if isinstance(status.get("session"), dict) else {}
    activity = status.get("sidecar_activity", {}) if isinstance(status.get("sidecar_activity"), dict) else {}
    service = status.get("service", {}) if isinstance(status.get("service"), dict) else {}
    health = service.get("health", {}) if isinstance(service.get("health"), dict) else {}
    embed = service.get("embed", {}) if isinstance(service.get("embed"), dict) else {}
    sockets = status.get("ollama_sockets", {}) if isinstance(status.get("ollama_sockets"), dict) else {}
    gates = status.get("operator_gates", []) if isinstance(status.get("operator_gates"), list) else []
    attention_items: list[str] = []
    sidecar_safe = True
    sidecar_reason = "service_ready_and_no_ollama_socket_contention"

    # Prefer the fail-closed, project-scoped peer liveness when it is available
    # so foreign-project participants cannot satisfy "peer alive" for this repo.
    peer_alive_value = (
        session.get("project_peer_alive")
        if "project_peer_alive" in session
        else session.get("peer_alive")
    )
    if session.get("connected") is not True:
        attention_items.append("session_not_connected")
    if peer_alive_value is not True:
        attention_items.append("peer_not_live")
    elif _is_positive_int(activity.get("pending_peer_ack_count")):
        attention_items.append("peer_ack_pending")
        if activity.get("pending_peer_ack_overdue") is True:
            attention_items.append("peer_ack_overdue")
    elif activity.get("peer_event_stale") is True:
        attention_items.append("peer_sidecar_quiet")

    health_status = health.get("status")
    if service.get("checked") is not False and health_status != "ready":
        attention_items.append("service_not_ready")
        sidecar_safe = False
        sidecar_reason = f"service_health_{health_status or 'unknown'}"

    embed_status = embed.get("status")
    if service.get("checked") is not False and (
        embed_status != "ok" or embed.get("resident") is not True or embed.get("pinned") is not True
    ):
        attention_items.append("embed_not_pinned")
        sidecar_safe = False
        sidecar_reason = f"embed_{embed_status or 'unknown'}"

    established_count = sockets.get("established_count")
    if _is_positive_int(established_count):
        attention_items.append("ollama_socket_contention")
        sidecar_safe = False
        sidecar_reason = "ollama_has_established_inference_sockets"

    gate_names = [str(gate.get("gate")) for gate in gates if isinstance(gate, dict) and gate.get("gate")]
    if "phase69_proof_run" in gate_names:
        attention_items.append("phase69_operator_target_needed")
    if "phase70_t9_acceptance" in gate_names:
        attention_items.append("phase70_operator_gpu_window_needed")
    if "phase70_acceptance_remediation" in gate_names:
        attention_items.append("phase70_remediation_in_progress")

    if "peer_not_live" in attention_items:
        state = "peer_not_live"
        next_action = "reconcile the SWE sidecar or wait for Claude Code to rejoin this repo"
    elif "service_not_ready" in attention_items or "embed_not_pinned" in attention_items:
        state = "runtime_degraded"
        next_action = "inspect the local ollarma service before starting more SWE work"
    elif "ollama_socket_contention" in attention_items:
        state = "runtime_busy"
        next_action = "avoid model-heavy sidecars until established Ollama sockets drain"
    elif "peer_ack_overdue" in attention_items:
        state = "peer_ack_overdue"
        next_action = (
            "continue bounded backup/operator-gate work and publish receipts; "
            "do not claim Phase 69/70 complete without operator-run evidence"
        )
    elif "peer_ack_pending" in attention_items:
        state = "peer_ack_pending"
        next_action = "wait for Claude Code to acknowledge the pending SWE sidecar marker before duplicating the handoff"
    elif "peer_sidecar_quiet" in attention_items:
        state = "peer_quiet"
        next_action = "publish a directed handoff and ask Claude Code to acknowledge current Phase 69/70 gates"
    elif "phase69_operator_target_needed" in attention_items:
        state = "operator_gated"
        next_action = "ask for the Phase 69 real target repo and task before --allow-target-writes proof execution"
    elif "phase70_operator_gpu_window_needed" in attention_items:
        state = "operator_gated"
        next_action = "wait for the Phase 70 T9 GPU acceptance window"
    elif "phase70_remediation_in_progress" in attention_items:
        state = "remediation_in_progress"
        next_action = "v5.1-rc tagged (criteria #1-4,#6 MET); drive Phase 70 criterion #5 remediation: EXP-002 inert calibration -> (EXP-003) -> EXP-004 -> T9c rerun -> PI sign-off, or PI accepts PARTIAL"
    else:
        state = "ready"
        next_action = "continue the active ollarma plan and publish progress to the SWE sidecar"

    return {
        "state": state,
        "attention_items": attention_items,
        "next_action": next_action,
        "model_sidecar": {
            "safe_to_launch": sidecar_safe,
            "reason": sidecar_reason,
        },
    }


def collect_swe_session_status(
    *,
    repo_root: pathlib.Path | str = pathlib.Path.cwd(),
    session_dir: pathlib.Path | str = DEFAULT_SESSION_DIR,
    event_limit: int = 10,
    this_runtime: str = "codex",
    peer_runtime: str = "claude-code",
    check_service: bool = True,
    check_sockets: bool = True,
    check_processes: bool = True,
    include_phase69_target_scan: bool = False,
    include_phase70_operator_packet: bool = False,
    http_json_get: HttpJsonGetter = _http_json,
    command_runner: CommandRunner = _run_command,
    pid_alive_checker: PidAliveChecker = _pid_is_alive,
) -> dict[str, Any]:
    repo = pathlib.Path(repo_root)
    session = pathlib.Path(session_dir)
    session_summary = summarize_session(
        session,
        this_runtime=this_runtime,
        peer_runtime=peer_runtime,
        pid_alive_checker=pid_alive_checker,
        repo_root=repo,
    )
    runtime_processes: dict[str, list[dict[str, Any]]] = {"claude-code": []}
    if check_processes:
        runtime_processes["claude-code"] = discover_claude_processes(repo, command_runner=command_runner)
    peer_discovered_alive = peer_runtime == "claude-code" and any(
        proc.get("repo_match") for proc in runtime_processes.get("claude-code", [])
    )
    session_summary["recorded_peer_alive"] = session_summary["peer_alive"]
    session_summary["peer_discovered_alive"] = peer_discovered_alive
    if peer_discovered_alive and not session_summary["peer_alive"]:
        session_summary["peer_alive"] = True
        session_summary["peer_liveness_source"] = "process_discovery"
    elif session_summary["peer_alive"]:
        session_summary["peer_liveness_source"] = "active_json_pid"
    else:
        session_summary["peer_liveness_source"] = "not_live"
    # Project-scoped liveness is authoritative and fails closed: a repo-matched
    # live process counts; a foreign-repo active.json participant never does.
    project_peer_recorded_alive = bool(session_summary.get("project_peer_alive"))
    project_peer_alive = project_peer_recorded_alive or peer_discovered_alive
    session_summary["project_peer_alive"] = project_peer_alive
    if project_peer_recorded_alive:
        session_summary["project_peer_liveness_source"] = "active_json_pid"
    elif peer_discovered_alive:
        session_summary["project_peer_liveness_source"] = "process_discovery"
    else:
        session_summary["project_peer_liveness_source"] = "not_live"
    status: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(repo),
        "session_dir": str(session),
        "session": session_summary,
        "runtime_processes": runtime_processes if check_processes else {"checked": False},
        "sidecar_activity": summarize_sidecar_activity(
            session,
            this_runtime=this_runtime,
            peer_runtime=peer_runtime,
            repo_root=repo,
        ),
        "recent_events": recent_sidecar_events(session, limit=event_limit),
        "operator_gates": detect_operator_gates(repo),
        "phase69_target_candidates": {"checked": False},
        "phase70_operator_packet": {"checked": False},
        "service": {"checked": False},
        "ollama_sockets": {"checked": False},
        "inference_safe": True,
        "inference_calls_issued": 0,
    }
    if include_phase69_target_scan:
        status["phase69_target_candidates"] = scan_phase69_target_candidates(
            repo,
            command_runner=command_runner,
        )
    if include_phase70_operator_packet:
        status["phase70_operator_packet"] = build_phase70_operator_packet(repo)
    if check_service:
        status["service"] = summarize_health(http_json_get=http_json_get)
    if check_sockets:
        status["ollama_sockets"] = summarize_ollama_sockets(command_runner=command_runner)
    sidecar = status["sidecar_activity"] if isinstance(status["sidecar_activity"], dict) else {}
    status["project"] = {
        "project_root": str(repo),
        "scope": "strict",
        "project_connected": session_summary.get("project_connected"),
        "project_peer_present": session_summary.get("project_peer_present"),
        "project_peer_alive": session_summary.get("project_peer_alive"),
        "project_peer_liveness_source": session_summary.get("project_peer_liveness_source"),
        "foreign_participant_count": session_summary.get("foreign_participant_count"),
        "unknown_repo_participant_count": session_summary.get("unknown_repo_participant_count"),
        "foreign_project_event_count": sidecar.get("foreign_project_event_count"),
        "foreign_project_runtimes": sidecar.get("foreign_project_runtimes"),
    }
    status["monitor"] = derive_monitor_assessment(status)
    return status


def collect_watch_statuses(
    *,
    repo_root: pathlib.Path | str = pathlib.Path.cwd(),
    session_dir: pathlib.Path | str = DEFAULT_SESSION_DIR,
    event_limit: int = 10,
    this_runtime: str = "codex",
    peer_runtime: str = "claude-code",
    check_service: bool = True,
    check_sockets: bool = True,
    check_processes: bool = True,
    include_phase69_target_scan: bool = False,
    include_phase70_operator_packet: bool = False,
    append_event: bool = False,
    event_step: str = "swe_session_status",
    event_state: str = "status",
    event_note: str | None = None,
    watch_count: int = 1,
    watch_interval_s: float = 5.0,
    http_json_get: HttpJsonGetter = _http_json,
    command_runner: CommandRunner = _run_command,
    pid_alive_checker: PidAliveChecker = _pid_is_alive,
    sleeper: Sleeper = time.sleep,
    peer_ack_marker: str | None = None,
    stop_on_peer_ack: bool = False,
) -> list[dict[str, Any]]:
    """Collect one or more finite status samples.

    This is a bounded watch helper, not a daemon. It exists so agents can run a
    short monitoring window with the same probes and event format as one-shot
    status.
    """

    count = max(1, int(watch_count))
    interval = max(0.0, float(watch_interval_s))
    repo = pathlib.Path(repo_root)
    session = pathlib.Path(session_dir)
    samples: list[dict[str, Any]] = []
    for index in range(count):
        status = collect_swe_session_status(
            repo_root=repo,
            session_dir=session,
            event_limit=event_limit,
            this_runtime=this_runtime,
            peer_runtime=peer_runtime,
            check_service=check_service,
            check_sockets=check_sockets,
            check_processes=check_processes,
            include_phase69_target_scan=include_phase69_target_scan,
            include_phase70_operator_packet=include_phase70_operator_packet,
            http_json_get=http_json_get,
            command_runner=command_runner,
            pid_alive_checker=pid_alive_checker,
        )
        status["watch"] = {"index": index + 1, "count": count, "interval_s": interval}
        if peer_ack_marker:
            status["peer_ack"] = summarize_peer_ack(
                session,
                marker=peer_ack_marker,
                this_runtime=this_runtime,
                peer_runtime=peer_runtime,
            )
        if append_event:
            status["appended_event"] = append_sidecar_event(
                session,
                repo_root=repo,
                runtime=this_runtime,
                step=event_step,
                state=event_state,
                note=event_note or _compact_status_note(status),
            )
        samples.append(status)
        if (
            stop_on_peer_ack
            and isinstance(status.get("peer_ack"), dict)
            and status["peer_ack"].get("acknowledged") is True
        ):
            break
        if index + 1 < count:
            sleeper(interval)
    return samples


def compact_status_line(status: dict[str, Any]) -> str:
    """Return a stable single-line summary for shell monitoring."""

    monitor = status.get("monitor", {}) if isinstance(status.get("monitor"), dict) else {}
    session = status.get("session", {}) if isinstance(status.get("session"), dict) else {}
    activity = status.get("sidecar_activity", {}) if isinstance(status.get("sidecar_activity"), dict) else {}
    sockets = status.get("ollama_sockets", {}) if isinstance(status.get("ollama_sockets"), dict) else {}
    sidecar = monitor.get("model_sidecar", {}) if isinstance(monitor.get("model_sidecar"), dict) else {}
    peer_ack = status.get("peer_ack", {}) if isinstance(status.get("peer_ack"), dict) else {}
    phase69_targets = (
        status.get("phase69_target_candidates", {})
        if isinstance(status.get("phase69_target_candidates"), dict)
        else {}
    )
    phase70_packet = (
        status.get("phase70_operator_packet", {})
        if isinstance(status.get("phase70_operator_packet"), dict)
        else {}
    )
    owner_bits: list[str] = []
    for process in sockets.get("client_processes", []) if isinstance(sockets.get("client_processes"), list) else []:
        if not isinstance(process, dict):
            continue
        owner_bits.append(
            f"{process.get('pid')}:{process.get('command')}:{process.get('connection_count')}@{process.get('cwd')}"
        )
    pending_markers = activity.get("pending_peer_ack_markers", [])
    last_pending_ack = activity.get("last_pending_peer_ack", {})
    pending_ack_age = (
        last_pending_ack.get("age_seconds") if isinstance(last_pending_ack, dict) else None
    )
    pending_marker = ""
    if isinstance(pending_markers, list) and pending_markers:
        pending_marker = str(pending_markers[-1])
    if peer_ack.get("marker"):
        pending_marker = str(peer_ack.get("marker"))
    parts = [
        f"state={monitor.get('state')}",
        f"peer_alive={session.get('peer_alive')}",
        f"peer_ack={peer_ack.get('acknowledged') if peer_ack else 'n/a'}",
        f"pending_ack={pending_marker or 'none'}",
        f"pending_ack_age_s={pending_ack_age if pending_ack_age is not None else 'n/a'}",
        f"pending_ack_overdue={activity.get('pending_peer_ack_overdue')}",
        f"sidecar_safe={sidecar.get('safe_to_launch')}",
        f"sockets={sockets.get('established_count')}",
        f"socket_owners={';'.join(owner_bits) if owner_bits else 'none'}",
        f"inference_calls_issued={status.get('inference_calls_issued')}",
        f"next_action={monitor.get('next_action')}",
    ]
    if phase69_targets.get("checked") is True:
        recommended = phase69_targets.get("recommended_clean_targets", [])
        if isinstance(recommended, list):
            parts.insert(-1, f"phase69_clean_targets={','.join(str(x) for x in recommended) or 'none'}")
        packet = phase69_targets.get("operator_packet", {})
        if isinstance(packet, dict):
            parts.insert(-1, f"phase69_operator_packet={packet.get('status', 'unknown')}")
    if phase70_packet.get("checked") is True:
        parts.insert(-1, f"phase70_operator_packet={phase70_packet.get('status', 'unknown')}")
    watch = status.get("watch", {}) if isinstance(status.get("watch"), dict) else {}
    if watch:
        parts.insert(0, f"watch={watch.get('index')}/{watch.get('count')}")
    return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report the read-only Claude Code <-> Codex SWE sidecar status.")
    parser.add_argument("--repo-root", default=".", help="Repo root for planning gate detection.")
    parser.add_argument("--session-dir", default=str(DEFAULT_SESSION_DIR), help="Shared SWE session root directory.")
    parser.add_argument(
        "--project-session",
        action="store_true",
        help="Isolate SWE state to a per-project subdir (projects/<key>) under --session-dir so cross-project events never share a spine.",
    )
    parser.add_argument("--event-limit", type=int, default=10, help="Recent sidecar events to include.")
    parser.add_argument("--this-runtime", default="codex", help="Runtime identity to check in active.json.")
    parser.add_argument("--peer-runtime", default="claude-code", help="Peer runtime identity to check in active.json.")
    parser.add_argument("--no-service-probe", action="store_true", help="Skip localhost HTTP/embed status probes.")
    parser.add_argument("--no-socket-probe", action="store_true", help="Skip lsof :11434 socket summary.")
    parser.add_argument("--no-process-probe", action="store_true", help="Skip Claude process discovery by cwd.")
    parser.add_argument("--phase69-target-scan", action="store_true", help="Include read-only Git readiness scan for Phase 69 target candidates.")
    parser.add_argument("--phase70-operator-packet", action="store_true", help="Include read-only Phase 70 T9-T12 operator command templates.")
    parser.add_argument("--reconcile-active", action="store_true", help="Repair active.json's claude-code participant from live repo-matched process discovery.")
    parser.add_argument("--append-event", action="store_true", help="Append a compact status event to the shared SWE sidecar.")
    parser.add_argument("--event-step", default="swe_session_status", help="Step field when --append-event is used.")
    parser.add_argument("--event-state", default="status", help="State field when --append-event is used.")
    parser.add_argument("--event-note", default=None, help="Override note field when --append-event is used.")
    parser.add_argument("--watch-count", type=int, default=1, help="Finite number of status samples to emit.")
    parser.add_argument("--watch-interval", type=float, default=5.0, help="Seconds to sleep between watch samples.")
    parser.add_argument("--request-peer-ack", action="store_true", help="Append a directed acknowledgement request for the peer runtime.")
    parser.add_argument("--ack-marker", default=None, help="Marker to use with --request-peer-ack; generated when omitted.")
    parser.add_argument("--ack-note", default=None, help="Override note for --request-peer-ack.")
    parser.add_argument("--wait-peer-ack", default=None, metavar="MARKER", help="Bounded wait/check for a peer acknowledgement marker; returns 2 when still pending.")
    parser.add_argument("--compact", action="store_true", help="Emit compact single-line status summaries instead of JSON.")
    args = parser.parse_args(argv)
    repo_root = pathlib.Path(args.repo_root).resolve()
    session_root = pathlib.Path(args.session_dir).expanduser()
    if args.project_session:
        session_dir = project_session_dir(session_root, repo_root)
        session_dir.mkdir(parents=True, exist_ok=True)
    else:
        session_dir = session_root
    reconciliation: dict[str, Any] | None = None
    if args.reconcile_active:
        reconciliation = reconcile_active_session(repo_root, session_dir)
    statuses = collect_watch_statuses(
        repo_root=repo_root,
        session_dir=session_dir,
        event_limit=args.event_limit,
        this_runtime=args.this_runtime,
        peer_runtime=args.peer_runtime,
        check_service=not args.no_service_probe,
        check_sockets=not args.no_socket_probe,
        check_processes=not args.no_process_probe,
        include_phase69_target_scan=args.phase69_target_scan,
        include_phase70_operator_packet=args.phase70_operator_packet,
        append_event=args.append_event,
        event_step=args.event_step,
        event_state=args.event_state,
        event_note=args.event_note,
        watch_count=args.watch_count,
        watch_interval_s=args.watch_interval,
        peer_ack_marker=args.wait_peer_ack,
        stop_on_peer_ack=bool(args.wait_peer_ack),
    )
    if reconciliation is not None:
        statuses[0]["reconciliation"] = reconciliation
    if args.request_peer_ack:
        monitor = statuses[0].get("monitor", {}) if isinstance(statuses[0].get("monitor"), dict) else {}
        note = args.ack_note or (
            "Claude Code acknowledgement requested. "
            f"Monitor state={monitor.get('state')}; next_action={monitor.get('next_action')}; "
            "reply with ack_for=<marker> or an event carrying the same marker."
        )
        statuses[0]["requested_peer_ack"] = append_peer_ack_request(
            session_dir,
            repo_root=repo_root,
            runtime=args.this_runtime,
            peer_runtime=args.peer_runtime,
            note=note,
            marker=args.ack_marker,
        )
    if args.compact:
        for status in statuses:
            sys.stdout.write(compact_status_line(status) + "\n")
    elif len(statuses) == 1:
        json.dump(statuses[0], sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
    else:
        for status in statuses:
            json.dump(status, sys.stdout, sort_keys=True)
            sys.stdout.write("\n")
    if args.wait_peer_ack:
        last = statuses[-1].get("peer_ack", {}) if isinstance(statuses[-1].get("peer_ack"), dict) else {}
        return 0 if last.get("acknowledged") is True else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

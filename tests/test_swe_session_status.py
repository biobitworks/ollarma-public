"""Offline tests for the shared SWE-session status helper."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ollarma.swe_session_status import (
    append_peer_ack_request,
    append_sidecar_event,
    build_phase70_operator_packet,
    compact_status_line,
    collect_watch_statuses,
    collect_swe_session_status,
    derive_monitor_assessment,
    discover_claude_processes,
    main,
    recent_sidecar_events,
    reconcile_active_session,
    scan_phase69_target_candidates,
    summarize_session,
    summarize_sidecar_activity,
    summarize_peer_ack,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def test_summarize_session_reports_peer_presence(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()
    (session_dir / "active.json").write_text(
        json.dumps(
            {
                "participants": [
                    {"runtime": "claude-code", "pid": 1},
                    {"runtime": "codex", "pid": 2},
                ],
                "updated_at": "2026-05-31T15:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_session(session_dir, pid_alive_checker=lambda pid: pid == 1)

    assert summary["connected"] is True
    assert summary["peer_present"] is True
    assert summary["peer_alive"] is True
    assert summary["participant_runtimes"] == ["claude-code", "codex"]
    assert summary["participants"][0]["pid_alive"] is True
    assert summary["participants"][1]["pid_alive"] is False


def test_summarize_session_reports_stale_peer_pid(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()
    (session_dir / "active.json").write_text(
        json.dumps({"participants": [{"runtime": "claude-code", "pid": 12345}, {"runtime": "codex", "pid": 2}]}),
        encoding="utf-8",
    )

    summary = summarize_session(session_dir, pid_alive_checker=lambda pid: pid == 2)

    assert summary["peer_present"] is True
    assert summary["peer_alive"] is False
    assert summary["stale_participant_count"] == 1
    assert summary["participants"][0]["pid_liveness_reason"] == "stale_pid"


def test_summarize_session_handles_missing_pid(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()
    (session_dir / "active.json").write_text(
        json.dumps({"participants": [{"runtime": "claude-code"}]}),
        encoding="utf-8",
    )

    summary = summarize_session(session_dir)

    assert summary["peer_present"] is True
    assert summary["peer_alive"] is False
    assert summary["participants"][0]["pid_alive"] is None
    assert summary["participants"][0]["pid_liveness_reason"] == "pid_missing"


def test_recent_sidecar_events_returns_tail_only(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()
    _write_jsonl(session_dir / "events.jsonl", [{"n": 1}, {"n": 2}, {"n": 3}])

    assert recent_sidecar_events(session_dir, limit=2) == [{"n": 2}, {"n": 3}]


def test_summarize_sidecar_activity_tracks_peer_freshness(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "runtime": "claude-code",
                "step": "working",
                "state": "ok",
                "repo": "/repo",
                "note": "peer",
                "ts": "2026-05-31T14:50:00Z",
            },
            {
                "runtime": "codex",
                "step": "handoff",
                "state": "ok",
                "repo": "/repo",
                "note": "this",
                "ts": "2026-05-31T15:00:00Z",
            },
        ],
    )
    now = datetime(2026, 5, 31, 15, 10, tzinfo=timezone.utc).timestamp()

    activity = summarize_sidecar_activity(session_dir, stale_after_s=1800, now_epoch=now)

    assert activity["event_count"] == 2
    assert activity["runtime_counts"] == {"claude-code": 1, "codex": 1}
    assert activity["peer_event_seen"] is True
    assert activity["peer_event_stale"] is False
    assert activity["last_event"]["claude-code"]["age_seconds"] == 1200


def test_summarize_sidecar_activity_marks_missing_peer_event_stale(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()
    _write_jsonl(session_dir / "events.jsonl", [{"runtime": "codex", "ts": "2026-05-31T15:00:00Z"}])

    activity = summarize_sidecar_activity(session_dir, now_epoch=0)

    assert activity["peer_event_seen"] is False
    assert activity["peer_event_stale"] is True
    assert activity["last_event"]["claude-code"] is None


def test_append_sidecar_event_uses_shared_jsonl_shape(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    repo = tmp_path / "repo"
    repo.mkdir()

    event = append_sidecar_event(
        session_dir,
        repo_root=repo,
        runtime="codex",
        step="step",
        state="state",
        note="x" * 600,
        now="2026-05-31T15:45:00Z",
    )

    rows = [json.loads(line) for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert event["ts"] == "2026-05-31T15:45:00Z"
    assert rows == [event]
    assert rows[0]["runtime"] == "codex"
    assert rows[0]["repo"] == str(repo)
    assert rows[0]["step"] == "step"
    assert rows[0]["state"] == "state"
    assert len(rows[0]["note"]) == 500


def test_append_peer_ack_request_adds_directed_marker(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    repo = tmp_path / "repo"
    repo.mkdir()

    event = append_peer_ack_request(
        session_dir,
        repo_root=repo,
        runtime="codex",
        peer_runtime="claude-code",
        marker="ack-1",
        note="please acknowledge",
        now="2026-05-31T15:55:00Z",
    )

    rows = [json.loads(line) for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == [event]
    assert event["step"] == "swe_peer_ack_request"
    assert event["state"] == "ack_requested"
    assert event["marker"] == "ack-1"
    assert event["to_runtime"] == "claude-code"
    assert event["requires_ack"] is True
    assert event["ack_expected_from"] == "claude-code"


def test_summarize_sidecar_activity_reports_pending_peer_ack(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "runtime": "codex",
                "to_runtime": "claude-code",
                "requires_ack": True,
                "marker": "ack-1",
                "step": "swe_peer_ack_request",
                "state": "ack_requested",
                "ts": "2026-05-31T15:00:00Z",
            },
            {
                "runtime": "codex",
                "to_runtime": "claude-code",
                "requires_ack": True,
                "marker": "ack-2",
                "step": "swe_peer_ack_request",
                "state": "ack_requested",
                "ts": "2026-05-31T15:01:00Z",
            },
            {
                "runtime": "claude-code",
                "ack_for": "ack-1",
                "step": "swe_peer_ack",
                "state": "acknowledged",
                "ts": "2026-05-31T15:02:00Z",
            },
        ],
    )

    activity = summarize_sidecar_activity(session_dir, now_epoch=datetime(2026, 5, 31, 15, 10, tzinfo=timezone.utc).timestamp())

    assert activity["pending_peer_ack_count"] == 1
    assert activity["pending_peer_ack_markers"] == ["ack-2"]
    assert activity["last_pending_peer_ack"]["marker"] == "ack-2"
    assert activity["last_pending_peer_ack"]["age_seconds"] == 540
    assert activity["pending_peer_ack_overdue"] is True
    assert activity["pending_peer_ack_overdue_after_s"] == 300


def test_summarize_peer_ack_detects_peer_ack_for_marker(tmp_path: Path) -> None:
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "runtime": "codex",
                "to_runtime": "claude-code",
                "requires_ack": True,
                "marker": "ack-1",
                "step": "swe_peer_ack_request",
                "state": "ack_requested",
                "ts": "2026-05-31T15:00:00Z",
            },
            {
                "runtime": "claude-code",
                "ack_for": "ack-1",
                "step": "swe_peer_ack",
                "state": "acknowledged",
                "ts": "2026-05-31T15:01:00Z",
            },
        ],
    )

    ack = summarize_peer_ack(
        session_dir,
        marker="ack-1",
        now_epoch=datetime(2026, 5, 31, 15, 10, tzinfo=timezone.utc).timestamp(),
    )

    assert ack["acknowledged"] is True
    assert ack["request_seen"] is True
    assert ack["ack_event"]["step"] == "swe_peer_ack"
    assert ack["request_event"]["step"] == "swe_peer_ack_request"


def test_collect_status_is_read_only_and_inference_safe(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    (repo / ".planning").mkdir(parents=True)
    session_dir.mkdir()
    (repo / ".planning" / "STATE.md").write_text("phase_69_complete: false\nT9 operator-gated\n", encoding="utf-8")
    (repo / ".planning" / "ROADMAP.md").write_text("Phase 69 Operator gate\n", encoding="utf-8")
    (session_dir / "active.json").write_text(
        json.dumps({"participants": [{"runtime": "codex"}, {"runtime": "claude-code"}]}),
        encoding="utf-8",
    )
    _write_jsonl(session_dir / "events.jsonl", [{"runtime": "claude-code", "state": "working"}])

    def fake_http_json(url: str, timeout_s: float) -> dict:
        if url.endswith("/embed/status"):
            return {"status": "ok", "model": "nomic-embed-text", "available": True, "resident": True, "pinned": True}
        return {
            "status": "ready",
            "chat_selection": {"model": "phi4-mini"},
            "route_selection": {"model": "phi4-mini"},
            "startup_readiness": {
                "pipeline": {"loaded_models": ["nomic-embed-text:latest", "qwen2.5:1.5b"]},
                "swap": {"status": "ready", "swap_used_mb": 512},
            },
        }

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 0, "42\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "42":
            return subprocess.CompletedProcess(args, 0, f"p42\nn{repo}\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "10":
            return subprocess.CompletedProcess(args, 0, f"p10\nn{repo / 'service'}\n", "")
        assert args[0] == "lsof"
        stdout = (
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            "python 10 byron 1u IPv4 x 0t0 TCP 127.0.0.1:1->127.0.0.1:11434 (ESTABLISHED)\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    status = collect_swe_session_status(
        repo_root=repo,
        session_dir=session_dir,
        http_json_get=fake_http_json,
        command_runner=fake_command,
        pid_alive_checker=lambda pid: True,
    )

    assert status["inference_safe"] is True
    assert status["inference_calls_issued"] == 0
    assert status["session"]["peer_present"] is True
    assert status["session"]["peer_alive"] is True
    assert status["session"]["peer_discovered_alive"] is True
    assert status["session"]["peer_liveness_source"] == "process_discovery"
    assert status["runtime_processes"]["claude-code"][0]["repo_match"] is True
    assert status["sidecar_activity"]["peer_event_stale"] is True
    assert status["service"]["health"]["status"] == "ready"
    assert status["service"]["embed"]["pinned"] is True
    assert status["ollama_sockets"]["established_count"] == 1
    assert status["ollama_sockets"]["client_processes"] == [
        {
            "pid": 10,
            "command": "python",
            "connection_count": 1,
            "cwd": str(repo / "service"),
            "sample_names": ["127.0.0.1:1->127.0.0.1:11434 (ESTABLISHED)"],
        }
    ]
    assert status["monitor"]["state"] == "runtime_busy"
    assert status["monitor"]["model_sidecar"]["safe_to_launch"] is False
    assert "ollama_socket_contention" in status["monitor"]["attention_items"]
    assert {gate["gate"] for gate in status["operator_gates"]} == {
        "phase69_proof_run",
        "phase70_t9_acceptance",
    }


def test_derive_monitor_assessment_prefers_operator_gate_when_runtime_clear() -> None:
    assessment = derive_monitor_assessment(
        {
            "session": {"connected": True, "peer_alive": True},
            "service": {
                "checked": True,
                "health": {"status": "ready"},
                "embed": {"status": "ok", "resident": True, "pinned": True},
            },
            "ollama_sockets": {"checked": True, "established_count": 0},
            "operator_gates": [{"gate": "phase69_proof_run"}],
        }
    )

    assert assessment["state"] == "operator_gated"
    assert assessment["model_sidecar"]["safe_to_launch"] is True
    assert assessment["next_action"].startswith("ask for the Phase 69 real target")


def test_scan_phase69_target_candidates_ranks_clean_git_targets(tmp_path: Path) -> None:
    active = tmp_path / "active"
    repo = active / "ollarma"
    repo.mkdir(parents=True)
    for name in ["xenodisorder", "deltaprot", "missing"]:
        if name != "missing":
            (active / name).mkdir()

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        name = Path(args[2]).name
        if name == "missing":
            raise AssertionError("missing target should not call git")
        if args[3:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, str(active / name) + "\n", "")
        if args[3:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "main\n", "")
        if args[3:] == ["status", "--short"]:
            stdout = "" if name == "xenodisorder" else " M README.md\n"
            return subprocess.CompletedProcess(args, 0, stdout, "")
        raise AssertionError(args)

    scan = scan_phase69_target_candidates(
        repo,
        candidate_names=["deltaprot", "xenodisorder", "missing"],
        command_runner=fake_command,
    )

    assert scan["active_root"] == str(active)
    assert scan["recommended_clean_targets"] == ["xenodisorder"]
    assert scan["usable_with_review"] == ["deltaprot"]
    assert scan["missing_or_invalid"] == ["missing"]
    assert scan["operator_packet"]["status"] == "operator_target_task_required"
    assert "swarm_proof_run.py" in scan["operator_packet"]["preflight_smoke_command"]
    assert scan["operator_packet"]["proof_run_templates"] == [
        {
            "target": "xenodisorder",
            "command": (
                ".venv/bin/python scripts/swarm_proof_run.py "
                "--mode proof "
                f"--target '{active / 'xenodisorder'}' "
                "--task '<real task>' "
                "--allow-target-writes"
            ),
        }
    ]
    by_name = {item["name"]: item for item in scan["candidates"]}
    assert by_name["xenodisorder"]["status"] == "ready_clean"
    assert by_name["deltaprot"]["dirty_count"] == 1
    assert by_name["missing"]["status"] == "missing"


def test_collect_status_can_include_phase69_target_scan(tmp_path: Path) -> None:
    active = tmp_path / "active"
    repo = active / "ollarma"
    repo.mkdir(parents=True)
    (active / "xenodisorder").mkdir()
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[3:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, str(active / "xenodisorder") + "\n", "")
        if args[3:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "main\n", "")
        if args[3:] == ["status", "--short"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    status = collect_swe_session_status(
        repo_root=repo,
        session_dir=session_dir,
        check_service=False,
        check_sockets=False,
        include_phase69_target_scan=True,
        command_runner=fake_command,
    )

    assert status["phase69_target_candidates"]["checked"] is True
    assert status["phase69_target_candidates"]["recommended_clean_targets"] == ["xenodisorder"]
    assert status["phase69_target_candidates"]["operator_packet"]["proof_run_templates"][0]["target"] == "xenodisorder"
    assert status["inference_calls_issued"] == 0


def test_build_phase70_operator_packet_surfaces_gpu_gate_templates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "prompts").mkdir()
    (repo / "scripts" / "swarm_acceptance.py").write_text("# driver\n", encoding="utf-8")
    (repo / "prompts" / "PROMPT_OLLARMA_SWARM_001_LOCAL_GPU.md").write_text("# prompt\n", encoding="utf-8")

    packet = build_phase70_operator_packet(repo)

    assert packet["checked"] is True
    assert packet["prompt_id"] == "PROMPT-OLLARMA-SWARM-001"
    assert packet["status"] == "operator_gpu_window_required"
    assert packet["available"] is True
    assert packet["preflight_commands"] == [
        "ollama ps",
        "ollama list",
        "ollama pull qwen2.5-coder:7b",
    ]
    assert packet["smoke_command"] == ".venv/bin/python scripts/swarm_acceptance.py --smoke"
    assert packet["live_command"] == ".venv/bin/python scripts/swarm_acceptance.py"
    assert packet["resume_command"].endswith("--run-dir runs/swarm_acceptance_<UTC>")
    assert "acceptance_verdict.json" in packet["audit_command"]
    assert "Do not run live T9" in packet["hard_halts"][0]
    assert "T9 live run must produce acceptance_verdict.json" in packet["completion_gate"]


def test_collect_status_can_include_phase70_operator_packet(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "prompts").mkdir()
    (repo / "scripts" / "swarm_acceptance.py").write_text("# driver\n", encoding="utf-8")
    (repo / "prompts" / "PROMPT_OLLARMA_SWARM_001_LOCAL_GPU.md").write_text("# prompt\n", encoding="utf-8")
    session_dir = tmp_path / "swe_session"
    session_dir.mkdir()

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        raise AssertionError(args)

    status = collect_swe_session_status(
        repo_root=repo,
        session_dir=session_dir,
        check_service=False,
        check_sockets=False,
        include_phase70_operator_packet=True,
        command_runner=fake_command,
    )

    assert status["phase70_operator_packet"]["checked"] is True
    assert status["phase70_operator_packet"]["status"] == "operator_gpu_window_required"
    assert status["phase70_operator_packet"]["available"] is True
    assert status["inference_calls_issued"] == 0


def test_derive_monitor_assessment_reports_peer_not_live_first() -> None:
    assessment = derive_monitor_assessment(
        {
            "session": {"connected": True, "peer_alive": False},
            "sidecar_activity": {"peer_event_stale": True},
            "service": {
                "checked": True,
                "health": {"status": "ready"},
                "embed": {"status": "ok", "resident": True, "pinned": True},
            },
            "ollama_sockets": {"checked": True, "established_count": 0},
            "operator_gates": [{"gate": "phase69_proof_run"}],
        }
    )

    assert assessment["state"] == "peer_not_live"
    assert "peer_not_live" in assessment["attention_items"]
    assert "phase69_operator_target_needed" in assessment["attention_items"]


def test_derive_monitor_assessment_reports_quiet_live_peer() -> None:
    assessment = derive_monitor_assessment(
        {
            "session": {"connected": True, "peer_alive": True},
            "sidecar_activity": {"peer_event_stale": True},
            "service": {
                "checked": True,
                "health": {"status": "ready"},
                "embed": {"status": "ok", "resident": True, "pinned": True},
            },
            "ollama_sockets": {"checked": True, "established_count": 0},
            "operator_gates": [{"gate": "phase69_proof_run"}],
        }
    )

    assert assessment["state"] == "peer_quiet"
    assert "peer_sidecar_quiet" in assessment["attention_items"]
    assert assessment["next_action"].startswith("publish a directed handoff")


def test_derive_monitor_assessment_reports_pending_ack_before_quiet_peer() -> None:
    assessment = derive_monitor_assessment(
        {
            "session": {"connected": True, "peer_alive": True},
            "sidecar_activity": {
                "peer_event_stale": True,
                "pending_peer_ack_count": 1,
                "pending_peer_ack_overdue": True,
            },
            "service": {
                "checked": True,
                "health": {"status": "ready"},
                "embed": {"status": "ok", "resident": True, "pinned": True},
            },
            "ollama_sockets": {"checked": True, "established_count": 0},
            "operator_gates": [{"gate": "phase69_proof_run"}],
        }
    )

    assert assessment["state"] == "peer_ack_overdue"
    assert "peer_ack_pending" in assessment["attention_items"]
    assert "peer_ack_overdue" in assessment["attention_items"]
    assert "peer_sidecar_quiet" not in assessment["attention_items"]
    assert assessment["next_action"].startswith("continue bounded backup")


def test_derive_monitor_assessment_waits_for_fresh_pending_ack() -> None:
    assessment = derive_monitor_assessment(
        {
            "session": {"connected": True, "peer_alive": True},
            "sidecar_activity": {
                "peer_event_stale": True,
                "pending_peer_ack_count": 1,
                "pending_peer_ack_overdue": False,
            },
            "service": {
                "checked": True,
                "health": {"status": "ready"},
                "embed": {"status": "ok", "resident": True, "pinned": True},
            },
            "ollama_sockets": {"checked": True, "established_count": 0},
            "operator_gates": [{"gate": "phase69_proof_run"}],
        }
    )

    assert assessment["state"] == "peer_ack_pending"
    assert "peer_ack_pending" in assessment["attention_items"]
    assert "peer_ack_overdue" not in assessment["attention_items"]
    assert assessment["next_action"].startswith("wait for Claude Code")


def test_compact_status_line_summarizes_actionable_fields() -> None:
    line = compact_status_line(
        {
            "monitor": {
                "state": "runtime_busy",
                "next_action": "wait",
                "model_sidecar": {"safe_to_launch": False},
            },
            "session": {"peer_alive": True},
            "sidecar_activity": {
                "pending_peer_ack_markers": ["ack-1"],
                "last_pending_peer_ack": {"age_seconds": 450},
                "pending_peer_ack_overdue": True,
            },
            "phase69_target_candidates": {
                "checked": True,
                "recommended_clean_targets": ["xenodisorder"],
                "operator_packet": {"status": "operator_target_task_required"},
            },
            "phase70_operator_packet": {
                "checked": True,
                "status": "operator_gpu_window_required",
            },
            "ollama_sockets": {
                "established_count": 2,
                "client_processes": [
                    {
                        "pid": 10,
                        "command": "python",
                        "connection_count": 1,
                        "cwd": "/repo",
                    }
                ],
            },
            "peer_ack": {"marker": "ack-1", "acknowledged": False},
            "inference_calls_issued": 0,
            "watch": {"index": 1, "count": 2},
        }
    )

    assert "watch=1/2" in line
    assert "state=runtime_busy" in line
    assert "peer_ack=False" in line
    assert "pending_ack=ack-1" in line
    assert "pending_ack_age_s=450" in line
    assert "pending_ack_overdue=True" in line
    assert "sidecar_safe=False" in line
    assert "phase69_clean_targets=xenodisorder" in line
    assert "phase69_operator_packet=operator_target_task_required" in line
    assert "phase70_operator_packet=operator_gpu_window_required" in line
    assert "socket_owners=10:python:1@/repo" in line
    assert "inference_calls_issued=0" in line


def test_main_prints_json_without_live_probes(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
        ]
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["service"]["checked"] is False
    assert output["ollama_sockets"]["checked"] is False


def test_main_can_emit_phase70_operator_packet(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    (repo / "scripts").mkdir(parents=True)
    (repo / "prompts").mkdir()
    (repo / "scripts" / "swarm_acceptance.py").write_text("# driver\n", encoding="utf-8")
    (repo / "prompts" / "PROMPT_OLLARMA_SWARM_001_LOCAL_GPU.md").write_text("# prompt\n", encoding="utf-8")
    session_dir.mkdir()

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
            "--phase70-operator-packet",
        ]
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["phase70_operator_packet"]["checked"] is True
    assert output["phase70_operator_packet"]["available"] is True
    assert output["phase70_operator_packet"]["status"] == "operator_gpu_window_required"
    assert output["inference_calls_issued"] == 0


def test_main_can_append_event_with_custom_note(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
            "--append-event",
            "--event-step",
            "handoff",
            "--event-state",
            "ok",
            "--event-note",
            "ready",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    rows = [json.loads(line) for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert code == 0
    assert output["appended_event"]["step"] == "handoff"
    assert output["appended_event"]["state"] == "ok"
    assert output["appended_event"]["note"] == "ready"
    assert rows == [output["appended_event"]]


def test_main_can_request_peer_ack(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
            "--request-peer-ack",
            "--ack-marker",
            "ack-main",
            "--ack-note",
            "ack please",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    rows = [json.loads(line) for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert code == 0
    assert output["requested_peer_ack"]["marker"] == "ack-main"
    assert output["requested_peer_ack"]["note"] == "ack please"
    assert rows == [output["requested_peer_ack"]]


def test_collect_watch_statuses_is_bounded_and_can_append_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()
    sleeps: list[float] = []

    samples = collect_watch_statuses(
        repo_root=repo,
        session_dir=session_dir,
        check_service=False,
        check_sockets=False,
        check_processes=False,
        append_event=True,
        event_step="watch",
        event_state="ok",
        event_note="sample",
        watch_count=2,
        watch_interval_s=0.25,
        sleeper=sleeps.append,
    )

    rows = [json.loads(line) for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [sample["watch"]["index"] for sample in samples] == [1, 2]
    assert [sample["watch"]["count"] for sample in samples] == [2, 2]
    assert sleeps == [0.25]
    assert [row["step"] for row in rows] == ["watch", "watch"]
    assert [row["note"] for row in rows] == ["sample", "sample"]


def test_collect_watch_statuses_can_stop_on_peer_ack(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {"runtime": "codex", "to_runtime": "claude-code", "requires_ack": True, "marker": "ack-1"},
            {"runtime": "claude-code", "ack_for": "ack-1"},
        ],
    )
    sleeps: list[float] = []

    samples = collect_watch_statuses(
        repo_root=repo,
        session_dir=session_dir,
        check_service=False,
        check_sockets=False,
        check_processes=False,
        watch_count=3,
        watch_interval_s=1.0,
        peer_ack_marker="ack-1",
        stop_on_peer_ack=True,
        sleeper=sleeps.append,
    )

    assert len(samples) == 1
    assert sleeps == []
    assert samples[0]["peer_ack"]["acknowledged"] is True


def test_main_watch_mode_prints_jsonl(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
            "--watch-count",
            "2",
            "--watch-interval",
            "0",
        ]
    )

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert [line["watch"]["index"] for line in lines] == [1, 2]
    assert all(line["inference_calls_issued"] == 0 for line in lines)


def test_main_compact_watch_mode_prints_lines(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
            "--watch-count",
            "2",
            "--watch-interval",
            "0",
            "--compact",
        ]
    )

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert len(lines) == 2
    assert lines[0].startswith("watch=1/2 | ")
    assert "inference_calls_issued=0" in lines[0]


def test_main_wait_peer_ack_returns_two_when_pending(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()
    _write_jsonl(session_dir / "events.jsonl", [{"runtime": "codex", "to_runtime": "claude-code", "requires_ack": True, "marker": "ack-1"}])

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
            "--wait-peer-ack",
            "ack-1",
            "--watch-count",
            "1",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["peer_ack"]["acknowledged"] is False


def test_main_wait_peer_ack_returns_zero_when_acknowledged(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {"runtime": "codex", "to_runtime": "claude-code", "requires_ack": True, "marker": "ack-1"},
            {"runtime": "claude-code", "ack_for": "ack-1"},
        ],
    )

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
            "--wait-peer-ack",
            "ack-1",
            "--watch-count",
            "2",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["peer_ack"]["acknowledged"] is True


def test_discover_claude_processes_marks_repo_matches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 0, "11\n12\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "11":
            return subprocess.CompletedProcess(args, 0, f"p11\nn{repo}\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "12":
            return subprocess.CompletedProcess(args, 0, f"p12\nn{other}\n", "")
        raise AssertionError(args)

    processes = discover_claude_processes(repo, command_runner=fake_command)

    assert [(p["pid"], p["repo_match"]) for p in processes] == [(11, True), (12, False)]


def test_collect_status_uses_process_discovery_when_active_json_pid_is_stale(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()
    (session_dir / "active.json").write_text(
        json.dumps({"participants": [{"runtime": "claude-code", "pid": 999}]}),
        encoding="utf-8",
    )

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 0, "42\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "42":
            return subprocess.CompletedProcess(args, 0, f"p42\nn{repo}\n", "")
        return subprocess.CompletedProcess(args, 1, "", "")

    status = collect_swe_session_status(
        repo_root=repo,
        session_dir=session_dir,
        check_service=False,
        check_sockets=False,
        command_runner=fake_command,
        pid_alive_checker=lambda pid: False,
    )

    assert status["session"]["recorded_peer_alive"] is False
    assert status["session"]["peer_discovered_alive"] is True
    assert status["session"]["peer_alive"] is True
    assert status["session"]["peer_liveness_source"] == "process_discovery"


def test_reconcile_active_session_repairs_claude_participant(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    session_dir.mkdir()
    active_path = session_dir / "active.json"
    active_path.write_text(
        json.dumps(
            {
                "session_id": "swe-local",
                "participants": [
                    {"runtime": "codex", "pid": 1, "repo": str(repo)},
                    {"runtime": "claude-code", "pid": 999, "repo": str(repo), "joined_at": "old"},
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 0, "42\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "42":
            return subprocess.CompletedProcess(args, 0, f"p42\nn{repo}\n", "")
        raise AssertionError(args)

    result = reconcile_active_session(repo, session_dir, command_runner=fake_command, now="2026-05-31T15:40:00Z")

    repaired = json.loads(active_path.read_text(encoding="utf-8"))
    claude = next(p for p in repaired["participants"] if p["runtime"] == "claude-code")
    codex = next(p for p in repaired["participants"] if p["runtime"] == "codex")
    assert result["updated"] is True
    assert claude["pid"] == 42
    assert claude["joined_at"] == "old"
    assert claude["updated_at"] == "2026-05-31T15:40:00Z"
    assert claude["liveness_source"] == "process_discovery"
    assert codex["pid"] == 1


def test_reconcile_active_session_noops_without_repo_matched_claude(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    session_dir = tmp_path / "swe_session"
    repo.mkdir()
    other.mkdir()
    session_dir.mkdir()
    active_path = session_dir / "active.json"
    active_path.write_text(json.dumps({"session_id": "swe-local", "participants": []}), encoding="utf-8")

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 0, "42\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "42":
            return subprocess.CompletedProcess(args, 0, f"p42\nn{other}\n", "")
        raise AssertionError(args)

    result = reconcile_active_session(repo, session_dir, command_runner=fake_command)

    assert result["updated"] is False
    assert json.loads(active_path.read_text(encoding="utf-8"))["participants"] == []


# --- Project-scoped (fail-closed) SWE-session isolation -----------------------


def test_summarize_session_scopes_participants_to_current_project(tmp_path: Path) -> None:
    repo = tmp_path / "current"
    repo_a = tmp_path / "repoA"
    repo_b = tmp_path / "repoB"
    session_dir = tmp_path / "swe_session"
    for directory in (repo, repo_a, repo_b, session_dir):
        directory.mkdir()
    (session_dir / "active.json").write_text(
        json.dumps(
            {
                "participants": [
                    {"runtime": "claude-code", "pid": 1, "repo": str(repo)},
                    {"runtime": "codex", "pid": 2, "repo": str(repo_a)},
                    {"runtime": "claude-code", "pid": 3, "repo": str(repo_b)},
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_session(
        session_dir,
        this_runtime="codex",
        peer_runtime="claude-code",
        pid_alive_checker=lambda pid: True,
        repo_root=repo,
    )

    # Legacy unscoped view still reports presence across all repos.
    assert summary["peer_present"] is True
    # Project-scoped view counts only the current-repo participant.
    assert summary["project_scope"] == "strict"
    assert summary["project_root"] == str(repo)
    assert summary["project_peer_present"] is True
    assert summary["project_peer_alive"] is True
    assert summary["project_participant_count"] == 1
    assert summary["foreign_participant_count"] == 2
    # codex (this_runtime) only registered against repoA, so not connected here.
    assert summary["project_connected"] is False


def test_summarize_session_ignores_foreign_and_unknown_peer(tmp_path: Path) -> None:
    repo = tmp_path / "current"
    other = tmp_path / "other"
    session_dir = tmp_path / "swe_session"
    for directory in (repo, other, session_dir):
        directory.mkdir()
    (session_dir / "active.json").write_text(
        json.dumps(
            {
                "participants": [
                    {"runtime": "claude-code", "pid": 1, "repo": str(other)},
                    {"runtime": "claude-code", "pid": 2},  # no repo => unknown
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_session(
        session_dir, pid_alive_checker=lambda pid: True, repo_root=repo
    )

    assert summary["peer_present"] is True  # legacy unscoped
    assert summary["project_peer_present"] is False
    assert summary["project_peer_alive"] is False
    assert summary["foreign_participant_count"] == 2
    assert summary["unknown_repo_participant_count"] == 1


def test_summarize_sidecar_activity_buckets_foreign_project_events(tmp_path: Path) -> None:
    repo = tmp_path / "current"
    other = tmp_path / "other"
    session_dir = tmp_path / "swe_session"
    for directory in (repo, other, session_dir):
        directory.mkdir()
    now = datetime(2026, 6, 9, 13, 0, tzinfo=timezone.utc).timestamp()
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {"runtime": "claude-code", "repo": str(other), "ts": "2026-06-09T12:59:50Z", "state": "foreign"},
            {"runtime": "codex", "repo": str(repo), "ts": "2026-06-09T12:59:55Z", "state": "current"},
        ],
    )

    activity = summarize_sidecar_activity(session_dir, now_epoch=now, repo_root=repo)

    assert activity["project_scope"] == "strict"
    assert activity["project_event_count"] == 1
    assert activity["foreign_project_event_count"] == 1
    assert activity["total_event_count"] == 2
    assert "claude-code" in activity["foreign_project_runtimes"]
    # The peer only acted in a foreign repo: no current-project peer activity.
    assert activity["peer_event_seen"] is False
    assert activity["peer_event_stale"] is True


def test_foreign_peer_ack_does_not_clear_pending_request(tmp_path: Path) -> None:
    repo = tmp_path / "current"
    other = tmp_path / "other"
    session_dir = tmp_path / "swe_session"
    for directory in (repo, other, session_dir):
        directory.mkdir()
    now = datetime(2026, 6, 9, 13, 10, tzinfo=timezone.utc).timestamp()
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "runtime": "codex",
                "repo": str(repo),
                "to_runtime": "claude-code",
                "requires_ack": True,
                "marker": "m-1",
                "ts": "2026-06-09T13:00:00Z",
            },
            # A stale/foreign claude-code ack for the same marker must be ignored.
            {
                "runtime": "claude-code",
                "repo": str(other),
                "ack_for": "m-1",
                "marker": "m-1",
                "ts": "2026-06-09T13:05:00Z",
            },
        ],
    )

    activity = summarize_sidecar_activity(session_dir, now_epoch=now, repo_root=repo)
    assert activity["pending_peer_ack_count"] == 1
    assert "m-1" in activity["pending_peer_ack_markers"]

    ack = summarize_peer_ack(session_dir, marker="m-1", repo_root=repo)
    assert ack["acknowledged"] is False
    assert ack["project_scope"] == "strict"


def test_reconcile_active_session_ignores_foreign_claude_process(tmp_path: Path) -> None:
    repo = tmp_path / "current"
    other = tmp_path / "other"
    session_dir = tmp_path / "swe_session"
    for directory in (repo, other, session_dir):
        directory.mkdir()
    active_path = session_dir / "active.json"
    active_path.write_text(
        json.dumps(
            {
                "session_id": "swe-local",
                "participants": [{"runtime": "claude-code", "pid": 5, "repo": str(other)}],
            }
        ),
        encoding="utf-8",
    )

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 0, "77\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "77":
            return subprocess.CompletedProcess(args, 0, f"p77\nn{other}\n", "")
        raise AssertionError(args)

    result = reconcile_active_session(repo, session_dir, command_runner=fake_command)

    assert result["updated"] is False
    assert result["reason"] == "no_repo_matched_claude_process"
    # The foreign participant is left untouched, never promoted into this repo.
    participants = json.loads(active_path.read_text(encoding="utf-8"))["participants"]
    assert participants == [{"runtime": "claude-code", "pid": 5, "repo": str(other)}]


def test_collect_status_project_peer_alive_requires_repo_matched_process(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    session_dir = tmp_path / "swe_session"
    for directory in (repo, elsewhere, session_dir):
        directory.mkdir()
    (session_dir / "active.json").write_text(
        json.dumps({"participants": [{"runtime": "claude-code", "pid": 999, "repo": str(elsewhere)}]}),
        encoding="utf-8",
    )

    def fake_command(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(args, 0, "42\n", "")
        if args[:3] == ["lsof", "-a", "-p"] and args[3] == "42":
            return subprocess.CompletedProcess(args, 0, f"p42\nn{repo}\n", "")
        return subprocess.CompletedProcess(args, 1, "", "")

    status = collect_swe_session_status(
        repo_root=repo,
        session_dir=session_dir,
        check_service=False,
        check_sockets=False,
        command_runner=fake_command,
        pid_alive_checker=lambda pid: False,
    )

    # active.json peer is in a foreign repo (project_peer_present False), but a
    # live repo-matched Claude process makes the project peer authoritative-alive.
    assert status["session"]["project_peer_present"] is False
    assert status["session"]["project_peer_alive"] is True
    assert status["session"]["project_peer_liveness_source"] == "process_discovery"
    assert status["project"]["project_peer_alive"] is True


def test_main_status_is_project_scoped_and_fails_closed(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "current"
    other = tmp_path / "other"
    session_dir = tmp_path / "swe_session"
    for directory in (repo, other, session_dir):
        directory.mkdir()
    (session_dir / "active.json").write_text(
        json.dumps(
            {
                "participants": [
                    {"runtime": "claude-code", "pid": 1, "repo": str(other)},
                    {"runtime": "codex", "pid": 2, "repo": str(other)},
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        session_dir / "events.jsonl",
        [{"runtime": "claude-code", "repo": str(other), "state": "foreign"}],
    )

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_dir),
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["project"]["scope"] == "strict"
    assert output["project"]["project_root"] == str(repo)
    assert output["project"]["project_peer_present"] is False
    assert output["project"]["foreign_participant_count"] == 2
    assert output["project"]["foreign_project_event_count"] == 1
    # Fail closed: a foreign-repo peer never satisfies peer-alive for this repo.
    assert output["session"]["project_peer_alive"] is False
    assert "peer_not_live" in output["monitor"]["attention_items"]


# --- Per-project transport isolation (write side) ----------------------------


def test_project_session_dir_isolates_by_repo(tmp_path: Path) -> None:
    from ollarma.swe_session_status import project_key, project_session_dir

    root = tmp_path / "swe_session"
    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    repo_a.mkdir()
    repo_b.mkdir()

    assert project_key(repo_a) == "alpha"
    dir_a = project_session_dir(root, repo_a)
    dir_b = project_session_dir(root, repo_b)
    assert dir_a == root / "projects" / "alpha"
    assert dir_b == root / "projects" / "beta"
    assert dir_a != dir_b


def test_main_project_session_writes_under_per_project_dir(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "current"
    session_root = tmp_path / "swe_session"
    repo.mkdir()
    session_root.mkdir()

    code = main(
        [
            "--repo-root",
            str(repo),
            "--session-dir",
            str(session_root),
            "--project-session",
            "--no-service-probe",
            "--no-socket-probe",
            "--no-process-probe",
            "--append-event",
            "--event-note",
            "scoped-write",
        ]
    )

    assert code == 0
    # The event lands in the per-project subdir, NOT the global root spine.
    project_events = session_root / "projects" / "current" / "events.jsonl"
    assert project_events.exists()
    assert not (session_root / "events.jsonl").exists()
    rows = [json.loads(line) for line in project_events.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["note"] == "scoped-write"

    output = json.loads(capsys.readouterr().out)
    assert output["session_dir"].endswith(str(Path("projects") / "current"))
    assert output["project"]["project_root"] == str(repo)

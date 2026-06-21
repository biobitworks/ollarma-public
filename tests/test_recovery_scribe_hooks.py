"""Tests for scribe hook integration (phase 45, SCRIBE-01..04, TEST-07)."""
from __future__ import annotations

import pathlib
import time

import orjson
import pytest

from ollarma import scribe_hooks


@pytest.fixture
def temp_cwd(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Run with cwd=tmp_path so scribe I/O doesn't touch the real repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _enable_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks are enabled by default (no env var). Strip any env override."""
    monkeypatch.delenv(scribe_hooks.HOOK_ENV_VAR, raising=False)


def _read_entries(cwd: pathlib.Path) -> list[dict]:
    log = cwd / ".ollarma" / "session-log.jsonl"
    if not log.exists():
        return []
    lines = log.read_bytes().splitlines()
    return [orjson.loads(ln) for ln in lines if ln.strip()]


class TestPreDispatch:
    def test_writes_started_entry(self, temp_cwd: pathlib.Path) -> None:
        scribe_hooks.pre_dispatch("route_prompt", project="p", task="t")
        entries = _read_entries(temp_cwd)
        assert len(entries) == 1
        assert entries[0]["state"] == "started"
        assert entries[0]["project"] == "p"
        assert entries[0]["task"] == "t"
        assert "pre_dispatch:route_prompt" in entries[0]["notes"]

    def test_disabled_env_suppresses(
        self, temp_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(scribe_hooks.HOOK_ENV_VAR, "off")
        scribe_hooks.pre_dispatch("x")
        assert _read_entries(temp_cwd) == []


class TestEndOfRun:
    def test_completed_entry(self, temp_cwd: pathlib.Path) -> None:
        scribe_hooks.end_of_run("x", state="completed", project="p", task="t")
        entries = _read_entries(temp_cwd)
        assert entries[-1]["state"] == "completed"

    def test_blocked_entry_with_next_action(self, temp_cwd: pathlib.Path) -> None:
        scribe_hooks.end_of_run(
            "x", state="blocked", project="p", task="t",
            notes="boom", next_action="inspect",
        )
        entries = _read_entries(temp_cwd)
        assert entries[-1]["state"] == "blocked"
        assert entries[-1]["next_action"] == "inspect"

    def test_invalid_state_coerced(self, temp_cwd: pathlib.Path) -> None:
        scribe_hooks.end_of_run("x", state="weird")  # type: ignore[arg-type]
        entries = _read_entries(temp_cwd)
        assert entries[-1]["state"] == "completed"


class TestContextManager:
    def test_success_path_writes_started_and_completed(
        self, temp_cwd: pathlib.Path,
    ) -> None:
        with scribe_hooks.hooked("route_prompt", project="p", task="t"):
            pass
        entries = _read_entries(temp_cwd)
        states = [e["state"] for e in entries]
        assert states == ["started", "completed"]

    def test_exception_path_writes_blocked(self, temp_cwd: pathlib.Path) -> None:
        with pytest.raises(ValueError):
            with scribe_hooks.hooked("route_prompt", project="p", task="t"):
                raise ValueError("boom")
        entries = _read_entries(temp_cwd)
        assert entries[-1]["state"] == "blocked"
        assert "boom" in entries[-1]["notes"]


class TestHeartbeatThread:
    def test_heartbeat_emits_in_progress(
        self, temp_cwd: pathlib.Path,
    ) -> None:
        """Heartbeat with a short cadence emits in_progress entries."""
        hb = scribe_hooks.HeartbeatThread(
            "run_benchmark", project="p", task="t", cadence_s=0.1,
        )
        hb.start()
        time.sleep(0.35)  # allow ~3 ticks
        hb.stop()
        entries = _read_entries(temp_cwd)
        in_progress = [e for e in entries if e["state"] == "in_progress"]
        assert len(in_progress) >= 2  # allow timing slack

    def test_heartbeat_disabled_suppresses(
        self, temp_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(scribe_hooks.HOOK_ENV_VAR, "off")
        hb = scribe_hooks.HeartbeatThread(
            "run_benchmark", project="p", cadence_s=0.05,
        )
        hb.start()
        time.sleep(0.15)
        hb.stop()
        # Hooks disabled -> no session log at all
        assert not (temp_cwd / ".ollarma" / "session-log.jsonl").exists()


class TestVoluntaryScribeStillWorks:
    """SCRIBE-04: existing scribe_progress behavior unchanged."""
    def test_voluntary_call(self, temp_cwd: pathlib.Path) -> None:
        from ollarma.scribe import ScribeEntry, scribe_progress
        res = scribe_progress(
            project_root=temp_cwd,
            entry=ScribeEntry(project="voluntary", state="in_progress"),
        )
        assert res.written is True
        entries = _read_entries(temp_cwd)
        assert any(e["project"] == "voluntary" for e in entries)


class TestGatedEntrypointsEmitStarted:
    """Via fake inner bodies, confirm each wrapped entrypoint writes a
    ``started`` entry on invocation. We patch the admission check and the
    inner body so we don't need real adapters / manifests / agents.
    """

    def _install_admission_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ollarma import admission as _admission
        def _noop(*_a, **_k):
            return None
        monkeypatch.setattr(_admission, "check_recovery", _noop)

    def test_route_prompt_emits_started(
        self, temp_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._install_admission_noop(monkeypatch)
        from ollarma import service
        from ollarma.service import RouteResult
        def _fake_body(**_k) -> RouteResult:
            return RouteResult(
                final_response="ok", tool_calls_count=0, project="p",
            )
        monkeypatch.setattr(service, "_route_prompt_body", _fake_body)
        service.route_prompt("hello", project="p")
        entries = _read_entries(temp_cwd)
        states = [e["state"] for e in entries]
        assert "started" in states
        assert "completed" in states

    def test_run_agent_emits_blocked_on_body_error(
        self, temp_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._install_admission_noop(monkeypatch)
        from ollarma import service
        def _boom(**_k):
            raise RuntimeError("inner failure")
        monkeypatch.setattr(service, "_run_agent_body", _boom)
        with pytest.raises(RuntimeError):
            service.run_agent("HelperAgent", "hi")
        entries = _read_entries(temp_cwd)
        states = [e["state"] for e in entries]
        assert "started" in states
        assert "blocked" in states

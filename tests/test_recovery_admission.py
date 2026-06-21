"""Tests for recovery admission control (phase 44, ADMIT-01..07, TEST-06)."""
from __future__ import annotations

import pathlib
import subprocess

import orjson
import pytest

from ollarma import admission, recovery


def _git(repo: pathlib.Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")


@pytest.fixture
def clean_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def stranded_repo(clean_repo: pathlib.Path) -> pathlib.Path:
    """Clean repo + one stranded sidecar worktree with untracked files."""
    wt = clean_repo / ".claude" / "worktrees" / "x"
    wt.parent.mkdir(parents=True)
    _git(clean_repo, "worktree", "add", "-b", "claude/x", str(wt))
    (wt / "scratch.py").write_text("print('wip')\n")
    return clean_repo


@pytest.fixture(autouse=True)
def _clear_admission_cache():
    """Each test gets a fresh cache so TTL doesn't leak between cases."""
    admission._cache_clear()
    yield
    admission._cache_clear()


@pytest.fixture(autouse=True)
def _enable_admission_for_tests_in_this_file(monkeypatch):
    """Override conftest's session-wide admission-off default.

    conftest.py sets OLLARMA_RECOVERY_ADMISSION=off so the rest of the suite
    does not trip on developer sidecar worktrees. The admission tests here
    need admission ON to validate behavior; specific tests that want it off
    (e.g. ``test_admission_off_bypasses``) use their own ``monkeypatch.setenv``.
    """
    monkeypatch.delenv(admission.ADMISSION_ENV_VAR, raising=False)


class TestAdmissionPassthrough:
    def test_clean_repo_admits(self, clean_repo: pathlib.Path) -> None:
        state = admission.check_recovery(
            entrypoint="test", project_root=str(clean_repo),
        )
        assert state.state == "clean"

    def test_resume_context_admits(self, clean_repo: pathlib.Path) -> None:
        (clean_repo / ".ollarma").mkdir()
        (clean_repo / ".ollarma" / "RESUME.md").write_text("# resume\n")
        # resume_context_available is NOT admission-blocking
        state = admission.check_recovery(
            entrypoint="test", project_root=str(clean_repo),
        )
        assert state.state == "resume_context_available"


class TestAdmissionBlocks:
    def test_stranded_worktree_blocks(self, stranded_repo: pathlib.Path) -> None:
        with pytest.raises(admission.RecoveryRequiredError) as excinfo:
            admission.check_recovery(
                entrypoint="test", project_root=str(stranded_repo),
            )
        err = excinfo.value
        assert err.blocker_code == "STRANDED_WORKTREE"
        assert err.entrypoint == "test"
        assert err.next_fix_commands  # non-empty
        payload = err.to_error_payload()
        assert payload["error"] == "RECOVERY_REQUIRED"
        assert payload["blocker_code"] == "STRANDED_WORKTREE"

    def test_block_appends_receipt(self, stranded_repo: pathlib.Path) -> None:
        with pytest.raises(admission.RecoveryRequiredError):
            admission.check_recovery(
                entrypoint="route_prompt", project_root=str(stranded_repo),
            )
        receipts_path = stranded_repo / ".ollarma" / "recovery_block_receipts.jsonl"
        assert receipts_path.exists()
        lines = receipts_path.read_bytes().splitlines()
        assert len(lines) == 1
        parsed = orjson.loads(lines[0])
        assert parsed["blocker_code"] == "STRANDED_WORKTREE"
        assert parsed["entrypoint"] == "route_prompt"
        assert parsed["schema_version"] == 1


class TestCacheBehavior:
    def test_cache_reuses_scan(
        self, monkeypatch: pytest.MonkeyPatch, clean_repo: pathlib.Path,
    ) -> None:
        call_count = {"n": 0}
        original = recovery.scan

        def counting_scan(*a, **k):
            call_count["n"] += 1
            return original(*a, **k)

        monkeypatch.setattr(recovery, "scan", counting_scan)

        admission.check_recovery(entrypoint="a", project_root=str(clean_repo))
        admission.check_recovery(entrypoint="b", project_root=str(clean_repo))
        assert call_count["n"] == 1  # second call hit cache

    def test_force_bypasses_cache(
        self, monkeypatch: pytest.MonkeyPatch, clean_repo: pathlib.Path,
    ) -> None:
        call_count = {"n": 0}
        original = recovery.scan

        def counting_scan(*a, **k):
            call_count["n"] += 1
            return original(*a, **k)

        monkeypatch.setattr(recovery, "scan", counting_scan)

        admission.check_recovery(entrypoint="a", project_root=str(clean_repo))
        admission.check_recovery(
            entrypoint="b", project_root=str(clean_repo), force=True,
        )
        assert call_count["n"] == 2

    def test_env_force_bypasses_cache(
        self,
        monkeypatch: pytest.MonkeyPatch,
        clean_repo: pathlib.Path,
    ) -> None:
        call_count = {"n": 0}
        original = recovery.scan

        def counting_scan(*a, **k):
            call_count["n"] += 1
            return original(*a, **k)

        monkeypatch.setattr(recovery, "scan", counting_scan)
        monkeypatch.setenv(admission.FORCE_SCAN_ENV_VAR, "1")

        admission.check_recovery(entrypoint="a", project_root=str(clean_repo))
        admission.check_recovery(entrypoint="b", project_root=str(clean_repo))
        assert call_count["n"] == 2


class TestOptOut:
    def test_admission_off_bypasses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stranded_repo: pathlib.Path,
    ) -> None:
        monkeypatch.setenv(admission.ADMISSION_ENV_VAR, "off")
        # Should NOT raise even though repo is stranded.
        state = admission.check_recovery(
            entrypoint="test", project_root=str(stranded_repo),
        )
        assert state.state == "clean"  # synthetic admission-disabled state

    def test_admission_on_is_default(self) -> None:
        assert admission.admission_enabled() is True


class TestGatesOnServiceEntrypoints:
    """Verify each gated service function raises RecoveryRequiredError when
    admission is blocked. We monkey-patch ``check_recovery`` on the admission
    module to simulate a block, so tests don't need to stand up real stranded
    repos for every entrypoint.
    """

    def _install_fake_block(self, monkeypatch: pytest.MonkeyPatch, entrypoint: str) -> None:
        def fake_check(*, entrypoint, **_kwargs):  # noqa: ARG001
            raise admission.RecoveryRequiredError(
                blocker_code="STRANDED_WORKTREE",
                next_fix_commands=["git status"],
                packet={
                    "state": "stranded_worktree_detected",
                    "project_id": "t",
                    "timestamp": "2026-04-17T00:00:00+00:00",
                },
                entrypoint=entrypoint,
            )
        monkeypatch.setattr(admission, "check_recovery", fake_check)

    def test_route_prompt_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_block(monkeypatch, "route_prompt")
        from ollarma import service
        with pytest.raises(admission.RecoveryRequiredError) as exc:
            service.route_prompt("hi", project="anywhere")
        assert exc.value.entrypoint == "route_prompt"

    def test_submit_workflow_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_block(monkeypatch, "submit_workflow")
        from ollarma import service
        with pytest.raises(admission.RecoveryRequiredError) as exc:
            service.submit_workflow(
                project="x",
                manifest_ref="stable:nothing",  # type: ignore[arg-type]
                step_id="noop",
            )
        assert exc.value.entrypoint == "submit_workflow"

    def test_submit_autopilot_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_block(monkeypatch, "submit_autopilot")
        from ollarma import service
        with pytest.raises(admission.RecoveryRequiredError) as exc:
            service.submit_autopilot(project="x")
        assert exc.value.entrypoint == "submit_autopilot"

    def test_run_agent_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_block(monkeypatch, "run_agent")
        from ollarma import service
        with pytest.raises(admission.RecoveryRequiredError) as exc:
            service.run_agent("HelperAgent", "hi")
        assert exc.value.entrypoint == "run_agent"

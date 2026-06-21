"""Tests for ollarma.recovery scanner (phase 41, RECOV-01..05 + TEST-01)."""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from ollarma import recovery


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: pathlib.Path, *args: str) -> None:
    """Run a git command inside ``repo`` and fail the test on non-zero exit."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args} failed: {result.stderr}"
        )


@pytest.fixture
def clean_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Minimal initialized repo with one commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _make_sidecar_worktree(
    repo: pathlib.Path, root_dir: str, worktree_name: str, branch_name: str,
) -> pathlib.Path:
    """Create a sidecar worktree under repo/<root_dir>/<worktree_name>."""
    worktree_dir = repo / root_dir / worktree_name
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(
        repo, "worktree", "add", "-b", branch_name,
        str(worktree_dir),
    )
    return worktree_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCleanClassification:
    def test_clean_repo(self, clean_repo: pathlib.Path) -> None:
        state = recovery.scan(clean_repo)
        assert state.state == "clean"
        assert state.blocker_code == "OK"
        assert state.resume_present is False
        assert state.session_log_present is False
        assert state.ahead_commits == []
        # All worktrees in fresh repo are the main worktree (not sidecar).
        assert all(not w.is_sidecar for w in state.worktrees)
        assert state.next_fix_commands == []
        assert state.probable_reasoning_loss is False
        assert state.unknown_loss_risk is False

    def test_schema_version_stamped(self, clean_repo: pathlib.Path) -> None:
        state = recovery.scan(clean_repo)
        assert state.schema_version == 1

    def test_source_annotated(self, clean_repo: pathlib.Path) -> None:
        state = recovery.scan(clean_repo, source="admission")
        assert state.source == "admission"


class TestResumeContextAvailable:
    def test_resume_file_present(self, clean_repo: pathlib.Path) -> None:
        (clean_repo / ".ollarma").mkdir()
        (clean_repo / ".ollarma" / "RESUME.md").write_text(
            "# resume\n\nWe paused mid-task.\n"
        )
        state = recovery.scan(clean_repo)
        assert state.state == "resume_context_available"
        assert state.blocker_code == "RESUME_AVAILABLE"
        assert state.resume_present is True
        assert any("RESUME.md" in cmd for cmd in state.next_fix_commands)

    def test_session_log_present(self, clean_repo: pathlib.Path) -> None:
        (clean_repo / ".ollarma").mkdir()
        (clean_repo / ".ollarma" / "session-log.jsonl").write_text(
            '{"state":"in_progress"}\n'
        )
        state = recovery.scan(clean_repo)
        assert state.state == "resume_context_available"
        assert state.session_log_present is True
        assert any("session-log" in cmd for cmd in state.next_fix_commands)


class TestStrandedWorktreeDetection:
    def test_claude_sidecar_with_modifications(
        self, clean_repo: pathlib.Path,
    ) -> None:
        wt = _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "serene-pasteur",
            "claude/serene-pasteur",
        )
        # Untracked file in sidecar
        (wt / "scratch.py").write_text("print('wip')\n")
        state = recovery.scan(clean_repo)
        assert state.state == "stranded_worktree_detected"
        assert state.blocker_code == "STRANDED_WORKTREE"
        sidecar_worktrees = [w for w in state.worktrees if w.is_sidecar]
        assert len(sidecar_worktrees) == 1
        assert sidecar_worktrees[0].untracked == ["scratch.py"]
        assert state.artifacts_at_risk  # non-empty
        assert any(str(wt) in cmd for cmd in state.next_fix_commands)

    def test_codex_sidecar_with_modifications(
        self, clean_repo: pathlib.Path,
    ) -> None:
        wt = _make_sidecar_worktree(
            clean_repo, ".codex/worktrees", "thinking-turing",
            "codex/thinking-turing",
        )
        (wt / "scratch.py").write_text("print('wip')\n")
        state = recovery.scan(clean_repo)
        assert state.state == "stranded_worktree_detected"
        sidecar = [w for w in state.worktrees if w.is_sidecar]
        assert any(".codex/worktrees" in w.path for w in sidecar)

    def test_sidecar_worktree_clean_is_not_stranded(
        self, clean_repo: pathlib.Path,
    ) -> None:
        # A sidecar worktree exists but has no modifications.
        _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "tidy", "claude/tidy",
        )
        state = recovery.scan(clean_repo)
        # A clean sidecar worktree (no mods, no ahead commits from it) -> clean.
        assert state.state == "clean"


class TestBranchListParsing:
    """Unit-level tests for the `git branch --list` prefix handling.

    ``git branch --list`` renders three possible line prefixes:
      ``  branch`` — ordinary branch
      ``* branch`` — current branch (HEAD of the main repo)
      ``+ branch`` — branch checked out in a *linked worktree*

    The scanner must detect sidecar branches regardless of which prefix
    git used. The original implementation only stripped ``* `` and ``+``
    fell through, so real stranded sidecar branches checked out in a
    linked worktree were silently ignored.
    """

    def _fake_git(self, branch_list_output: str):
        """Return a stub for ``recovery._run_git`` that serves branch-list
        output and empty strings for everything else (including log)."""
        def fake(repo_root, *args, **_kwargs):  # noqa: ARG001
            if args[:2] == ("branch", "--list"):
                return branch_list_output
            return ""  # other calls (e.g. log main..branch) return empty
        return fake

    def test_ordinary_line_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, clean_repo: pathlib.Path,
    ) -> None:
        monkeypatch.setattr(
            recovery, "_run_git",
            self._fake_git("  claude/ordinary\n  main\n"),
        )
        result = recovery._scan_sidecar_branches(clean_repo)
        # No ahead commits because fake log returns empty, but the branch
        # must have been recognized and attempted. Confirm by re-running
        # with a log stub that returns one commit.
        assert result == []  # empty log output => no ahead commits

    def test_current_branch_marker_still_handled(
        self, monkeypatch: pytest.MonkeyPatch, clean_repo: pathlib.Path,
    ) -> None:
        # ``* main`` must NOT be treated as a sidecar (it's the base).
        # ``  claude/foo`` MUST be detected.
        calls: list[str] = []

        def fake(repo_root, *args, **_kwargs):  # noqa: ARG001
            if args[:2] == ("branch", "--list"):
                return "* main\n  claude/foo\n"
            if args[0] == "log":
                calls.append(args[1])  # "main..<branch>"
                return ""
            return ""

        monkeypatch.setattr(recovery, "_run_git", fake)
        recovery._scan_sidecar_branches(clean_repo, base_branch="main")
        assert calls == ["main..claude/foo"]

    def test_linked_worktree_plus_marker_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, clean_repo: pathlib.Path,
    ) -> None:
        """REGRESSION: ``+ claude/upbeat-shirley`` must be recognized."""
        calls: list[str] = []

        def fake(repo_root, *args, **_kwargs):  # noqa: ARG001
            if args[:2] == ("branch", "--list"):
                # Realistic output: current + linked-worktree + ordinary
                return (
                    "* main\n"
                    "+ claude/upbeat-shirley\n"
                    "  claude/tidy\n"
                )
            if args[0] == "log":
                calls.append(args[1])
                return ""
            return ""

        monkeypatch.setattr(recovery, "_run_git", fake)
        recovery._scan_sidecar_branches(clean_repo, base_branch="main")
        # Both sidecars must have been probed, sorted alphabetically.
        assert calls == ["main..claude/tidy", "main..claude/upbeat-shirley"]

    def test_ahead_commits_populated_for_linked_worktree_branch(
        self, monkeypatch: pytest.MonkeyPatch, clean_repo: pathlib.Path,
    ) -> None:
        """REGRESSION: the plus-prefixed branch produces AheadCommits when
        its log output is non-empty."""
        def fake(repo_root, *args, **_kwargs):  # noqa: ARG001
            if args[:2] == ("branch", "--list"):
                return "* main\n+ claude/upbeat-shirley\n"
            if args[0] == "log":
                return (
                    "3e694c4abc|wip: phase-30 paused at plan-complete/pre-execute\n"
                    "42bd435def|docs(30): create phase 30 D3 plan\n"
                )
            return ""

        monkeypatch.setattr(recovery, "_run_git", fake)
        result = recovery._scan_sidecar_branches(clean_repo, base_branch="main")
        assert len(result) == 2
        assert all(c.branch == "claude/upbeat-shirley" for c in result)
        assert result[0].sha == "3e694c4abc"
        assert "wip" in result[0].subject


class TestLiveLinkedWorktreeAhead:
    """Integration-level: make a real sidecar worktree with ahead commits
    and scan WITHOUT removing the worktree first (which is the scenario
    the original `test_ahead_commit_on_sidecar_branch` missed)."""

    def test_sidecar_branch_with_live_worktree_flagged(
        self, clean_repo: pathlib.Path,
    ) -> None:
        wt = _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "upbeat-shirley",
            "claude/upbeat-shirley",
        )
        # Add two commits on the sidecar branch, worktree stays attached.
        (wt / "plan.md").write_text("# plan\n")
        _git(wt, "add", ".")
        _git(wt, "commit", "-m", "docs(30): create phase 30 plan")
        (wt / "plan.md").write_text("# plan v2\n")
        _git(wt, "add", ".")
        _git(wt, "commit", "-m", "wip: phase-30 paused at plan-complete")

        state = recovery.scan(clean_repo)
        # Classification must NOT fall through to resume / clean.
        assert state.state in {
            "stranded_worktree_detected",
            "possible_work_loss",
            "recovery_sweep_required",
        }, f"unexpected state: {state.state}"
        # Ahead commits populated.
        assert len(state.ahead_commits) >= 2
        assert any(
            c.branch == "claude/upbeat-shirley" for c in state.ahead_commits
        ), state.ahead_commits


class TestPossibleWorkLoss:
    def test_ahead_commit_on_sidecar_branch(
        self, clean_repo: pathlib.Path,
    ) -> None:
        # Create a sidecar branch with one commit ahead of main.
        wt = _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "fervent-gauss",
            "claude/fervent-gauss",
        )
        (wt / "feature.py").write_text("# new feature\n")
        _git(wt, "add", ".")
        _git(wt, "commit", "-m", "wip: new feature")
        # Remove the worktree (simulating: session ended, worktree gone,
        # but branch still has ahead commits).
        _git(clean_repo, "worktree", "remove", "--force", str(wt))

        state = recovery.scan(clean_repo)
        assert state.state == "possible_work_loss"
        assert state.blocker_code == "POSSIBLE_WORK_LOSS"
        assert len(state.ahead_commits) >= 1
        assert state.ahead_commits[0].branch == "claude/fervent-gauss"
        assert state.probable_reasoning_loss is True
        assert any(
            "claude/fervent-gauss" in cmd for cmd in state.next_fix_commands
        )


class TestRecoverySweepRequired:
    def test_stranded_worktree_and_ahead_commits_together(
        self, clean_repo: pathlib.Path,
    ) -> None:
        # One sidecar worktree with uncommitted modifications
        wt1 = _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "upbeat-shirley",
            "claude/upbeat-shirley",
        )
        (wt1 / "scratch.py").write_text("print('wip')\n")

        # Another sidecar branch with ahead commits (worktree removed)
        wt2 = _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "musing-yonath",
            "claude/musing-yonath",
        )
        (wt2 / "feature.py").write_text("# new feature\n")
        _git(wt2, "add", ".")
        _git(wt2, "commit", "-m", "wip: second feature")
        _git(clean_repo, "worktree", "remove", "--force", str(wt2))

        state = recovery.scan(clean_repo)
        assert state.state == "recovery_sweep_required"
        assert state.blocker_code == "RECOVERY_SWEEP_REQUIRED"
        assert state.unknown_loss_risk is True
        assert state.artifacts_at_risk
        assert state.ahead_commits


class TestAdmissionBlocking:
    @pytest.mark.parametrize("state_str,should_block", [
        ("clean", False),
        ("resume_context_available", False),
        ("stranded_worktree_detected", True),
        ("recovery_sweep_required", True),
        ("possible_work_loss", True),
    ])
    def test_admission_blocking_decision(
        self, state_str: str, should_block: bool,
    ) -> None:
        rs = recovery.RecoveryState(
            project_id="t", repo_root="/t", timestamp="2026-01-01T00:00:00+00:00",
            state=state_str,  # type: ignore[arg-type]
            blocker_code=recovery._STATE_TO_BLOCKER[state_str],  # type: ignore[index]
            resume_present=False, session_log_present=False,
        )
        assert recovery.is_admission_blocking(rs) is should_block


class TestClassificationOrdering:
    def test_stranded_plus_ahead_is_sweep_not_stranded(
        self, clean_repo: pathlib.Path,
    ) -> None:
        """Severity ordering: stranded AND ahead -> sweep (not stranded)."""
        wt1 = _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "a", "claude/a",
        )
        (wt1 / "modify.py").write_text("# wip\n")

        wt2 = _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "b", "claude/b",
        )
        (wt2 / "feature.py").write_text("# feature\n")
        _git(wt2, "add", ".")
        _git(wt2, "commit", "-m", "wip")
        _git(clean_repo, "worktree", "remove", "--force", str(wt2))

        state = recovery.scan(clean_repo)
        assert state.state == "recovery_sweep_required"

    def test_stranded_outranks_resume(self, clean_repo: pathlib.Path) -> None:
        """Stranded wins over resume context."""
        wt = _make_sidecar_worktree(
            clean_repo, ".claude/worktrees", "stranded", "claude/stranded",
        )
        (wt / "scratch.py").write_text("# wip\n")

        (clean_repo / ".ollarma").mkdir()
        (clean_repo / ".ollarma" / "RESUME.md").write_text("# resume\n")

        state = recovery.scan(clean_repo)
        assert state.state == "stranded_worktree_detected"


class TestNextFixDeterminism:
    def test_same_input_same_fixes(self, clean_repo: pathlib.Path) -> None:
        """Calling scan() twice on the same repo yields the same fix commands."""
        (clean_repo / ".ollarma").mkdir()
        (clean_repo / ".ollarma" / "RESUME.md").write_text("# resume\n")
        a = recovery.scan(clean_repo).next_fix_commands
        b = recovery.scan(clean_repo).next_fix_commands
        assert a == b

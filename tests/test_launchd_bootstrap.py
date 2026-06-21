"""Tests for Plan 51-01: launchd bootstrap hardening (PERSIST-01).

The installer must never touch the live user LaunchAgent during test runs --
all subprocess + filesystem writes are mocked. The goal is to pin the exact
launchctl + plutil command shapes and failure semantics the operator relies on.
"""
from __future__ import annotations

import pathlib
import subprocess
from dataclasses import dataclass
from typing import Callable

import pytest
from typer.testing import CliRunner

from ollarma.cli import app

runner = CliRunner()


@dataclass
class _FakeResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _make_run_recorder(
    script: dict[tuple[str, ...], _FakeResult] | None = None,
    *,
    default: _FakeResult | None = None,
) -> tuple[list[list[str]], Callable]:
    """Return ``(calls, fake_run)`` so tests can assert exact subprocess argvs.

    ``script`` lets a test set a specific return per argv prefix; unmatched
    invocations get ``default`` (or a zero-code result if ``default`` is None).
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # noqa: ARG001 -- match subprocess.run signature
        calls.append(list(cmd))
        # Match on argv prefix tuples
        if script:
            for prefix, result in script.items():
                if tuple(cmd[: len(prefix)]) == prefix:
                    return result
        return default or _FakeResult(returncode=0)

    return calls, fake_run


@pytest.fixture
def fake_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect ``pathlib.Path.home()`` so installer writes under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def stub_copy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Intercept shutil.copy2 so tests don't need a real plist file."""
    import shutil as _shutil

    recorded: list[tuple[str, str]] = []

    def _fake_copy(src, dst, *args, **kwargs):  # noqa: ARG001
        recorded.append((str(src), str(dst)))
        # Create an empty target so downstream .exists() checks pass.
        pathlib.Path(str(dst)).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(str(dst)).write_text("<plist>")
        return str(dst)

    monkeypatch.setattr(_shutil, "copy2", _fake_copy)
    return recorded


class TestPlistValidation:
    def test_install_runs_plutil_lint_on_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """`plutil -lint` must run against the installed plist before bootstrap."""
        calls, fake_run = _make_run_recorder()
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output

        plutil_calls = [c for c in calls if c and c[0] == "plutil"]
        assert plutil_calls, f"expected plutil call; got {calls!r}"
        assert plutil_calls[0][:2] == ["plutil", "-lint"]

    def test_failed_plutil_aborts_with_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        script = {("plutil", "-lint"): _FakeResult(returncode=1, stderr="bad plist")}
        calls, fake_run = _make_run_recorder(script=script)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code != 0
        # No launchctl bootstrap when plutil fails.
        bootstrap = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
        assert bootstrap == [], f"bootstrap must not run after plutil failure: {calls!r}"


class TestLaunchctlCommandShape:
    def test_install_uses_modern_launchctl_sequence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """bootout -> bootstrap -> enable -> kickstart -> list -> print."""
        # Make the poll probe return non-zero immediately so it doesn't spin.
        script = {
            ("launchctl", "list", "com.byron.ollarma"): _FakeResult(returncode=1),
        }
        calls, fake_run = _make_run_recorder(script=script)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output

        # Extract launchctl subcommands in order; skip poll-list probes (internal
        # implementation detail of _bootout_and_wait) — they appear before bootstrap.
        bootstrap_idx = next(
            (i for i, c in enumerate(calls) if c[:2] == ["launchctl", "bootstrap"]),
            len(calls),
        )
        poll_probe_indices = {
            i for i, c in enumerate(calls[:bootstrap_idx])
            if c[:2] == ["launchctl", "list"]
        }
        launchctl = [
            c[1] for i, c in enumerate(calls)
            if c and c[0] == "launchctl" and i not in poll_probe_indices
        ]
        expected_order = ["bootout", "bootstrap", "enable", "kickstart", "list", "print"]
        assert launchctl == expected_order, f"wrong launchctl order: {launchctl}"

    def test_launchctl_uses_label_not_filename_for_bootout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """bootout target must be gui/<uid>/com.byron.ollarma (NOT ...plist)."""
        calls, fake_run = _make_run_recorder()
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output

        bootout = next((c for c in calls if c[:2] == ["launchctl", "bootout"]), None)
        assert bootout is not None
        target = bootout[2]
        assert target.endswith("/com.byron.ollarma"), target
        assert not target.endswith(".plist"), (
            f"bootout target must be label, not filename: {target!r}"
        )

    def test_launchctl_bootstrap_path_is_installed_plist(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        calls, fake_run = _make_run_recorder()
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output

        bootstrap = next(
            (c for c in calls if c[:2] == ["launchctl", "bootstrap"]), None,
        )
        assert bootstrap is not None
        # Shape: launchctl bootstrap gui/<uid> <plist-path>
        assert bootstrap[2].startswith("gui/")
        assert bootstrap[3].endswith("com.byron.ollarma.plist")
        assert str(fake_home) in bootstrap[3]


class TestFailureModes:
    def test_failed_bootstrap_reports_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        script = {
            ("launchctl", "bootstrap"): _FakeResult(
                returncode=5, stderr="Input/output error",
            ),
        }
        calls, fake_run = _make_run_recorder(script=script)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code != 0

    def test_bootout_failure_is_tolerated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """bootout is best-effort: if the service isn't loaded it returns 113."""
        script = {
            ("launchctl", "bootout"): _FakeResult(
                returncode=113, stderr="Could not find specified service",
            ),
        }
        calls, fake_run = _make_run_recorder(script=script)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output
        # Must still proceed to bootstrap after the tolerated bootout failure
        assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)


class TestSubprocessSafety:
    def test_all_subprocess_calls_use_timeout_and_no_shell(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """Record kwargs and confirm timeout is set and shell=False for every call."""
        captured_kwargs: list[dict] = []

        def fake_run(cmd, *args, **kwargs):  # noqa: ARG001
            captured_kwargs.append(kwargs)
            return _FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output
        assert captured_kwargs
        for kw in captured_kwargs:
            assert kw.get("shell") is not True, (
                f"no shell=True allowed: {kw!r}"
            )
            assert "timeout" in kw, f"every call must set timeout: {kw!r}"


class TestLogDirBootstrap:
    def test_install_creates_log_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        _, fake_run = _make_run_recorder()
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output
        log_dir = fake_home / "Library" / "Logs" / "ollarma"
        assert log_dir.is_dir()


class TestIdempotentReinstall:
    def test_second_install_with_target_present(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """Running --install twice must not crash; both calls should succeed."""
        calls, fake_run = _make_run_recorder()
        monkeypatch.setattr(subprocess, "run", fake_run)

        r1 = runner.invoke(app, ["start", "--install"])
        assert r1.exit_code == 0, r1.output
        r2 = runner.invoke(app, ["start", "--install"])
        assert r2.exit_code == 0, r2.output


class TestBootstrapRetry:
    def test_bootstrap_eio_retries_once_and_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """First bootstrap returns EIO (5); second attempt returns 0.

        Both bootout and bootstrap must be called exactly twice in total.
        """
        bootstrap_calls = 0

        def stateful_run(cmd, *_args, **_kwargs):
            nonlocal bootstrap_calls
            argv = list(cmd)
            # plutil always succeeds
            if argv[:2] == ["plutil", "-lint"]:
                return _FakeResult(returncode=0)
            # bootout always succeeds (best-effort)
            if argv[:2] == ["launchctl", "bootout"]:
                return _FakeResult(returncode=0)
            # poll: list returns 1 (gone) immediately to avoid sleep loops
            if argv[:2] == ["launchctl", "list"]:
                return _FakeResult(returncode=1)
            # bootstrap: fail first, succeed second
            if argv[:2] == ["launchctl", "bootstrap"]:
                bootstrap_calls += 1
                if bootstrap_calls == 1:
                    return _FakeResult(returncode=5, stderr="Input/output error")
                return _FakeResult(returncode=0)
            # everything else (enable, kickstart, print) succeeds
            return _FakeResult(returncode=0)

        calls: list[list[str]] = []

        def recording_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return stateful_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", recording_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output

        bootout_calls = [c for c in calls if c[:2] == ["launchctl", "bootout"]]
        bootstrap_cmds = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
        assert len(bootout_calls) == 2, f"expected 2 bootout calls, got: {bootout_calls}"
        assert len(bootstrap_cmds) == 2, f"expected 2 bootstrap calls, got: {bootstrap_cmds}"

    def test_bootstrap_eio_twice_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """Bootstrap returns EIO (5) on both attempts; result must be nonzero."""
        script = {
            ("launchctl", "bootstrap"): _FakeResult(
                returncode=5, stderr="Input/output error"
            ),
            # poll returns non-zero so it exits immediately
            ("launchctl", "list"): _FakeResult(returncode=1),
        }
        calls, fake_run = _make_run_recorder(script=script)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code != 0

    def test_poll_unload_between_bootout_and_bootstrap(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """Poll must observe: list returns 0 (loaded) then 1 (gone) before bootstrap.

        Verifies that _bootout_and_wait waits for the unloaded state before
        calling bootstrap.
        """
        list_call_count = 0

        def stateful_run(cmd, *_args, **_kwargs):
            nonlocal list_call_count
            argv = list(cmd)
            if argv[:2] == ["launchctl", "list"] and argv[2:] == ["com.byron.ollarma"]:
                list_call_count += 1
                # First call: still loaded (return 0); second call: gone (return 1)
                if list_call_count == 1:
                    return _FakeResult(returncode=0, stdout="loaded")
                return _FakeResult(returncode=1)
            return _FakeResult(returncode=0)

        calls: list[list[str]] = []

        def recording_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return stateful_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", recording_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output

        # Confirm at least two list probes happened before bootstrap
        bootstrap_idx = next(
            (i for i, c in enumerate(calls) if c[:2] == ["launchctl", "bootstrap"]),
            None,
        )
        assert bootstrap_idx is not None, "bootstrap must be called"

        list_probes_before_bootstrap = [
            c for c in calls[:bootstrap_idx]
            if c[:2] == ["launchctl", "list"] and c[2:] == ["com.byron.ollarma"]
        ]
        assert len(list_probes_before_bootstrap) >= 2, (
            f"expected >=2 poll probes before bootstrap, got {list_probes_before_bootstrap}"
        )


class TestKickstartTimeout:
    """Phase 55 DEBT-01: kickstart -kp TimeoutExpired is non-fatal.

    Bootstrap already succeeded by the time kickstart runs (Step 7).
    A timeout on kickstart is cosmetic — the service will start under
    launchd's KeepAlive control.  The installer must exit 0 and must
    still run the Step 8 list+print verification calls.
    """

    def test_kickstart_timeout_is_non_fatal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """TimeoutExpired on kickstart → exit 0, not exit 1."""
        import subprocess as _sp

        def kickstart_timeout_run(cmd, *args, **kwargs):
            argv = list(cmd)
            # poll list returns non-zero immediately so _bootout_and_wait exits fast
            if argv[:2] == ["launchctl", "list"] and len(argv) == 3:
                return _FakeResult(returncode=1)
            if argv[:2] == ["launchctl", "kickstart"]:
                raise _sp.TimeoutExpired(cmd=argv, timeout=10)
            return _FakeResult(returncode=0)

        monkeypatch.setattr(_sp, "run", kickstart_timeout_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, (
            f"kickstart timeout must be non-fatal; got exit {result.exit_code}.\n{result.output}"
        )
        assert "Warning" in result.output or "warning" in result.output.lower(), (
            f"expected a warning message in output; got:\n{result.output}"
        )

    def test_kickstart_timeout_does_not_skip_verification(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_home: pathlib.Path,
        stub_copy: list[tuple[str, str]],
    ) -> None:
        """Step 8 list+print verification must still run after kickstart timeout."""
        import subprocess as _sp

        calls: list[list[str]] = []

        def recording_run(cmd, *args, **kwargs):
            argv = list(cmd)
            calls.append(argv)
            if argv[:2] == ["launchctl", "list"] and len(argv) == 3:
                # poll probe: return 1 (gone) so _bootout_and_wait exits fast
                return _FakeResult(returncode=1)
            if argv[:2] == ["launchctl", "kickstart"]:
                raise _sp.TimeoutExpired(cmd=argv, timeout=10)
            return _FakeResult(returncode=0)

        monkeypatch.setattr(_sp, "run", recording_run)

        result = runner.invoke(app, ["start", "--install"])
        assert result.exit_code == 0, result.output

        # After kickstart timeout, Step 8 must still call list (label only) + print.
        # We need calls that happen AFTER the kickstart call.
        kickstart_idx = next(
            (i for i, c in enumerate(calls) if c[:2] == ["launchctl", "kickstart"]),
            None,
        )
        assert kickstart_idx is not None, "kickstart must have been attempted"

        post_kickstart = calls[kickstart_idx + 1 :]
        list_after = [c for c in post_kickstart if c[:2] == ["launchctl", "list"]]
        print_after = [c for c in post_kickstart if c[:2] == ["launchctl", "print"]]
        assert list_after, f"list verification must run after kickstart timeout; got {post_kickstart}"
        assert print_after, f"print verification must run after kickstart timeout; got {post_kickstart}"

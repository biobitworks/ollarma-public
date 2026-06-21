"""Tests for recovery packet persistence (phase 42, PACKET-01..04 + TEST-02)."""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from ollarma import recovery


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


class TestPacketFilename:
    def test_filename_format(self) -> None:
        state = recovery.RecoveryState(
            project_id="t",
            repo_root="/t",
            timestamp="2026-04-17T18:05:11.123456+00:00",
            state="clean",
            blocker_code="OK",
            resume_present=False,
            session_log_present=False,
        )
        name = recovery._packet_filename(state)
        assert name == "20260417T180511Z-clean.json"

    def test_filename_slug_dashes_not_underscores(self) -> None:
        state = recovery.RecoveryState(
            project_id="t",
            repo_root="/t",
            timestamp="2026-04-17T00:00:00+00:00",
            state="stranded_worktree_detected",
            blocker_code="STRANDED_WORKTREE",
            resume_present=False,
            session_log_present=False,
        )
        name = recovery._packet_filename(state)
        assert name.endswith("-stranded-worktree-detected.json")


class TestWritePacket:
    def test_write_creates_file_and_latest(
        self, clean_repo: pathlib.Path,
    ) -> None:
        state = recovery.scan(clean_repo)
        packet_path = recovery.write_packet(state, clean_repo)
        assert packet_path.exists()
        assert packet_path.parent.name == "incidents"
        assert packet_path.parent.parent.name == ".ollarma"

        latest = clean_repo / ".ollarma" / "incidents" / "latest.json"
        assert latest.exists()
        # latest.json must hold the same bytes as the named packet
        assert latest.read_bytes() == packet_path.read_bytes()

    def test_incidents_dir_bootstrapped(
        self, clean_repo: pathlib.Path,
    ) -> None:
        # Ensure no pre-existing .ollarma dir
        assert not (clean_repo / ".ollarma").exists()
        state = recovery.scan(clean_repo)
        recovery.write_packet(state, clean_repo)
        assert (clean_repo / ".ollarma" / "incidents").is_dir()


class TestRoundTrip:
    def test_read_after_write(self, clean_repo: pathlib.Path) -> None:
        state = recovery.scan(clean_repo)
        packet_path = recovery.write_packet(state, clean_repo)
        loaded = recovery.read_packet(packet_path)
        assert loaded.model_dump() == state.model_dump()

    def test_read_latest_returns_most_recent(
        self, clean_repo: pathlib.Path,
    ) -> None:
        # First scan
        s1 = recovery.scan(clean_repo, source="first")
        recovery.write_packet(s1, clean_repo)

        # Simulate a second, different scan
        s2 = recovery.scan(clean_repo, source="second")
        recovery.write_packet(s2, clean_repo)

        latest = recovery.read_latest_packet(clean_repo)
        assert latest is not None
        assert latest.source == "second"

    def test_read_latest_returns_none_when_no_packets(
        self, clean_repo: pathlib.Path,
    ) -> None:
        assert recovery.read_latest_packet(clean_repo) is None

    def test_schema_version_survives_roundtrip(
        self, clean_repo: pathlib.Path,
    ) -> None:
        state = recovery.scan(clean_repo)
        assert state.schema_version == 1
        packet_path = recovery.write_packet(state, clean_repo)
        loaded = recovery.read_packet(packet_path)
        assert loaded.schema_version == 1


class TestDeterminism:
    def test_same_state_same_bytes(self, clean_repo: pathlib.Path) -> None:
        """Two calls to _packet_bytes() on the same state produce identical bytes."""
        state = recovery.RecoveryState(
            project_id="t",
            repo_root="/t",
            timestamp="2026-04-17T00:00:00+00:00",
            state="clean",
            blocker_code="OK",
            resume_present=False,
            session_log_present=False,
            next_fix_commands=["cmd1", "cmd2"],
        )
        b1 = recovery._packet_bytes(state)
        b2 = recovery._packet_bytes(state)
        assert b1 == b2

    def test_sorted_keys_independent_of_field_order(self) -> None:
        """Sort-keys means bytes depend on content, not Python field declaration."""
        state = recovery.RecoveryState(
            project_id="t",
            repo_root="/t",
            timestamp="2026-04-17T00:00:00+00:00",
            state="clean",
            blocker_code="OK",
            resume_present=False,
            session_log_present=False,
        )
        b = recovery._packet_bytes(state)
        # Sanity: "blocker_code" comes before "project_id" alphabetically
        # (both at top level). Confirm JSON is sorted-key rendered.
        import orjson
        parsed = orjson.loads(b)
        keys = list(parsed.keys())
        assert keys == sorted(keys)


class TestScanAndPersist:
    def test_convenience_wrapper(self, clean_repo: pathlib.Path) -> None:
        state, path = recovery.scan_and_persist(clean_repo, source="smoke")
        assert path.exists()
        assert state.source == "smoke"
        loaded = recovery.read_latest_packet(clean_repo)
        assert loaded is not None
        assert loaded.source == "smoke"


class TestPacketHasAllRequiredFields:
    def test_all_packet_schema_fields_present(
        self, clean_repo: pathlib.Path,
    ) -> None:
        """PACKET-01: every required field in the scan output."""
        state = recovery.scan(clean_repo)
        dumped = state.model_dump()
        required = {
            "schema_version", "project_id", "repo_root", "timestamp",
            "source", "state", "blocker_code",
            "resume_present", "session_log_present",
            "worktrees", "ahead_commits", "modified_files",
            "untracked_files", "artifacts_at_risk",
            "probable_reasoning_loss", "unknown_loss_risk",
            "next_fix_commands", "notes",
        }
        assert required.issubset(dumped.keys())

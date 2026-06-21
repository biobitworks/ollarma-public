"""Tests for the scribe module — token-loss resilience progress capture."""
from __future__ import annotations

import orjson
import pytest

from ollarma.scribe import ScribeEntry, scribe_progress, read_resume, read_session_log


class TestScribeProgress:
    """Core scribe_progress functionality."""

    def test_creates_session_log(self, tmp_path):
        entry = ScribeEntry(project="test-proj", phase="1", task="T1", state="started")
        result = scribe_progress(tmp_path, entry)

        assert result.written is True
        assert result.entry_count == 1
        log_path = tmp_path / ".ollarma" / "session-log.jsonl"
        assert log_path.exists()

    def test_appends_multiple_entries(self, tmp_path):
        for i in range(3):
            entry = ScribeEntry(project="test-proj", phase="1", task=f"T{i}", state="in_progress")
            result = scribe_progress(tmp_path, entry)

        assert result.entry_count == 3

    def test_generates_resume_md(self, tmp_path):
        entry = ScribeEntry(
            project="ollarma",
            phase="39",
            task="39-01-T1",
            state="in_progress",
            notes="Wiring GuardrailGate into find_on_mac",
            next_action="Complete T2: wire read_sibling_file",
        )
        scribe_progress(tmp_path, entry)

        resume_path = tmp_path / ".ollarma" / "RESUME.md"
        assert resume_path.exists()
        content = resume_path.read_text()
        assert "ollarma" in content
        assert "39-01-T1" in content
        assert "in_progress" in content
        assert "Complete T2" in content

    def test_decisions_tracked(self, tmp_path):
        entry = ScribeEntry(
            project="test",
            phase="5",
            task="T1",
            state="completed",
            decisions=["Use lazy imports", "AdapterConfig needs explicit args"],
        )
        scribe_progress(tmp_path, entry)

        resume = (tmp_path / ".ollarma" / "RESUME.md").read_text()
        assert "Use lazy imports" in resume
        assert "AdapterConfig needs explicit args" in resume

    def test_artifacts_tracked(self, tmp_path):
        entry = ScribeEntry(
            project="test",
            phase="1",
            task="T1",
            state="completed",
            artifacts=["src/service.py", "tests/test_service.py"],
        )
        scribe_progress(tmp_path, entry)

        resume = (tmp_path / ".ollarma" / "RESUME.md").read_text()
        assert "src/service.py" in resume

    def test_jsonl_format(self, tmp_path):
        entry = ScribeEntry(project="test", phase="2", task="T1", state="started")
        scribe_progress(tmp_path, entry)

        log_path = tmp_path / ".ollarma" / "session-log.jsonl"
        raw = log_path.read_bytes().strip()
        parsed = orjson.loads(raw)
        assert parsed["project"] == "test"
        assert parsed["phase"] == "2"
        assert parsed["state"] == "started"


class TestReadResume:
    """read_resume function tests."""

    def test_returns_empty_when_no_resume(self, tmp_path):
        assert read_resume(tmp_path) == ""

    def test_returns_content_after_scribe(self, tmp_path):
        entry = ScribeEntry(project="test", phase="1", task="T1", state="started")
        scribe_progress(tmp_path, entry)

        content = read_resume(tmp_path)
        assert "Session Resume" in content
        assert "test" in content


class TestReadSessionLog:
    """read_session_log function tests."""

    def test_returns_empty_when_no_log(self, tmp_path):
        assert read_session_log(tmp_path) == []

    def test_returns_entries(self, tmp_path):
        for i in range(5):
            entry = ScribeEntry(project="test", phase="1", task=f"T{i}", state="in_progress")
            scribe_progress(tmp_path, entry)

        entries = read_session_log(tmp_path, last_n=3)
        assert len(entries) == 3
        assert entries[-1]["task"] == "T4"

    def test_last_n_limits_output(self, tmp_path):
        for i in range(10):
            entry = ScribeEntry(project="test", phase="1", task=f"T{i}", state="in_progress")
            scribe_progress(tmp_path, entry)

        entries = read_session_log(tmp_path, last_n=2)
        assert len(entries) == 2
        assert entries[0]["task"] == "T8"
        assert entries[1]["task"] == "T9"


class TestResumeRegeneration:
    """Test that RESUME.md stays current across multiple entries."""

    def test_resume_reflects_latest_state(self, tmp_path):
        # Start a task
        scribe_progress(tmp_path, ScribeEntry(
            project="test", phase="39", task="T1", state="started",
            next_action="Begin coding",
        ))

        # Complete it
        scribe_progress(tmp_path, ScribeEntry(
            project="test", phase="39", task="T1", state="completed",
            next_action="Run tests",
        ))

        resume = read_resume(tmp_path)
        assert "completed" in resume
        assert "Run tests" in resume

    def test_multiple_phases_in_resume(self, tmp_path):
        scribe_progress(tmp_path, ScribeEntry(
            project="test", phase="38", task="T1", state="completed",
        ))
        scribe_progress(tmp_path, ScribeEntry(
            project="test", phase="39", task="T1", state="started",
        ))

        resume = read_resume(tmp_path)
        assert "Phase: 38" in resume
        assert "Phase: 39" in resume

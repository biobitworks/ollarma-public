"""Tests for Phase 37 safety surfaces (SAFE-01, SAFE-03)."""
import pytest
from unittest.mock import MagicMock
from ollarma.macfind_searcher import _apply_antigence_redaction
from ollarma.guardrail import GateResult


def _make_gate(tristate="pass"):
    gate = MagicMock()
    gate_result = GateResult(
        tristate=tristate,
        blocked=(tristate == "block"),
        risk_level="LOW" if tristate == "pass" else "HIGH",
        confidence=0.9,
        reasons=[],
        is_code=False,
    )
    gate.validate.return_value = (tristate, gate_result)
    return gate


class MockHit:
    def __init__(self, path, snippet):
        self.path = path
        self.snippet = snippet


class TestAntigenceRedaction:
    def test_no_gate_returns_all_hits(self):
        """Without gate, all hits pass through unchanged."""
        hits = [MockHit("a.py", "hello world"), MockHit("b.py", "normal text")]
        events = []
        result = _apply_antigence_redaction(hits, gate=None, redaction_events=events)
        assert len(result) == 2
        assert events == []

    def test_clean_hit_skips_gate(self):
        """Hit with no secret pattern skips gate even if gate is provided."""
        gate = _make_gate("block")  # would block if called
        hits = [MockHit("a.py", "this is a normal file")]
        events = []
        result = _apply_antigence_redaction(hits, gate=gate, redaction_events=events)
        assert len(result) == 1
        gate.validate.assert_not_called()

    def test_secret_pattern_hit_blocked(self):
        """Hit with API_KEY pattern blocked by gate is excluded and logged."""
        gate = _make_gate("block")
        hits = [MockHit("secrets.env", "API_KEY=supersecret123")]
        events = []
        result = _apply_antigence_redaction(hits, gate=gate, redaction_events=events)
        assert len(result) == 0
        assert any("REDACTED:secrets.env" in e for e in events)

    def test_secret_pattern_hit_flagged(self):
        """Hit with TOKEN pattern flagged by gate is kept but annotated."""
        gate = _make_gate("flag")
        hits = [MockHit("config.py", "TOKEN=something_worth_reviewing")]
        events = []
        result = _apply_antigence_redaction(hits, gate=gate, redaction_events=events)
        assert len(result) == 1
        assert any("FLAGGED:config.py" in e for e in events)

    def test_clean_hit_passes_with_pass_gate(self):
        """Hit with no secret patterns passes through without redaction."""
        gate = _make_gate("pass")
        hits = [MockHit("readme.md", "This is documentation")]
        events = []
        result = _apply_antigence_redaction(hits, gate=gate, redaction_events=events)
        assert len(result) == 1
        assert events == []


class TestSiblingReaderGate:
    def test_read_sibling_passes_with_pass_gate(self, tmp_path):
        """read_sibling_path with pass gate returns content normally."""
        from ollarma.sibling_reader import read_sibling_path
        test_file = tmp_path / "test.txt"
        test_file.write_text("normal content")
        gate = _make_gate("pass")
        receipt = read_sibling_path(
            test_file,
            namespace="test",
            allowlisted_roots=[tmp_path],
            gate=gate,
        )
        assert receipt.content == "normal content"

    def test_read_sibling_blocked_by_gate(self, tmp_path):
        """read_sibling_path with block gate raises ValueError(SIBLING_CONTENT_BLOCKED)."""
        from ollarma.sibling_reader import read_sibling_path, SIBLING_CONTENT_BLOCKED
        test_file = tmp_path / "secret.env"
        test_file.write_text("API_KEY=supersecret")
        gate = _make_gate("block")
        with pytest.raises(ValueError, match=SIBLING_CONTENT_BLOCKED):
            read_sibling_path(
                test_file,
                namespace="test",
                allowlisted_roots=[tmp_path],
                gate=gate,
            )

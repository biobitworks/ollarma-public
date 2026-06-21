"""Tests for harness/sonifier.py -- Audio rendering of evidence chains.

Covers SONI-01 (evidence chain -> WAV), SONI-02 (dissonant tones at
tampered positions), SONI-04 (deterministic waveform, stdlib wave + numpy only).

TDD RED: These tests import from ollarma.sonifier which does not exist yet.
"""
from __future__ import annotations

import ast
import hashlib
import io
import struct
import wave

import numpy as np
import orjson
import pytest

from ollarma.evidence import (
    build_receipt_chain,
    canonical_hash,
    deterministic_row,
    write_evidence_file,
    GENESIS_PARENT_HASH,
)
from ollarma.sonifier import (
    dissonant_freq,
    make_tone,
    receipt_index_to_freq,
    sonify_chain,
    sonify_comparison,
    validate_receipts,
)


# ---------------------------------------------------------------------------
# Constants (must match sonifier module)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100
TONE_DURATION = 0.3
C4 = 261.63
SEMITONE = 2 ** (1 / 12)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result_dict(**overrides) -> dict:
    """Minimal BenchmarkResult dict with all deterministic + timing fields."""
    base = {
        "model": "qwen3:8b",
        "task_id": "science_01",
        "suite": "science",
        "num_ctx": 4096,
        "prefill_tps": 100.0,
        "decode_tps": 50.0,
        "quality_score": 0.85,
        "model_digest": "sha256:abc123",
        "ollama_version": "ollama version 0.19.0",
        "thinking_mode": False,
        "prompt_hash": "deadbeef" * 8,
        "schema_version": "1",
        "run_ts": "2026-04-07T00:00:00Z",
        "raw_response": "The answer is 42.",
    }
    base.update(overrides)
    return base


def _build_chain(rows: list[dict]) -> list[dict]:
    """Build evidence chain and return receipts as list of dicts."""
    chain = build_receipt_chain(rows, run_id="TEST")
    return [r.model_dump() for r in chain.receipts]


# ---------------------------------------------------------------------------
# TestValidateReceipts
# ---------------------------------------------------------------------------

class TestValidateReceipts:
    def test_all_valid_returns_all_true(self):
        """3 valid rows + receipts -> [True, True, True]."""
        rows = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
            _make_result_dict(task_id="t3"),
        ]
        receipts = _build_chain(rows)
        result = validate_receipts(rows, receipts)
        assert result == [True, True, True]

    def test_tampered_row_returns_false_at_position(self):
        """Tamper row[1] -> [True, False, True] (no cascade -- Pitfall 1)."""
        rows = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
            _make_result_dict(task_id="t3"),
        ]
        receipts = _build_chain(rows)
        # Tamper row[1] AFTER chain was built
        rows[1]["raw_response"] = "TAMPERED DATA"
        result = validate_receipts(rows, receipts)
        assert result[0] is True
        assert result[1] is False
        assert result[2] is True  # No cascade

    def test_tampered_receipt_hash_returns_false(self):
        """Tamper receipt[0].receipt_hash -> [False, False, True].

        Receipt[0] fails because its receipt_hash is wrong.
        Receipt[1] fails because the stored tampered receipt_hash becomes
        the parent for receipt[1], breaking the chain link.
        Receipt[2] passes because receipt[1]'s receipt_hash is untampered,
        so the chain link from receipt[1] -> receipt[2] is intact.
        """
        rows = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
            _make_result_dict(task_id="t3"),
        ]
        receipts = _build_chain(rows)
        receipts[0]["receipt_hash"] = "0" * 64
        result = validate_receipts(rows, receipts)
        assert result[0] is False
        assert result[1] is False  # Cascade from tampered chain link
        assert result[2] is True   # Recovers because receipt[1] hash is intact

    def test_mismatched_lengths_returns_all_false(self):
        """3 rows, 2 receipts -> [False, False, False]."""
        rows = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
            _make_result_dict(task_id="t3"),
        ]
        receipts = _build_chain(rows)[:2]  # Only 2 receipts
        result = validate_receipts(rows, receipts)
        assert result == [False, False, False]


# ---------------------------------------------------------------------------
# TestFrequencyMapping
# ---------------------------------------------------------------------------

class TestFrequencyMapping:
    def test_index_0_is_c4(self):
        """receipt_index_to_freq(0) == C4 (261.63 Hz)."""
        assert receipt_index_to_freq(0) == pytest.approx(261.63, abs=0.01)

    def test_index_7_is_c5(self):
        """receipt_index_to_freq(7) == C5 (523.25 Hz), one octave up."""
        assert receipt_index_to_freq(7) == pytest.approx(523.25, abs=0.1)

    def test_ascending_within_octave(self):
        """Frequencies increase within the first octave (indices 0..6)."""
        freqs = [receipt_index_to_freq(i) for i in range(7)]
        for i in range(6):
            assert freqs[i] < freqs[i + 1], f"freq[{i}] >= freq[{i + 1}]"

    def test_dissonant_is_tritone_above(self):
        """dissonant_freq(0) == receipt_index_to_freq(0) * (2**(6/12))."""
        expected = receipt_index_to_freq(0) * (2 ** (6 / 12))
        assert dissonant_freq(0) == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# TestMakeTone
# ---------------------------------------------------------------------------

class TestMakeTone:
    def test_returns_float64_array(self):
        """make_tone returns an np.float64 array."""
        tone = make_tone(440.0)
        assert tone.dtype == np.float64

    def test_length_matches_duration(self):
        """Tone length matches SAMPLE_RATE * duration."""
        tone = make_tone(440.0, 0.3)
        expected_len = int(SAMPLE_RATE * 0.3)
        assert len(tone) == expected_len

    def test_values_in_range(self):
        """All tone values fall within [-1.0, 1.0]."""
        tone = make_tone(440.0)
        assert np.all(tone >= -1.0)
        assert np.all(tone <= 1.0)

    def test_envelope_starts_at_zero(self):
        """First sample is 0.0 due to attack envelope."""
        tone = make_tone(440.0)
        assert tone[0] == 0.0


# ---------------------------------------------------------------------------
# TestSonifyChain
# ---------------------------------------------------------------------------

class TestSonifyChain:
    def test_returns_bytes(self):
        """sonify_chain returns bytes."""
        rows = [_make_result_dict(task_id="t1")]
        receipts = _build_chain(rows)
        result = sonify_chain(rows, receipts)
        assert isinstance(result, bytes)

    def test_valid_wav_header(self):
        """First 4 bytes == b'RIFF', bytes[8:12] == b'WAVE'."""
        rows = [_make_result_dict(task_id="t1")]
        receipts = _build_chain(rows)
        wav_bytes = sonify_chain(rows, receipts)
        assert wav_bytes[:4] == b"RIFF"
        assert wav_bytes[8:12] == b"WAVE"

    def test_mono_channel_count(self):
        """WAV nchannels == 1 (mono)."""
        rows = [_make_result_dict(task_id="t1")]
        receipts = _build_chain(rows)
        wav_bytes = sonify_chain(rows, receipts)
        with wave.open(io.BytesIO(wav_bytes), "r") as wf:
            assert wf.getnchannels() == 1


# ---------------------------------------------------------------------------
# TestTamperDetection
# ---------------------------------------------------------------------------

class TestTamperDetection:
    def test_tampered_receipt_produces_different_frequency(self):
        """WAV bytes differ from valid chain at tampered position."""
        rows = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
        ]
        receipts_valid = _build_chain(rows)
        valid_wav = sonify_chain(rows, receipts_valid)

        # Tamper row[0]
        tampered_rows = [
            _make_result_dict(task_id="t1", raw_response="TAMPERED"),
            _make_result_dict(task_id="t2"),
        ]
        tampered_wav = sonify_chain(tampered_rows, receipts_valid)

        assert valid_wav != tampered_wav


# ---------------------------------------------------------------------------
# TestDeterminism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_chain_same_bytes(self):
        """sonify_chain called twice with same input -> identical bytes."""
        rows = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
        ]
        receipts = _build_chain(rows)
        wav1 = sonify_chain(rows, receipts)
        wav2 = sonify_chain(rows, receipts)
        assert wav1 == wav2

    def test_sha256_of_output_is_stable(self):
        """hashlib.sha256(output).hexdigest() matches across calls."""
        rows = [_make_result_dict(task_id="t1")]
        receipts = _build_chain(rows)
        wav1 = sonify_chain(rows, receipts)
        wav2 = sonify_chain(rows, receipts)
        hash1 = hashlib.sha256(wav1).hexdigest()
        hash2 = hashlib.sha256(wav2).hexdigest()
        assert hash1 == hash2


# ---------------------------------------------------------------------------
# TestPureFunctions
# ---------------------------------------------------------------------------

class TestPureFunctions:
    def test_no_forbidden_imports(self):
        """sonifier.py only imports from {wave, numpy, np, harness.evidence, __future__, io}."""
        import pathlib

        sonifier_path = pathlib.Path(__file__).parent.parent / "src" / "ollarma" / "sonifier.py"
        source = sonifier_path.read_text()
        tree = ast.parse(source)

        allowed_modules = {"wave", "numpy", "np", "ollarma.evidence", "ollarma", "__future__", "io"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_root = alias.name.split(".")[0]
                    assert module_root in allowed_modules, (
                        f"Forbidden import: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    module_root = node.module.split(".")[0]
                    assert module_root in allowed_modules, (
                        f"Forbidden import: from {node.module}"
                    )


# ---------------------------------------------------------------------------
# TestSonifyComparison (stereo WAV assembly)
# ---------------------------------------------------------------------------

class TestSonifyComparison:
    def test_returns_bytes(self):
        """sonify_comparison returns bytes."""
        rows_a = [_make_result_dict(task_id="t1")]
        receipts_a = _build_chain(rows_a)
        rows_b = [_make_result_dict(task_id="t2")]
        receipts_b = _build_chain(rows_b)
        result = sonify_comparison(rows_a, receipts_a, rows_b, receipts_b)
        assert isinstance(result, bytes)

    def test_stereo_wav_header(self):
        """WAV nchannels == 2 (stereo), 16-bit, 44100 Hz."""
        rows_a = [_make_result_dict(task_id="t1")]
        receipts_a = _build_chain(rows_a)
        rows_b = [_make_result_dict(task_id="t2")]
        receipts_b = _build_chain(rows_b)
        wav_bytes = sonify_comparison(rows_a, receipts_a, rows_b, receipts_b)
        with wave.open(io.BytesIO(wav_bytes), "r") as wf:
            assert wf.getnchannels() == 2
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == SAMPLE_RATE

    def test_concordant_chains_same_frequencies(self):
        """Two identical chains produce left == right channel PCM."""
        rows = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
        ]
        receipts = _build_chain(rows)
        wav_bytes = sonify_comparison(rows, receipts, rows, receipts)

        with wave.open(io.BytesIO(wav_bytes), "r") as wf:
            raw_frames = wf.readframes(wf.getnframes())

        # Stereo 16-bit: interleaved L, R, L, R ...
        pcm = np.frombuffer(raw_frames, dtype=np.int16)
        left = pcm[0::2]
        right = pcm[1::2]
        np.testing.assert_array_equal(left, right)

    def test_discordant_chains_different_frequencies(self):
        """One valid chain + one tampered chain produce different L/R PCM.

        Chain A is valid (consonant tone). Chain B has its row tampered
        after receipt construction, so validation fails and produces a
        dissonant tone at position 0. L != R because consonant != dissonant.
        """
        rows_a = [_make_result_dict(task_id="t1")]
        receipts_a = _build_chain(rows_a)
        rows_b = [_make_result_dict(task_id="t1")]
        receipts_b = _build_chain(rows_b)
        # Tamper chain B's row data after building receipts
        rows_b[0]["raw_response"] = "TAMPERED DATA"
        wav_bytes = sonify_comparison(rows_a, receipts_a, rows_b, receipts_b)

        with wave.open(io.BytesIO(wav_bytes), "r") as wf:
            raw_frames = wf.readframes(wf.getnframes())

        pcm = np.frombuffer(raw_frames, dtype=np.int16)
        left = pcm[0::2]
        right = pcm[1::2]
        # Chain A valid (consonant) vs chain B tampered (dissonant) -> L != R
        assert not np.array_equal(left, right)

    def test_mismatched_lengths_pads_shorter(self):
        """Chain A (2 receipts) + chain B (3 receipts) -> output length matches 3-receipt duration."""
        rows_a = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
        ]
        receipts_a = _build_chain(rows_a)
        rows_b = [
            _make_result_dict(task_id="t3"),
            _make_result_dict(task_id="t4"),
            _make_result_dict(task_id="t5"),
        ]
        receipts_b = _build_chain(rows_b)
        wav_bytes = sonify_comparison(rows_a, receipts_a, rows_b, receipts_b)

        with wave.open(io.BytesIO(wav_bytes), "r") as wf:
            n_frames = wf.getnframes()

        expected_frames = int(SAMPLE_RATE * TONE_DURATION * 3)  # 3 receipts (longer chain)
        assert n_frames == expected_frames

    def test_deterministic_output(self):
        """Same inputs twice -> identical bytes."""
        rows_a = [_make_result_dict(task_id="t1")]
        receipts_a = _build_chain(rows_a)
        rows_b = [_make_result_dict(task_id="t2")]
        receipts_b = _build_chain(rows_b)
        wav1 = sonify_comparison(rows_a, receipts_a, rows_b, receipts_b)
        wav2 = sonify_comparison(rows_a, receipts_a, rows_b, receipts_b)
        assert wav1 == wav2


# ---------------------------------------------------------------------------
# TestBenchSonifyCLI (integration -- bench sonify <run-id>)
# ---------------------------------------------------------------------------

def _write_sealed_and_evidence_for_sonify(tmp_path, run_id="TEST"):
    """Helper: create sealed JSON + evidence JSON in tmp_path/results/."""
    results_dir = tmp_path / "results"
    results_dir.mkdir(exist_ok=True)

    rows = [
        _make_result_dict(task_id="t1"),
        _make_result_dict(task_id="t2"),
    ]
    sealed_path = results_dir / f"run-{run_id}.json"
    sealed_path.write_bytes(orjson.dumps(rows))

    chain = build_receipt_chain(rows, run_id=run_id)
    write_evidence_file(chain, results_dir, run_id)

    return rows, results_dir


class TestBenchSonifyCLI:
    def test_sonify_writes_wav_file(self, tmp_path, monkeypatch):
        """CLI writes run-{id}.sonify.wav."""
        from typer.testing import CliRunner
        from ollarma.cli import app

        _write_sealed_and_evidence_for_sonify(tmp_path, run_id="SON1")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["sonify", "SON1"])
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        wav_path = tmp_path / "results" / "run-SON1.sonify.wav"
        assert wav_path.exists(), f"WAV file not created: {wav_path}"

    def test_sonify_prints_receipt_count(self, tmp_path, monkeypatch):
        """Output contains receipt count."""
        from typer.testing import CliRunner
        from ollarma.cli import app

        _write_sealed_and_evidence_for_sonify(tmp_path, run_id="SON2")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["sonify", "SON2"])
        assert result.exit_code == 0
        assert "2 receipts" in result.output

    def test_sonify_missing_evidence_exits_1(self, tmp_path, monkeypatch):
        """Non-existent run-id -> exit 1."""
        from typer.testing import CliRunner
        from ollarma.cli import app

        monkeypatch.chdir(tmp_path)
        # Create empty results dir so path exists
        (tmp_path / "results").mkdir()

        runner = CliRunner()
        result = runner.invoke(app, ["sonify", "NONEXISTENT"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# TestBenchSonifyCompareCLI (integration -- bench sonify --compare)
# ---------------------------------------------------------------------------

class TestBenchSonifyCompareCLI:
    def test_compare_writes_stereo_wav(self, tmp_path, monkeypatch):
        """CLI writes compare-{a}-vs-{b}.wav."""
        from typer.testing import CliRunner
        from ollarma.cli import app

        _write_sealed_and_evidence_for_sonify(tmp_path, run_id="CMP-A")
        _write_sealed_and_evidence_for_sonify(tmp_path, run_id="CMP-B")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["sonify", "CMP-A", "--compare", "CMP-B"])
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        wav_path = tmp_path / "results" / "compare-CMP-A-vs-CMP-B.wav"
        assert wav_path.exists(), f"Stereo WAV not created: {wav_path}"

    def test_compare_missing_chain_exits_1(self, tmp_path, monkeypatch):
        """Missing comparison chain -> exit 1."""
        from typer.testing import CliRunner
        from ollarma.cli import app

        _write_sealed_and_evidence_for_sonify(tmp_path, run_id="EXISTS")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["sonify", "EXISTS", "--compare", "MISSING"])
        assert result.exit_code == 1

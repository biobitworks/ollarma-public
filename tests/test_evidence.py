"""Tests for harness/evidence.py — Evidence chain hash receipts.

Covers EVID-01 (append-only receipt chain with tamper detection) and
EVID-04 (deterministic evidence_root from identical inputs).
"""
import json
import pathlib
import tempfile

import orjson
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result_dict(**overrides) -> dict:
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


# ---------------------------------------------------------------------------
# GENESIS_PARENT_HASH constant
# ---------------------------------------------------------------------------

class TestGenesisParentHash:
    def test_genesis_parent_hash_length(self):
        from ollarma.evidence import GENESIS_PARENT_HASH
        assert len(GENESIS_PARENT_HASH) == 64

    def test_genesis_parent_hash_all_zeros(self):
        from ollarma.evidence import GENESIS_PARENT_HASH
        assert all(c == "0" for c in GENESIS_PARENT_HASH)


# ---------------------------------------------------------------------------
# DETERMINISTIC_FIELDS
# ---------------------------------------------------------------------------

class TestDeterministicFields:
    def test_deterministic_fields_exact_set(self):
        from ollarma.evidence import DETERMINISTIC_FIELDS
        expected = {
            "model", "task_id", "suite", "num_ctx",
            "raw_response", "quality_score",
            "model_digest", "ollama_version", "thinking_mode",
            "prompt_hash", "schema_version",
        }
        assert DETERMINISTIC_FIELDS == expected

    def test_deterministic_fields_excludes_timing(self):
        from ollarma.evidence import DETERMINISTIC_FIELDS
        assert "prefill_tps" not in DETERMINISTIC_FIELDS
        assert "decode_tps" not in DETERMINISTIC_FIELDS
        assert "run_ts" not in DETERMINISTIC_FIELDS


# ---------------------------------------------------------------------------
# canonical_hash
# ---------------------------------------------------------------------------

class TestCanonicalHash:
    def test_consistent_for_identical_input(self):
        from ollarma.evidence import canonical_hash
        d = {"a": 1, "b": 2}
        assert canonical_hash(d) == canonical_hash(d)

    def test_different_for_different_input(self):
        from ollarma.evidence import canonical_hash
        d1 = {"a": 1, "b": 2}
        d2 = {"a": 1, "b": 3}
        assert canonical_hash(d1) != canonical_hash(d2)

    def test_sorts_keys_recursively(self):
        from ollarma.evidence import canonical_hash
        # Different insertion order, same logical content
        d1 = {"z": {"b": 2, "a": 1}, "a": 0}
        d2 = {"a": 0, "z": {"a": 1, "b": 2}}
        assert canonical_hash(d1) == canonical_hash(d2)

    def test_returns_hex_string_of_length_64(self):
        from ollarma.evidence import canonical_hash
        h = canonical_hash({"x": 1})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# deterministic_row
# ---------------------------------------------------------------------------

class TestDeterministicRow:
    def test_extracts_only_deterministic_fields(self):
        from ollarma.evidence import deterministic_row, DETERMINISTIC_FIELDS
        row = _make_result_dict()
        det = deterministic_row(row)
        assert set(det.keys()) == DETERMINISTIC_FIELDS

    def test_excludes_timing_fields(self):
        from ollarma.evidence import deterministic_row
        row = _make_result_dict()
        det = deterministic_row(row)
        assert "prefill_tps" not in det
        assert "decode_tps" not in det
        assert "run_ts" not in det


# ---------------------------------------------------------------------------
# build_receipt_chain
# ---------------------------------------------------------------------------

class TestBuildReceiptChain:
    def test_single_row_returns_one_receipt(self):
        from ollarma.evidence import build_receipt_chain, GENESIS_PARENT_HASH
        rows = [_make_result_dict()]
        chain = build_receipt_chain(rows, run_id="TEST")
        assert chain.receipt_count == 1
        assert len(chain.receipts) == 1
        assert chain.receipts[0].parent_hash == GENESIS_PARENT_HASH
        assert chain.receipts[0].sequence == 0

    def test_three_rows_chained_parent_hash(self):
        from ollarma.evidence import build_receipt_chain
        rows = [
            _make_result_dict(task_id="t1"),
            _make_result_dict(task_id="t2"),
            _make_result_dict(task_id="t3"),
        ]
        chain = build_receipt_chain(rows, run_id="TEST")
        assert chain.receipt_count == 3
        assert len(chain.receipts) == 3
        # Chain: receipt[1].parent_hash == receipt[0].receipt_hash
        assert chain.receipts[1].parent_hash == chain.receipts[0].receipt_hash
        # Chain: receipt[2].parent_hash == receipt[1].receipt_hash
        assert chain.receipts[2].parent_hash == chain.receipts[1].receipt_hash

    def test_evidence_root_deterministic(self):
        from ollarma.evidence import build_receipt_chain
        rows = [_make_result_dict(task_id="t1"), _make_result_dict(task_id="t2")]
        chain1 = build_receipt_chain(rows, run_id="TEST")
        chain2 = build_receipt_chain(rows, run_id="TEST")
        assert chain1.evidence_root == chain2.evidence_root

    def test_evidence_root_is_last_receipt_hash(self):
        from ollarma.evidence import build_receipt_chain
        rows = [_make_result_dict(task_id="t1"), _make_result_dict(task_id="t2")]
        chain = build_receipt_chain(rows, run_id="TEST")
        assert chain.evidence_root == chain.receipts[-1].receipt_hash

    def test_empty_rows_returns_genesis_root(self):
        from ollarma.evidence import build_receipt_chain, GENESIS_PARENT_HASH
        chain = build_receipt_chain([], run_id="TEST")
        assert chain.evidence_root == GENESIS_PARENT_HASH
        assert chain.receipt_count == 0


# ---------------------------------------------------------------------------
# verify_evidence_chain
# ---------------------------------------------------------------------------

class TestVerifyEvidenceChain:
    def test_valid_chain_succeeds(self):
        from ollarma.evidence import build_receipt_chain, verify_evidence_chain
        rows = [_make_result_dict(task_id="t1"), _make_result_dict(task_id="t2")]
        chain = build_receipt_chain(rows, run_id="TEST")
        receipts = [r.model_dump() for r in chain.receipts]
        root = verify_evidence_chain(rows, receipts)
        assert root == chain.evidence_root

    def test_tampered_row_raises_value_error(self):
        """EVID-01: Modifying any row breaks the chain."""
        from ollarma.evidence import build_receipt_chain, verify_evidence_chain
        rows = [_make_result_dict(task_id="t1"), _make_result_dict(task_id="t2")]
        chain = build_receipt_chain(rows, run_id="TEST")
        receipts = [r.model_dump() for r in chain.receipts]
        # Tamper with first row
        rows[0]["raw_response"] = "TAMPERED"
        with pytest.raises(ValueError, match="row_hash"):
            verify_evidence_chain(rows, receipts)

    def test_receipt_count_mismatch_raises(self):
        from ollarma.evidence import build_receipt_chain, verify_evidence_chain
        rows = [_make_result_dict(task_id="t1"), _make_result_dict(task_id="t2")]
        chain = build_receipt_chain(rows, run_id="TEST")
        receipts = [r.model_dump() for r in chain.receipts]
        # Remove one receipt
        with pytest.raises(ValueError, match="count"):
            verify_evidence_chain(rows, receipts[:1])

    def test_tampered_receipt_hash_raises(self):
        from ollarma.evidence import build_receipt_chain, verify_evidence_chain
        rows = [_make_result_dict(task_id="t1"), _make_result_dict(task_id="t2")]
        chain = build_receipt_chain(rows, run_id="TEST")
        receipts = [r.model_dump() for r in chain.receipts]
        # Tamper receipt_hash
        receipts[0]["receipt_hash"] = "0" * 64
        with pytest.raises(ValueError, match="receipt_hash"):
            verify_evidence_chain(rows, receipts)


# ---------------------------------------------------------------------------
# EVID-04: deterministic evidence_root
# ---------------------------------------------------------------------------

class TestDeterministicEvidenceRoot:
    def test_identical_rows_produce_same_root(self):
        """Two calls with identical rows produce same evidence_root."""
        from ollarma.evidence import build_receipt_chain
        rows = [_make_result_dict(task_id="t1"), _make_result_dict(task_id="t2")]
        chain1 = build_receipt_chain(rows, run_id="RUN-A")
        chain2 = build_receipt_chain(rows, run_id="RUN-B")
        assert chain1.evidence_root == chain2.evidence_root

    def test_same_deterministic_different_timing_same_root(self):
        """Same deterministic fields, different timing -> same evidence_root (EVID-04)."""
        from ollarma.evidence import build_receipt_chain
        rows_a = [_make_result_dict(prefill_tps=100.0, decode_tps=50.0, run_ts="2026-04-07T00:00:00Z")]
        rows_b = [_make_result_dict(prefill_tps=200.0, decode_tps=99.0, run_ts="2026-04-07T12:00:00Z")]
        chain_a = build_receipt_chain(rows_a, run_id="A")
        chain_b = build_receipt_chain(rows_b, run_id="B")
        assert chain_a.evidence_root == chain_b.evidence_root


# ---------------------------------------------------------------------------
# write_evidence_file
# ---------------------------------------------------------------------------

class TestWriteEvidenceFile:
    def test_writes_valid_json(self, tmp_path):
        from ollarma.evidence import build_receipt_chain, write_evidence_file
        rows = [_make_result_dict()]
        chain = build_receipt_chain(rows, run_id="TEST")
        path = write_evidence_file(chain, tmp_path, "TEST")
        data = orjson.loads(path.read_bytes())
        assert "run_id" in data
        assert "schema_version" in data
        assert "evidence_root" in data
        assert "receipt_count" in data
        assert "receipts" in data

    def test_writes_correct_filename(self, tmp_path):
        from ollarma.evidence import build_receipt_chain, write_evidence_file
        rows = [_make_result_dict()]
        chain = build_receipt_chain(rows, run_id="TEST")
        path = write_evidence_file(chain, tmp_path, "TEST")
        assert path.name == "run-TEST.evidence.json"

    def test_evidence_root_matches_chain(self, tmp_path):
        from ollarma.evidence import build_receipt_chain, write_evidence_file
        rows = [_make_result_dict()]
        chain = build_receipt_chain(rows, run_id="TEST")
        path = write_evidence_file(chain, tmp_path, "TEST")
        data = orjson.loads(path.read_bytes())
        assert data["evidence_root"] == chain.evidence_root


# ---------------------------------------------------------------------------
# EvidenceChain frozen model
# ---------------------------------------------------------------------------

class TestEvidenceChainFrozen:
    def test_evidence_chain_is_frozen(self):
        from ollarma.evidence import build_receipt_chain
        rows = [_make_result_dict()]
        chain = build_receipt_chain(rows, run_id="TEST")
        with pytest.raises(Exception):
            chain.evidence_root = "tampered"


# ---------------------------------------------------------------------------
# ModelSelectionArtifact helpers
# ---------------------------------------------------------------------------

def _make_stats():
    """Create test ModelSuiteStats fixtures for artifact tests."""
    from ollarma.reporter import ModelSuiteStats
    return [
        ModelSuiteStats(model="qwen3:8b", suite="science", quality_mean=0.9,
                        quality_std=0.05, decode_tps_mean=45.0, decode_tps_std=2.0,
                        prefill_tps_mean=100.0, prefill_tps_std=5.0, ttft_ms=10.0,
                        trial_count=3, composite_score=0.85),
        ModelSuiteStats(model="phi4-mini", suite="science", quality_mean=0.7,
                        quality_std=0.1, decode_tps_mean=80.0, decode_tps_std=3.0,
                        prefill_tps_mean=150.0, prefill_tps_std=8.0, ttft_ms=6.7,
                        trial_count=3, composite_score=0.72),
        ModelSuiteStats(model="qwen3:8b", suite="code", quality_mean=0.8,
                        quality_std=0.05, decode_tps_mean=42.0, decode_tps_std=1.5,
                        prefill_tps_mean=95.0, prefill_tps_std=4.0, ttft_ms=10.5,
                        trial_count=3, composite_score=0.78),
    ]


# ---------------------------------------------------------------------------
# EVID-02: ModelSelectionArtifact
# ---------------------------------------------------------------------------

class TestModelSelectionArtifact:
    def test_artifact_is_frozen(self):
        from ollarma.evidence import ModelSelectionArtifact
        artifact = ModelSelectionArtifact(
            run_id="TEST", evidence_root="abc123",
            quality_weight_default=0.7, speed_weight_default=0.3,
            quality_weight_routing=0.4, speed_weight_routing=0.6,
            per_suite_winners={"science": "qwen3:8b"},
            pareto_frontier=["qwen3:8b"],
            stable_decision_hash="deadbeef",
        )
        with pytest.raises(Exception):
            artifact.run_id = "TAMPERED"

    def test_artifact_has_all_required_fields(self):
        from ollarma.evidence import ModelSelectionArtifact
        artifact = ModelSelectionArtifact(
            run_id="TEST", evidence_root="abc123",
            quality_weight_default=0.7, speed_weight_default=0.3,
            quality_weight_routing=0.4, speed_weight_routing=0.6,
            per_suite_winners={"science": "qwen3:8b"},
            pareto_frontier=["qwen3:8b"],
            stable_decision_hash="deadbeef",
        )
        assert artifact.run_id == "TEST"
        assert artifact.evidence_root == "abc123"
        assert artifact.quality_weight_default == 0.7
        assert artifact.speed_weight_default == 0.3
        assert artifact.quality_weight_routing == 0.4
        assert artifact.speed_weight_routing == 0.6
        assert artifact.per_suite_winners == {"science": "qwen3:8b"}
        assert artifact.pareto_frontier == ["qwen3:8b"]
        assert artifact.stable_decision_hash == "deadbeef"

    def test_stable_decision_hash_is_sha256_of_all_fields_except_itself(self):
        from ollarma.evidence import ModelSelectionArtifact, canonical_hash
        artifact_data = {
            "run_id": "TEST",
            "evidence_root": "abc123",
            "quality_weight_default": 0.7,
            "speed_weight_default": 0.3,
            "quality_weight_routing": 0.4,
            "speed_weight_routing": 0.6,
            "per_suite_winners": {"science": "qwen3:8b"},
            "pareto_frontier": ["qwen3:8b"],
        }
        expected_hash = canonical_hash(artifact_data)
        artifact = ModelSelectionArtifact(
            **artifact_data,
            stable_decision_hash=expected_hash,
        )
        assert artifact.stable_decision_hash == expected_hash


class TestBuildSelectionArtifact:
    def test_returns_artifact_with_correct_evidence_root(self):
        from ollarma.evidence import build_selection_artifact
        stats = _make_stats()
        artifact = build_selection_artifact(
            run_id="TEST", evidence_root="root123", stats=stats,
        )
        assert artifact.evidence_root == "root123"

    def test_per_suite_winners_correct(self):
        from ollarma.evidence import build_selection_artifact
        stats = _make_stats()
        artifact = build_selection_artifact(
            run_id="TEST", evidence_root="root123", stats=stats,
        )
        # build_selection_artifact recomputes composites from quality + normalized
        # TPS (aggregate_trials leaves composite_score=0.0), so the artifact agrees
        # with render_selection_md instead of using the order-dependent default.
        # science: phi4-mini (q=0.7 @ 80 tps -> composite 0.79) beats qwen3:8b
        # (q=0.9 @ 45 tps -> 0.63); both clear the quality floor (0.5).
        assert artifact.per_suite_winners["science"] == "phi4-mini"
        # code: qwen3:8b is the only model.
        assert artifact.per_suite_winners["code"] == "qwen3:8b"

    def test_pareto_frontier_populated_and_sorted(self):
        from ollarma.evidence import build_selection_artifact
        stats = _make_stats()
        artifact = build_selection_artifact(
            run_id="TEST", evidence_root="root123", stats=stats,
        )
        # Pareto frontier: non-dominated models sorted alphabetically
        assert isinstance(artifact.pareto_frontier, list)
        assert len(artifact.pareto_frontier) > 0
        assert artifact.pareto_frontier == sorted(artifact.pareto_frontier)

    def test_changing_evidence_root_changes_decision_hash(self):
        from ollarma.evidence import build_selection_artifact
        stats = _make_stats()
        a1 = build_selection_artifact(run_id="T", evidence_root="root_A", stats=stats)
        a2 = build_selection_artifact(run_id="T", evidence_root="root_B", stats=stats)
        assert a1.stable_decision_hash != a2.stable_decision_hash

    def test_changing_per_suite_winners_changes_decision_hash(self):
        from ollarma.evidence import build_selection_artifact
        from ollarma.reporter import ModelSuiteStats
        stats1 = _make_stats()
        # Create stats where phi4-mini wins science (higher composite)
        stats2 = [
            ModelSuiteStats(model="qwen3:8b", suite="science", quality_mean=0.5,
                            quality_std=0.05, decode_tps_mean=45.0, decode_tps_std=2.0,
                            prefill_tps_mean=100.0, prefill_tps_std=5.0, ttft_ms=10.0,
                            trial_count=3, composite_score=0.45),
            ModelSuiteStats(model="phi4-mini", suite="science", quality_mean=0.9,
                            quality_std=0.1, decode_tps_mean=80.0, decode_tps_std=3.0,
                            prefill_tps_mean=150.0, prefill_tps_std=8.0, ttft_ms=6.7,
                            trial_count=3, composite_score=0.92),
            ModelSuiteStats(model="qwen3:8b", suite="code", quality_mean=0.8,
                            quality_std=0.05, decode_tps_mean=42.0, decode_tps_std=1.5,
                            prefill_tps_mean=95.0, prefill_tps_std=4.0, ttft_ms=10.5,
                            trial_count=3, composite_score=0.78),
        ]
        a1 = build_selection_artifact(run_id="T", evidence_root="same", stats=stats1)
        a2 = build_selection_artifact(run_id="T", evidence_root="same", stats=stats2)
        assert a1.stable_decision_hash != a2.stable_decision_hash

    def test_identical_inputs_produce_identical_hash(self):
        from ollarma.evidence import build_selection_artifact
        stats = _make_stats()
        a1 = build_selection_artifact(run_id="T", evidence_root="root", stats=stats)
        a2 = build_selection_artifact(run_id="T", evidence_root="root", stats=stats)
        assert a1.stable_decision_hash == a2.stable_decision_hash


class TestWriteArtifactFile:
    def test_writes_valid_json_with_all_fields(self, tmp_path):
        from ollarma.evidence import build_selection_artifact, write_artifact_file
        stats = _make_stats()
        artifact = build_selection_artifact(
            run_id="TEST", evidence_root="root123", stats=stats,
        )
        path = write_artifact_file(artifact, tmp_path, "TEST")
        data = orjson.loads(path.read_bytes())
        assert "run_id" in data
        assert "evidence_root" in data
        assert "quality_weight_default" in data
        assert "speed_weight_default" in data
        assert "quality_weight_routing" in data
        assert "speed_weight_routing" in data
        assert "per_suite_winners" in data
        assert "pareto_frontier" in data
        assert "stable_decision_hash" in data

    def test_writes_correct_filename(self, tmp_path):
        from ollarma.evidence import build_selection_artifact, write_artifact_file
        stats = _make_stats()
        artifact = build_selection_artifact(
            run_id="TEST", evidence_root="root123", stats=stats,
        )
        path = write_artifact_file(artifact, tmp_path, "TEST")
        assert path.name == "run-TEST.artifact.json"


# ---------------------------------------------------------------------------
# EVID-03: bench verify CLI command
# ---------------------------------------------------------------------------

def _write_sealed_and_evidence(tmp_path, run_id="TEST"):
    """Helper: create sealed JSON + evidence JSON in tmp_path/results/."""
    from ollarma.evidence import build_receipt_chain, write_evidence_file

    results_dir = tmp_path / "results"
    results_dir.mkdir()

    rows = [
        _make_result_dict(task_id="t1"),
        _make_result_dict(task_id="t2"),
    ]
    sealed_path = results_dir / f"run-{run_id}.json"
    sealed_path.write_bytes(orjson.dumps(rows))

    chain = build_receipt_chain(rows, run_id=run_id)
    write_evidence_file(chain, results_dir, run_id)

    return rows, results_dir


class TestBenchVerifyCLI:
    def test_verify_valid_chain_exits_0(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from ollarma.cli import app

        _write_sealed_and_evidence(tmp_path, run_id="VALID")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["verify", "VALID"])
        assert result.exit_code == 0
        assert "Evidence chain valid" in result.output

    def test_verify_tampered_row_exits_1(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from ollarma.cli import app

        rows, results_dir = _write_sealed_and_evidence(tmp_path, run_id="TAMPER")
        monkeypatch.chdir(tmp_path)

        # Tamper with sealed JSON -- modify a row
        sealed_path = results_dir / "run-TAMPER.json"
        tampered_rows = orjson.loads(sealed_path.read_bytes())
        tampered_rows[0]["raw_response"] = "TAMPERED DATA"
        sealed_path.write_bytes(orjson.dumps(tampered_rows))

        runner = CliRunner()
        result = runner.invoke(app, ["verify", "TAMPER"])
        assert result.exit_code == 1
        assert "VERIFICATION FAILED" in result.output

    def test_verify_missing_sealed_exits_1(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from ollarma.cli import app

        # Create results dir but no sealed file
        (tmp_path / "results").mkdir()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["verify", "MISSING"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_verify_generates_evidence_if_missing(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from ollarma.cli import app

        results_dir = tmp_path / "results"
        results_dir.mkdir()

        # Write sealed JSON only (no evidence file)
        rows = [_make_result_dict(task_id="t1"), _make_result_dict(task_id="t2")]
        sealed_path = results_dir / "run-NOEVIDENC.json"
        sealed_path.write_bytes(orjson.dumps(rows))

        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["verify", "NOEVIDENC"])
        assert result.exit_code == 0
        assert "Evidence chain valid" in result.output
        assert (results_dir / "run-NOEVIDENC.evidence.json").exists()


def test_build_selection_artifact_applies_and_records_quality_floor():
    """#6: the authoritative artifact must apply the quality floor (a quality-0
    fast model cannot bind a tier) and record the floor for provenance."""
    from ollarma.evidence import build_selection_artifact
    from ollarma.reporter import MIN_QUALITY_FLOOR, ModelSuiteStats

    def mk(model, suite, q, tps, comp):
        return ModelSuiteStats(
            model=model, suite=suite, quality_mean=q, quality_std=0.0,
            decode_tps_mean=tps, decode_tps_std=0.0, prefill_tps_mean=None,
            prefill_tps_std=0.0, ttft_ms=None, trial_count=3, composite_score=comp,
        )

    # Quality-0 model has the higher swarm composite but must NOT win.
    stats = [
        mk("qwen2.5:1.5b", "swarm", 0.0, 125.0, 0.60),
        mk("granite4.1:8b", "swarm", 0.95, 36.0, 0.50),
    ]
    art = build_selection_artifact("2026-06-09T00:00:00Z", "e" * 64, stats)

    assert art.per_suite_winners["swarm"] == "granite4.1:8b"
    assert art.min_quality_floor == MIN_QUALITY_FLOOR
    assert art.schema_version == "3"

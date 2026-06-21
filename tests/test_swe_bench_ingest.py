"""test_swe_bench_ingest.py -- SWE-01 substrate: dataset fetch + load.

Tests use a monkeypatched ``_download_bytes`` seam; no network I/O.
"""
from __future__ import annotations

import hashlib
import pathlib

import pytest

from ollarma import swe_bench


FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "swe_bench" / "minimal_subset.jsonl"


def _install_fake_downloader(monkeypatch, payload: bytes) -> None:
    """Route ``_download_bytes`` to a fixture payload."""
    monkeypatch.setattr(swe_bench, "_download_bytes", lambda *_a, **_kw: payload)


def test_fetch_creates_cache_dir_and_records_sha(tmp_path, monkeypatch):
    payload = FIXTURE_PATH.read_bytes()
    _install_fake_downloader(monkeypatch, payload)

    jsonl = swe_bench.fetch_dataset(tmp_path)

    expected_sha = hashlib.sha256(payload).hexdigest()
    sha_path = tmp_path / ".ollarma" / "benchmarks" / "swe-bench-lite" / "problems.sha256"
    assert jsonl.exists()
    assert jsonl.read_bytes() == payload
    assert sha_path.read_text().strip() == expected_sha


def test_fetch_idempotent_when_cache_valid(tmp_path, monkeypatch):
    payload = FIXTURE_PATH.read_bytes()
    call_count = {"n": 0}

    def _counting_downloader(*_a, **_kw):
        call_count["n"] += 1
        return payload

    monkeypatch.setattr(swe_bench, "_download_bytes", _counting_downloader)

    swe_bench.fetch_dataset(tmp_path)
    swe_bench.fetch_dataset(tmp_path)  # cache hit

    assert call_count["n"] == 1


def test_load_problems_parses_minimal_subset(tmp_path, monkeypatch):
    payload = FIXTURE_PATH.read_bytes()
    _install_fake_downloader(monkeypatch, payload)

    swe_bench.fetch_dataset(tmp_path)
    problems = swe_bench.load_problems(tmp_path)

    assert len(problems) == 3
    ids = [p.instance_id for p in problems]
    assert ids == [
        "synthetic-pass-1",
        "synthetic-fail-2",
        "synthetic-timeout-3",
    ]
    assert problems[0].test_cmd.startswith("python3 -c")
    # Model is frozen.
    with pytest.raises((TypeError, ValueError)):
        problems[0].instance_id = "mutated"

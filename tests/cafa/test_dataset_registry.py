"""Tests for D1 dataset registry — local, hashed, fail-closed bootstrap."""
import json
from pathlib import Path

import pytest

from ollarma.cafa.dataset_registry import (
    register,
    sha256_file,
    bootstrap_go_basic,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mini_go.obo"


def test_sha256_is_deterministic():
    a, n1 = sha256_file(FIXTURE)
    b, n2 = sha256_file(FIXTURE)
    assert a == b and n1 == n2 and n1 > 0


def test_register_appends_provenance_row(tmp_path):
    manifest = tmp_path / "source_intake.manifest.jsonl"
    rec = register("go_basic", FIXTURE, manifest=manifest)
    assert rec.sha256.startswith("sha256:")
    assert rec.kind == "ontology"
    assert rec.source_url and "go-basic.obo" in rec.source_url
    rows = manifest.read_text().strip().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["name"] == "go_basic"
    assert row["sha256"] == rec.sha256
    assert row["claim_ceiling"] == "MEASURED"


def test_register_missing_file_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        register("cafa_train", tmp_path / "nope.tsv", manifest=tmp_path / "m.jsonl")


def test_bootstrap_refuses_network_without_explicit_optin(tmp_path):
    # fail closed: no allow_download => no network, raises
    with pytest.raises(PermissionError):
        bootstrap_go_basic(tmp_path / "go-basic.obo")


def test_bootstrap_noop_if_present(tmp_path):
    dest = tmp_path / "go-basic.obo"
    dest.write_text("[Term]\nid: GO:0008150\n")
    # already present -> returns path, no network needed even without opt-in
    assert bootstrap_go_basic(dest) == dest

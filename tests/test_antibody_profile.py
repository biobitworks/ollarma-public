"""RTB-02 project antibody profile tests."""
from __future__ import annotations

import pathlib
import textwrap

import pytest

from ollarma.antibody_profile import (
    AntibodyLaneVerdict,
    AntibodyProfile,
    authority_status_for,
    detect_antibody_pack_gaps,
    resolve_antibody_lanes,
)
from ollarma.fleet import AdapterConfig, load_fleet_registry


def _adapter(**overrides) -> AdapterConfig:
    data = {
        "project_name": "deltaprot",
        "project_root": "/tmp/deltaprot",
    }
    data.update(overrides)
    return AdapterConfig(**data)


def test_antibody_profile_accepts_empty_profile() -> None:
    profile = AntibodyProfile(owner_project="deltaprot")

    assert profile.version == 1
    assert profile.default_antibodies == ()
    assert profile.imports == ()


def test_adapter_config_preserves_typed_antibody_profile_from_yaml(tmp_path: pathlib.Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "deltaprot.yaml").write_text(
        textwrap.dedent(
            """\
            project_name: deltaprot
            project_root: /tmp/deltaprot
            antibodies:
              - legacy_methodology
            antibody_profile:
              owner_project: deltaprot
              version: 1
              default_antibodies:
                - deltaprot_methodology
              antigen_banks:
                - key: deltaprot_bad_claim_patterns
                  digest: sha256:bank
                  embedding_model: nomic-embed-text
                  calibration_status: open
            """
        ),
        encoding="utf-8",
    )

    registry = load_fleet_registry(str(tmp_path))
    profile = registry["deltaprot"].antibody_profile

    assert profile is not None
    assert profile.owner_project == "deltaprot"
    assert profile.default_antibodies == ("deltaprot_methodology",)
    assert profile.antigen_banks[0].embedding_model == "nomic-embed-text"


def test_flat_antibodies_map_to_project_owned_and_prompt_injection_core() -> None:
    adapter = _adapter(antibodies=["methodology", "prompt_injection"])

    lanes = resolve_antibody_lanes(adapter)

    assert [lane.antibody_key for lane in lanes].count("prompt_injection") == 1
    assert ("methodology", "project_owned") in {
        (lane.antibody_key, lane.lane_class) for lane in lanes
    }


def test_valid_import_resolves_borrowed_shared_with_provenance() -> None:
    source = AntibodyProfile(
        owner_project="Antigence",
        exports=(
            {
                "key": "citation",
                "digest": "sha256:citation",
                "allowed_uses": ("chat", "workflow"),
                "claim_ceiling": "advisory",
            },
        ),
    )
    profile = AntibodyProfile(
        owner_project="deltaprot",
        imports=(
            {
                "key": "citation",
                "source_project": "Antigence",
                "required_digest": "sha256:citation",
                "allowed_uses": ("chat",),
            },
        ),
    )

    lanes = resolve_antibody_lanes(
        _adapter(antibody_profile=profile),
        source_profiles={"Antigence": source},
        allowed_use="chat",
    )

    borrowed = next(lane for lane in lanes if lane.antibody_key == "citation")
    assert borrowed.lane_class == "borrowed_shared"
    assert borrowed.source_project == "Antigence"
    assert borrowed.source_digest == "sha256:citation"
    assert borrowed.claim_ceiling == "advisory"


def test_import_fails_when_source_export_missing() -> None:
    profile = AntibodyProfile(
        owner_project="deltaprot",
        imports=(
            {
                "key": "citation",
                "source_project": "Antigence",
                "required_digest": "sha256:citation",
                "allowed_uses": ("chat",),
            },
        ),
    )

    with pytest.raises(ValueError, match="ANTIBODY_EXPORT_MISSING"):
        resolve_antibody_lanes(
            _adapter(antibody_profile=profile),
            source_profiles={"Antigence": AntibodyProfile(owner_project="Antigence")},
        )


def test_import_fails_when_digest_mismatches() -> None:
    source = AntibodyProfile(
        owner_project="Antigence",
        exports=({"key": "citation", "digest": "sha256:other", "allowed_uses": ("chat",)},),
    )
    profile = AntibodyProfile(
        owner_project="deltaprot",
        imports=(
            {
                "key": "citation",
                "source_project": "Antigence",
                "required_digest": "sha256:citation",
                "allowed_uses": ("chat",),
            },
        ),
    )

    with pytest.raises(ValueError, match="ANTIBODY_EXPORT_DIGEST_MISMATCH"):
        resolve_antibody_lanes(_adapter(antibody_profile=profile), source_profiles={"Antigence": source})


def test_import_fails_when_allowed_use_is_forbidden() -> None:
    source = AntibodyProfile(
        owner_project="Antigence",
        exports=({"key": "citation", "digest": "sha256:citation", "allowed_uses": ("workflow",)},),
    )
    profile = AntibodyProfile(
        owner_project="deltaprot",
        imports=(
            {
                "key": "citation",
                "source_project": "Antigence",
                "required_digest": "sha256:citation",
                "allowed_uses": ("workflow",),
            },
        ),
    )

    with pytest.raises(ValueError, match="ANTIBODY_IMPORT_USE_FORBIDDEN"):
        resolve_antibody_lanes(
            _adapter(antibody_profile=profile),
            source_profiles={"Antigence": source},
            allowed_use="chat",
        )


def test_unknown_operator_requested_antibody_is_rejected() -> None:
    adapter = _adapter(antibodies=["methodology"])

    with pytest.raises(ValueError, match="ANTIBODY_UNKNOWN_REQUESTED"):
        resolve_antibody_lanes(adapter, requested_keys=("freeform_unknown",))


def test_antigen_bank_lane_records_digest_embedding_and_calibration() -> None:
    profile = AntibodyProfile(
        owner_project="deltaprot",
        default_antibodies=("deltaprot_bad_claim_patterns",),
        antigen_banks=(
            {
                "key": "deltaprot_bad_claim_patterns",
                "digest": "sha256:bank",
                "embedding_model": "nomic-embed-text",
                "threshold": 0.7,
                "calibration_status": "open",
            },
        ),
    )

    lanes = resolve_antibody_lanes(_adapter(antibody_profile=profile))
    bank = next(lane for lane in lanes if lane.antibody_key == "deltaprot_bad_claim_patterns")

    assert bank.head_type == "antigen_bank"
    assert bank.source_digest == "sha256:bank"
    assert bank.embedding_model == "nomic-embed-text"
    assert bank.threshold == 0.7
    assert bank.calibration_status == "open"


def test_open_calibration_block_is_advisory_not_trusted() -> None:
    lane = resolve_antibody_lanes(
        _adapter(
            antibody_profile=AntibodyProfile(
                owner_project="deltaprot",
                default_antibodies=("bank",),
                antigen_banks=(
                    {
                        "key": "bank",
                        "digest": "sha256:bank",
                        "embedding_model": "nomic-embed-text",
                        "calibration_status": "open",
                    },
                ),
            )
        )
    )[1]

    assert authority_status_for(lane, "block") == "advisory"


def test_conflicting_lane_verdicts_are_represented_without_aggregation() -> None:
    lanes = resolve_antibody_lanes(_adapter(antibodies=["methodology", "logic"]))
    verdicts = (
        AntibodyLaneVerdict(
            lane=lanes[1],
            verdict_status="pass",
            authority_status=authority_status_for(lanes[1], "pass"),
            input_hash="in1",
            output_hash="out1",
        ),
        AntibodyLaneVerdict(
            lane=lanes[2],
            verdict_status="flag",
            authority_status=authority_status_for(lanes[2], "flag"),
            input_hash="in2",
            output_hash="out2",
        ),
    )

    assert [item.verdict_status for item in verdicts] == ["pass", "flag"]
    assert verdicts[0].lane.antibody_key != verdicts[1].lane.antibody_key


def test_review_mode_can_exclude_borrowed_shared_from_chat() -> None:
    source = AntibodyProfile(
        owner_project="Antigence",
        exports=({"key": "citation", "digest": "sha256:citation", "allowed_uses": ("chat",)},),
    )
    profile = AntibodyProfile(
        owner_project="deltaprot",
        default_antibodies=("methodology",),
        imports=(
            {
                "key": "citation",
                "source_project": "Antigence",
                "required_digest": "sha256:citation",
                "allowed_uses": ("chat",),
            },
        ),
        review_modes={"chat": ("core", "project_owned"), "workflow": ("core", "project_owned", "borrowed_shared")},
    )

    chat = resolve_antibody_lanes(_adapter(antibody_profile=profile), source_profiles={"Antigence": source}, review_mode="chat")
    workflow = resolve_antibody_lanes(
        _adapter(antibody_profile=profile),
        source_profiles={"Antigence": source},
        review_mode="workflow",
    )

    assert "citation" not in {lane.antibody_key for lane in chat}
    assert "citation" in {lane.antibody_key for lane in workflow}


def test_empty_project_antibody_manifest_reports_pack_gap() -> None:
    gaps = detect_antibody_pack_gaps(_adapter())

    assert len(gaps) == 1
    assert gaps[0].reason_code == "EMPTY_ANTIBODY_MANIFEST"

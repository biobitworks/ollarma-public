"""Project-owned antibody profile schemas and lane resolver.

RTB-02 makes antibody packs explicit without changing existing adapters. The
flat ``AdapterConfig.antibodies`` list remains a shorthand for project-owned
lanes; the typed ``antibody_profile`` block adds ownership, export/import
provenance, antigen-bank metadata, and model-role policy.
"""
from __future__ import annotations

from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from ollarma.antibody_model_policy import AntibodyModelPolicy

if TYPE_CHECKING:  # pragma: no cover
    from ollarma.fleet import AdapterConfig


CORE_ANTIBODY = "prompt_injection"

ClaimCeiling = Literal["advisory", "blocking", "escalation_only"]
CalibrationStatus = Literal["open", "closed", "unknown"]
LaneClass = Literal["core", "project_owned", "borrowed_shared", "operator_requested"]
HeadType = Literal["rule_floor", "antigen_bank", "tiny_cell", "model_judge"]
AuthorityStatus = Literal["trusted", "advisory", "degraded", "escalation_only"]


class AntibodyExport(BaseModel):
    key: str
    digest: str
    allowed_uses: tuple[str, ...] = ()
    claim_ceiling: ClaimCeiling = "advisory"

    model_config = ConfigDict(frozen=True)


class AntibodyImport(BaseModel):
    key: str
    source_project: str
    required_digest: str
    allowed_uses: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class AntigenBankConfig(BaseModel):
    key: str
    digest: str
    embedding_model: str
    threshold: float | None = None
    calibration_status: CalibrationStatus = "unknown"

    model_config = ConfigDict(frozen=True)


class AntibodyLaneSelection(BaseModel):
    antibody_key: str
    lane_class: LaneClass
    head_type: HeadType = "rule_floor"
    owner_project: str
    source_project: str | None = None
    source_digest: str | None = None
    embedding_model: str | None = None
    threshold: float | None = None
    calibration_status: CalibrationStatus = "unknown"
    allowed_uses: tuple[str, ...] = ()
    claim_ceiling: str = "advisory"

    model_config = ConfigDict(frozen=True)


class AntibodyProfile(BaseModel):
    owner_project: str
    version: int = 1
    default_antibodies: tuple[str, ...] = ()
    antigen_banks: tuple[AntigenBankConfig, ...] = ()
    exports: tuple[AntibodyExport, ...] = ()
    imports: tuple[AntibodyImport, ...] = ()
    review_modes: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    model_policy: tuple[AntibodyModelPolicy, ...] = ()

    model_config = ConfigDict(frozen=True)


class AntibodyPackGap(BaseModel):
    project: str
    reason_code: str
    detail: str

    model_config = ConfigDict(frozen=True)


class AntibodyLaneVerdict(BaseModel):
    """Lossless per-lane verdict metadata for later review packets."""

    lane: AntibodyLaneSelection
    verdict_status: Literal["pass", "flag", "block", "reject", "escalate"]
    authority_status: AuthorityStatus
    input_hash: str
    output_hash: str

    model_config = ConfigDict(frozen=True)


def _dedupe_lanes(lanes: list[AntibodyLaneSelection]) -> tuple[AntibodyLaneSelection, ...]:
    seen: set[tuple[str, str, str | None]] = set()
    result: list[AntibodyLaneSelection] = []
    for lane in lanes:
        key = (lane.antibody_key, lane.lane_class, lane.source_project)
        if key in seen:
            continue
        seen.add(key)
        result.append(lane)
    return tuple(result)


def _review_lane_classes(profile: AntibodyProfile | None, review_mode: str) -> set[str]:
    if profile is None:
        return {"core", "project_owned"}
    configured = profile.review_modes.get(review_mode)
    if configured is None:
        return {"core", "project_owned", "borrowed_shared", "operator_requested"}
    return set(configured)


def _source_profile_for(
    import_decl: AntibodyImport,
    source_profiles: dict[str, AntibodyProfile],
) -> AntibodyProfile:
    source = source_profiles.get(import_decl.source_project)
    if source is None:
        raise ValueError(f"ANTIBODY_IMPORT_SOURCE_MISSING:{import_decl.source_project}")
    return source


def _resolve_import(
    import_decl: AntibodyImport,
    *,
    source_profiles: dict[str, AntibodyProfile],
    allowed_use: str,
    owner_project: str,
) -> AntibodyLaneSelection:
    source = _source_profile_for(import_decl, source_profiles)
    export = next((item for item in source.exports if item.key == import_decl.key), None)
    if export is None:
        raise ValueError(f"ANTIBODY_EXPORT_MISSING:{import_decl.source_project}:{import_decl.key}")
    if export.digest != import_decl.required_digest:
        raise ValueError(f"ANTIBODY_EXPORT_DIGEST_MISMATCH:{import_decl.key}")
    if allowed_use not in export.allowed_uses or allowed_use not in import_decl.allowed_uses:
        raise ValueError(f"ANTIBODY_IMPORT_USE_FORBIDDEN:{import_decl.key}:{allowed_use}")
    return AntibodyLaneSelection(
        antibody_key=import_decl.key,
        lane_class="borrowed_shared",
        owner_project=owner_project,
        source_project=source.owner_project,
        source_digest=export.digest,
        allowed_uses=import_decl.allowed_uses,
        claim_ceiling=export.claim_ceiling,
    )


def _project_lane(
    key: str,
    *,
    owner_project: str,
    antigen_banks: dict[str, AntigenBankConfig],
    lane_class: LaneClass = "project_owned",
) -> AntibodyLaneSelection:
    bank = antigen_banks.get(key)
    if bank is not None:
        return AntibodyLaneSelection(
            antibody_key=key,
            lane_class=lane_class,
            head_type="antigen_bank",
            owner_project=owner_project,
            source_digest=bank.digest,
            embedding_model=bank.embedding_model,
            threshold=bank.threshold,
            calibration_status=bank.calibration_status,
            claim_ceiling="advisory" if bank.calibration_status != "closed" else "blocking",
        )
    return AntibodyLaneSelection(
        antibody_key=key,
        lane_class=lane_class,
        owner_project=owner_project,
    )


def resolve_antibody_lanes(
    adapter: "AdapterConfig",
    *,
    source_profiles: dict[str, AntibodyProfile] | None = None,
    requested_keys: tuple[str, ...] = (),
    review_mode: str = "chat",
    allowed_use: str = "chat",
    include_borrowed: bool = True,
) -> tuple[AntibodyLaneSelection, ...]:
    """Resolve deterministic antibody lanes for a project adapter."""

    profile = adapter.antibody_profile
    lane_classes = _review_lane_classes(profile, review_mode)
    owner_project = profile.owner_project if profile else adapter.project_name
    lanes: list[AntibodyLaneSelection] = []

    if "core" in lane_classes:
        lanes.append(
            AntibodyLaneSelection(
                antibody_key=CORE_ANTIBODY,
                lane_class="core",
                owner_project=owner_project,
            )
        )

    antigen_banks = {bank.key: bank for bank in (profile.antigen_banks if profile else ())}
    project_keys = tuple(profile.default_antibodies if profile else adapter.antibodies)
    if "project_owned" in lane_classes:
        for key in project_keys:
            if key == CORE_ANTIBODY:
                continue
            lanes.append(_project_lane(key, owner_project=owner_project, antigen_banks=antigen_banks))

    imported_by_key: dict[str, AntibodyLaneSelection] = {}
    if profile and include_borrowed and "borrowed_shared" in lane_classes:
        if profile.imports and source_profiles is None:
            raise ValueError("ANTIBODY_IMPORT_SOURCE_PROFILES_REQUIRED")
        for import_decl in profile.imports:
            lane = _resolve_import(
                import_decl,
                source_profiles=source_profiles or {},
                allowed_use=allowed_use,
                owner_project=owner_project,
            )
            lanes.append(lane)
            imported_by_key[lane.antibody_key] = lane

    available_project = {lane.antibody_key: lane for lane in lanes if lane.lane_class in {"core", "project_owned"}}
    for key in requested_keys:
        if key in available_project:
            lane = available_project[key]
            if lane.lane_class == "core":
                continue
            lanes.append(lane.model_copy(update={"lane_class": "operator_requested"}))
        elif key in imported_by_key:
            lanes.append(imported_by_key[key].model_copy(update={"lane_class": "operator_requested"}))
        else:
            raise ValueError(f"ANTIBODY_UNKNOWN_REQUESTED:{key}")

    return _dedupe_lanes(lanes)


def authority_status_for(lane: AntibodyLaneSelection, verdict_status: str) -> AuthorityStatus:
    """Return the authority posture for a lane/verdict pair."""

    if lane.claim_ceiling == "escalation_only":
        return "escalation_only"
    if lane.calibration_status in {"open", "unknown"} and verdict_status in {"block", "reject"}:
        return "advisory"
    if lane.claim_ceiling == "advisory" and verdict_status in {"block", "reject"}:
        return "advisory"
    return "trusted"


def detect_antibody_pack_gaps(adapter: "AdapterConfig") -> tuple[AntibodyPackGap, ...]:
    """Surface empty project antibody manifests as coverage gaps."""

    if adapter.antibody_profile is None and not adapter.antibodies:
        return (
            AntibodyPackGap(
                project=adapter.project_name,
                reason_code="EMPTY_ANTIBODY_MANIFEST",
                detail="adapter has no antibody_profile and antibodies is empty",
            ),
        )
    if adapter.antibody_profile is not None and not adapter.antibody_profile.default_antibodies:
        return (
            AntibodyPackGap(
                project=adapter.project_name,
                reason_code="EMPTY_ANTIBODY_PROFILE_DEFAULTS",
                detail="antibody_profile.default_antibodies is empty",
            ),
        )
    return ()


def profile_from_raw(raw: Any) -> AntibodyProfile | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, AntibodyProfile):
        return raw
    return AntibodyProfile.model_validate(raw)

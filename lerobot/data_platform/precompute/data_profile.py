"""Portable robot semantics and signal-layout metadata for local datasets."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from lerobot.data_platform.precompute.timeseries import (
    DATA_VERSION_DVT1,
    DATA_VERSION_DVT2,
    feature_vector_dim,
    infer_data_version_from_features,
)

DATA_PROFILE_FILENAME = "data_profile.json"
DATA_PROFILE_SCHEMA_VERSION = 2
DATA_PROFILE_PROTOCOL = 3
DEFAULT_PROCESSING_DATA_VERSION = DATA_VERSION_DVT2
ROBOT_PROFILE_DVT1 = "h10w_dvt1"
ROBOT_PROFILE_DVT2 = "h10w_dvt2"
STAGE_PROFILE_DVT1 = "h10w_dvt1_stage_v1"
STAGE_PROFILE_DVT2 = "h10w_dvt2_stage_v1"
STAGE_PROFILE_EQUAL_TIME = "time_equal_v1"
SIGNAL_SCHEMA_STANDARD_16D = "dual_arm_standard_16d"
# Backward-compatible import name for callers written before the Data Platform/Curation scope split.
SIGNAL_SCHEMA_TRAIN_16D = SIGNAL_SCHEMA_STANDARD_16D
_LEGACY_SIGNAL_SCHEMA_TRAIN_16D = "dual_arm_train_16d"


@dataclass(frozen=True)
class DatasetDataProfile:
    schema_version: int
    robot_profile: str
    signal_schema: str
    gripper_encoding: str
    stage_profile: str
    legacy_data_version: str | None
    resolution_source: str
    confirmed: bool

    def to_dict(self) -> dict:
        return asdict(self)

    def for_signal_schema(
        self,
        signal_schema: str,
        *,
        gripper_encoding: str | None = None,
        resolution_source: str | None = None,
    ) -> DatasetDataProfile:
        return replace(
            self,
            signal_schema=signal_schema,
            gripper_encoding=gripper_encoding or self.gripper_encoding,
            resolution_source=resolution_source or self.resolution_source,
        )


def signal_schema_from_features(features: dict | None) -> str:
    features = features or {}
    action_dim = feature_vector_dim(features.get("action"))
    state_dim = feature_vector_dim(features.get("state"))
    max_dim = max(action_dim, state_dim)
    if action_dim == 16 and state_dim == 16:
        return SIGNAL_SCHEMA_STANDARD_16D
    if max_dim:
        return f"action_{action_dim}d_state_{state_dim}d"
    return "unknown"


def has_body_joint_dimensions(features: dict | None) -> bool:
    """Return whether the stored signal layout contains DVT2 body joints 16..18."""
    features = features or {}
    return (
        max(
            feature_vector_dim(features.get("action")),
            feature_vector_dim(features.get("state")),
        )
        >= 18
    )


def has_legacy_flag_dimension(features: dict | None) -> bool:
    features = features or {}
    max_dim = max(
        feature_vector_dim(features.get("action")),
        feature_vector_dim(features.get("state")),
    )
    return max_dim == 17


def profile_from_data_version(
    data_version: str,
    features: dict | None,
    *,
    resolution_source: str,
    confirmed: bool,
) -> DatasetDataProfile:
    normalized = str(data_version).upper()
    if normalized not in {DATA_VERSION_DVT1, DATA_VERSION_DVT2}:
        raise ValueError(f"Unsupported data_version: {data_version}")
    is_dvt2 = normalized == DATA_VERSION_DVT2
    return DatasetDataProfile(
        schema_version=DATA_PROFILE_SCHEMA_VERSION,
        robot_profile=ROBOT_PROFILE_DVT2 if is_dvt2 else ROBOT_PROFILE_DVT1,
        signal_schema=signal_schema_from_features(features),
        gripper_encoding="auto_detect" if is_dvt2 else "legacy",
        stage_profile=STAGE_PROFILE_DVT2 if is_dvt2 else STAGE_PROFILE_DVT1,
        legacy_data_version=normalized,
        resolution_source=resolution_source,
        confirmed=bool(confirmed),
    )


def _profile_from_dict(payload: dict, source: str) -> DatasetDataProfile:
    schema_version = int(payload.get("schema_version") or 1)
    if schema_version not in {1, DATA_PROFILE_SCHEMA_VERSION}:
        raise ValueError(f"Unsupported data profile schema version in {source}: {schema_version}")
    data_version = str(payload.get("legacy_data_version") or "").upper()
    if data_version not in {"", DATA_VERSION_DVT1, DATA_VERSION_DVT2}:
        raise ValueError(f"Invalid data profile legacy_data_version in {source}")
    signal_schema = str(payload.get("signal_schema") or "unknown")
    if signal_schema == _LEGACY_SIGNAL_SCHEMA_TRAIN_16D:
        signal_schema = SIGNAL_SCHEMA_STANDARD_16D
    return DatasetDataProfile(
        schema_version=schema_version,
        robot_profile=str(payload.get("robot_profile") or ""),
        signal_schema=signal_schema,
        gripper_encoding=str(payload.get("gripper_encoding") or "unknown"),
        stage_profile=str(payload.get("stage_profile") or ""),
        legacy_data_version=data_version or None,
        resolution_source=str(payload.get("resolution_source") or source),
        confirmed=bool(payload.get("confirmed", False)),
    )


def resolve_data_profile(
    root: Path,
    features: dict | None = None,
    *,
    data_version_override: str | None = None,
    default_data_version: str | None = None,
) -> DatasetDataProfile:
    """Resolve semantic profile without treating vector dimensions as authoritative identity."""
    root = Path(root).expanduser()
    try:
        info = json.loads((root / "meta" / "info.json").read_text())
    except (OSError, json.JSONDecodeError):
        info = {}
    if features is None:
        features = info.get("features") or {}
    if str(info.get("robot_type") or "").lower() == "umi":
        if data_version_override:
            raise ValueError("UMI data cannot use a DVT processing profile")
        return profile_from_info({**info, "features": features})

    if data_version_override:
        return profile_from_data_version(
            data_version_override,
            features,
            resolution_source="explicit_override",
            confirmed=True,
        )

    profile_path = root / "meta" / DATA_PROFILE_FILENAME
    if profile_path.is_file():
        try:
            payload = json.loads(profile_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid data profile: {profile_path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid data profile: {profile_path}")
        return _profile_from_dict(payload, str(profile_path))

    if not info:
        try:
            info = json.loads((root / "meta" / "info.json").read_text())
        except (OSError, json.JSONDecodeError):
            info = {}
    embedded = info.get("data_profile")
    if isinstance(embedded, dict):
        return _profile_from_dict(embedded, "meta/info.json")

    standardize_path = root / "meta" / "preprocess_standardize.json"
    if standardize_path.is_file():
        try:
            standardize = json.loads(standardize_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid standardize provenance: {standardize_path}") from exc
        source_version = str(standardize.get("source_data_version") or "").upper()
        if source_version in {DATA_VERSION_DVT1, DATA_VERSION_DVT2}:
            profile = profile_from_data_version(
                source_version,
                features,
                resolution_source="standardize_provenance",
                confirmed=True,
            )
            return profile.for_signal_schema(
                SIGNAL_SCHEMA_STANDARD_16D,
                gripper_encoding="normalized_0_1" if source_version == DATA_VERSION_DVT2 else "legacy",
            )

    if default_data_version and _has_legacy_signals(features):
        return profile_from_data_version(
            default_data_version,
            features,
            resolution_source="operation_default",
            confirmed=True,
        )
    return profile_from_info({**info, "features": features})


def _has_legacy_signals(features: dict) -> bool:
    return any(feature_vector_dim(features.get(key)) >= 16 for key in ("action", "state"))


def profile_from_info(info: dict) -> DatasetDataProfile:
    """Resolve advertised metadata too, without requiring access to remote source files."""
    features = info.get("features") or {}
    robot = str(info.get("robot_type") or "").strip().lower()
    if robot == "umi":
        required = {f"{part}_pose": 6 for part in ("head", "left_arm", "right_arm")}
        required.update({f"{part}_quaternion_pose": 7 for part in ("head", "left_arm", "right_arm")})
        required.update(left_gripper_pos=1, right_gripper_pos=1)
        recognized = all(feature_vector_dim(features.get(key)) == dim for key, dim in required.items())
        return DatasetDataProfile(
            DATA_PROFILE_SCHEMA_VERSION,
            "umi",
            "umi_dual_hand_head_v1" if recognized else "unknown",
            "unknown",
            STAGE_PROFILE_EQUAL_TIME if recognized else "",
            None,
            "robot_type_and_features",
            recognized,
        )
    embedded = info.get("data_profile")
    if isinstance(embedded, dict):
        return _profile_from_dict(embedded, "data_profile")
    if info.get("data_version") in {DATA_VERSION_DVT1, DATA_VERSION_DVT2}:
        return profile_from_data_version(
            info["data_version"], features, resolution_source="legacy_metadata", confirmed=False
        )
    if robot in {"h10w", "dvt1", "dvt2"} or (not robot and _has_legacy_signals(features)):
        return profile_from_data_version(
            infer_data_version_from_features(features),
            features,
            resolution_source="dimension_inference",
            confirmed=False,
        )
    return DatasetDataProfile(
        DATA_PROFILE_SCHEMA_VERSION,
        "unknown",
        signal_schema_from_features(features),
        "unknown",
        "",
        None,
        "unrecognized",
        False,
    )


@dataclass(frozen=True)
class OperationCapability:
    available: bool
    reason: str | None = None


def default_processing_profile(profile: DatasetDataProfile, features: dict) -> DatasetDataProfile:
    """Choose operation defaults independently of the source dataset's recorded identity."""
    if profile.legacy_data_version is None:
        return profile
    return profile_from_data_version(
        DEFAULT_PROCESSING_DATA_VERSION, features, resolution_source="operation_default", confirmed=True
    )


def resolve_processing_profile(
    root: Path, features: dict | None = None, *, data_version_override: str | None = None
) -> DatasetDataProfile:
    profile = resolve_data_profile(root, features, data_version_override=data_version_override)
    if data_version_override:
        return profile
    if features is None:
        features = json.loads((Path(root) / "meta/info.json").read_text()).get("features") or {}
    return default_processing_profile(profile, features)


def operation_capabilities(profile: DatasetDataProfile, features: dict) -> dict[str, dict]:
    legacy = profile.legacy_data_version is not None
    reason = "Requires a compatible DVT action/state processing profile"
    result = {
        op: asdict(OperationCapability(legacy, None if legacy else reason))
        for op in (
            "standardize",
            "convert_action",
            "smooth_action",
            "value_edit",
            "quality_flags",
            "flag_fixes",
            "auto_stage",
            "embedding",
            "stage_return_alignment",
            "stage_return_height_alignment",
        )
    }
    for op in ("viewer.prepare", "analysis", "split", "merge", "convert_v3", "clear_flags"):
        result[op] = asdict(OperationCapability(True))
    if profile.stage_profile == STAGE_PROFILE_EQUAL_TIME:
        result["auto_stage"] = asdict(OperationCapability(True))
    # Other mutating operations have not been validated for native UMI signals yet.
    for op in ("drop_field", "subtract"):
        result[op] = asdict(
            OperationCapability(legacy, None if legacy else "Not supported for this signal layout")
        )
    if profile.robot_profile == "umi" and profile.signal_schema == "unknown":
        for op in ("split", "merge"):
            result[op] = asdict(OperationCapability(False, "Unrecognized UMI signal layout"))
    return result


def dataset_semantics(info: dict, profile: DatasetDataProfile | None = None) -> dict:
    profile = profile or profile_from_info(info)
    return {
        "robot_type": info.get("robot_type") or "unknown",
        "codebase_version": info.get("codebase_version"),
        "data_profile": profile.to_dict(),
        "default_processing_profile": default_processing_profile(
            profile, info.get("features") or {}
        ).to_dict(),
        "data_version": profile.legacy_data_version,
        "robot_profile": profile.robot_profile,
        "signal_schema": profile.signal_schema,
        "stage_profile": profile.stage_profile,
        "profile_confirmed": profile.confirmed,
        "data_profile_protocol": DATA_PROFILE_PROTOCOL,
        "operation_capabilities": operation_capabilities(profile, info.get("features") or {}),
    }


def required_data_profile_protocol(info: dict) -> int:
    """Old Agents omit robot_type; unclassified signal layouts require a fresh discovery report."""
    profile = profile_from_info(info)
    if profile.robot_profile == "umi" or (info.get("features") and profile.legacy_data_version is None):
        return DATA_PROFILE_PROTOCOL
    return 0


def require_operation(info: dict, operation: str, *, profile: DatasetDataProfile | None = None) -> None:
    profile = profile or profile_from_info(info)
    op = operation.removeprefix("preprocess.")
    capability = operation_capabilities(profile, info.get("features") or {}).get(op)
    if capability is not None and not capability["available"]:
        raise ValueError(f"{op}: {capability['reason']}")


def require_dataset_operation(
    root: Path, operation: str, *, data_version_override: str | None = None
) -> None:
    root = Path(root)
    info = json.loads((root / "meta/info.json").read_text())
    require_operation(
        info, operation, profile=resolve_data_profile(root, data_version_override=data_version_override)
    )


def write_data_profile(root: Path, profile: DatasetDataProfile, *, info: dict | None = None) -> Path:
    root = Path(root)
    meta_dir = root / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    payload = profile.to_dict()
    path = meta_dir / DATA_PROFILE_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    if info is not None:
        info["data_profile"] = payload
    return path

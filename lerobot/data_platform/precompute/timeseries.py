from __future__ import annotations

import math

import numpy as np

DATA_VERSION_DVT1 = "DVT1"
DATA_VERSION_DVT2 = "DVT2"
GRIPPER_NORMALIZE_INDICES = (7, 15)
BODY_JOINT_INDICES = (16, 17, 18)
UMI_GRIPPER_COLUMNS = {"left_gripper_pos", "right_gripper_pos"}
UMI_GRIPPER_SCALE = 100.0
GRIPPER_NORMALIZE_COLUMNS = {
    f"{column}_{idx}" for column in ("action", "state") for idx in GRIPPER_NORMALIZE_INDICES
}
UMI_GRIPPER_NORMALIZE_COLUMNS = set(UMI_GRIPPER_COLUMNS)


def feature_vector_dim(feature: dict | object | None) -> int:
    shape = (feature.get("shape") or []) if isinstance(feature, dict) else getattr(feature, "shape", [])
    if isinstance(shape, int):
        return int(shape)
    if isinstance(shape, (list, tuple)) and shape:
        try:
            return int(shape[0])
        except (TypeError, ValueError):
            return 0
    return 0


def infer_data_version_from_features(features: dict | None) -> str:
    features = features or {}
    action_dim = feature_vector_dim(features.get("action"))
    state_dim = feature_vector_dim(features.get("state"))
    return DATA_VERSION_DVT2 if max(action_dim, state_dim) >= 18 else DATA_VERSION_DVT1


def normalize_gripper_columns(
    values: np.ndarray, column_name: str, data_version: str = DATA_VERSION_DVT1
) -> np.ndarray:
    """Normalize gripper-like columns for plotting."""
    array = np.asarray(values, dtype=np.float64)
    normalized_data_version = str(data_version or "").upper()

    if normalized_data_version == DATA_VERSION_DVT2 and column_name in {"action", "state"}:
        if array.ndim != 2:
            return array

        normalized = array.copy()
        for idx in GRIPPER_NORMALIZE_INDICES:
            if idx >= normalized.shape[1]:
                continue
            column = normalized[:, idx]
            finite = column[np.isfinite(column)]
            if finite.size == 0:
                continue
            max_abs = np.max(np.abs(finite))
            if max_abs > 1.5:
                normalized[:, idx] = column / 100.0
        return normalized

    if normalized_data_version != DATA_VERSION_DVT2 and column_name in UMI_GRIPPER_COLUMNS:
        # These native scalar signals use raw 0..100 values, including values below 1.5.
        return array / UMI_GRIPPER_SCALE
    return array


def normalize_gripper_csv_value(header: str, value: str, data_version: str = DATA_VERSION_DVT1) -> str:
    """Normalize one gripper cell in a cached CSV to plotting convention."""
    normalized_data_version = str(data_version or "").upper()
    if normalized_data_version == DATA_VERSION_DVT2:
        if header.strip() not in GRIPPER_NORMALIZE_COLUMNS:
            return value
        scale = 100.0
    elif header.strip() not in UMI_GRIPPER_NORMALIZE_COLUMNS:
        return value
    else:
        scale = UMI_GRIPPER_SCALE

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(numeric):
        return value
    if normalized_data_version != DATA_VERSION_DVT2 or abs(numeric) > 1.5:
        return f"{numeric / scale:.12g}"
    return value

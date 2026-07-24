from __future__ import annotations

import math

import numpy as np


DATA_VERSION_DVT1 = "DVT1"
DATA_VERSION_DVT2 = "DVT2"
GRIPPER_NORMALIZE_INDICES = (7, 15)
BODY_JOINT_INDICES = (16, 17, 18)
GRIPPER_NORMALIZE_COLUMNS = {
    f"{column}_{idx}" for column in ("action", "state") for idx in GRIPPER_NORMALIZE_INDICES
}


def feature_vector_dim(feature: dict | object | None) -> int:
    if isinstance(feature, dict):
        shape = feature.get("shape") or []
    else:
        shape = getattr(feature, "shape", [])
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


def normalize_gripper_columns(values: np.ndarray, column_name: str, data_version: str = DATA_VERSION_DVT1) -> np.ndarray:
    """Normalize DVT2 gripper columns from 0-100 to the 0-1 plotting convention."""
    array = np.asarray(values, dtype=np.float64)
    if data_version.upper() != DATA_VERSION_DVT2 or column_name not in {"action", "state"} or array.ndim != 2:
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


def normalize_gripper_csv_value(header: str, value: str, data_version: str = DATA_VERSION_DVT1) -> str:
    """Normalize one DVT2 gripper cell in a cached CSV when it uses the 0-100 scale."""
    if data_version.upper() != DATA_VERSION_DVT2 or header.strip() not in GRIPPER_NORMALIZE_COLUMNS:
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(numeric):
        return value
    if abs(numeric) > 1.5:
        return f"{numeric / 100.0:.12g}"
    return value

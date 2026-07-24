from __future__ import annotations

from io import BytesIO

import numpy as np
import pyarrow as pa
from PIL import Image, ImageDraw


_X_NAMES = {"x", "pos_x", "position_x", "ee_x", "eef_x", "tcp_x", "gripper_x", "cartesian_x"}
_Y_NAMES = {"y", "pos_y", "position_y", "ee_y", "eef_y", "tcp_y", "gripper_y", "cartesian_y"}
_IMAGE_HINTS = ("image", "pixel", "uv", "screen")
_H10W_LINK_LENGTHS = np.asarray([0.24, 0.20, 0.16, 0.11], dtype=np.float32)
_H10W_WORKSPACE_BOUNDS = [-1.1, 1.1, -0.85, 0.85]


def _column_to_2d(column: pa.ChunkedArray) -> np.ndarray:
    rows = []
    for value in column.to_pylist():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            try:
                rows.append([float(v) for v in value])
            except (TypeError, ValueError):
                continue
        else:
            try:
                rows.append([float(value)])
            except (TypeError, ValueError):
                continue
    if not rows:
        return np.empty((0, 0), dtype=np.float32)
    width = max(len(row) for row in rows)
    padded = [row + [0.0] * (width - len(row)) for row in rows]
    return np.asarray(padded, dtype=np.float32)


def _flatten_names(names) -> list[str]:
    if names is None:
        return []
    if isinstance(names, str):
        return [names]
    if isinstance(names, dict):
        out = []
        for value in names.values():
            out.extend(_flatten_names(value))
        return out
    if isinstance(names, (list, tuple)):
        out = []
        for value in names:
            out.extend(_flatten_names(value))
        return out
    return [str(names)]


def _tokens(name: str) -> set[str]:
    lowered = name.lower().replace(".", "_").replace("/", "_").replace("-", "_")
    parts = {part for part in lowered.split("_") if part}
    parts.add(lowered)
    return parts


def _axis_from_name(name: str) -> str | None:
    tokens = _tokens(name)
    if tokens & _X_NAMES:
        return "x"
    if tokens & _Y_NAMES:
        return "y"
    return None


def _is_image_source(source: str) -> bool:
    lowered = source.lower()
    return any(hint in lowered for hint in _IMAGE_HINTS)


def _find_xy_indices(names: list[str]) -> tuple[int, int] | None:
    if len(names) < 2:
        return None
    x_idx = y_idx = None
    for idx, name in enumerate(names):
        axis = _axis_from_name(str(name))
        if axis == "x" and x_idx is None:
            x_idx = idx
        elif axis == "y" and y_idx is None:
            y_idx = idx
    if x_idx is None or y_idx is None:
        return None
    return x_idx, y_idx


def _feature_names(features: dict | None, key: str) -> list[str]:
    feature = (features or {}).get(key) or {}
    return _flatten_names(feature.get("names"))


def _vector_xy_array(table, features: dict | None) -> tuple[np.ndarray, str, bool] | None:
    preferred = [
        "observation.ee_pose",
        "observation.eef_pose",
        "observation.tcp_pose",
        "ee_pose",
        "eef_pose",
        "tcp_pose",
        "observation.state",
        "state",
        "action",
    ]
    keys = [key for key in preferred if key in table.column_names]
    keys.extend(key for key in table.column_names if key not in keys)
    for key in keys:
        names = _feature_names(features, key)
        arr = _column_to_2d(table[key])
        indices = _find_xy_indices(names)
        if indices is None or arr.shape[1] <= max(indices):
            continue
        x_idx, y_idx = indices
        source = f"{key}[{names[x_idx]},{names[y_idx]}]"
        return arr[:, [x_idx, y_idx]], source, _is_image_source(source)
    return None


def _scalar_xy_array(table) -> tuple[np.ndarray, str, bool] | None:
    by_prefix: dict[str, dict[str, str]] = {}
    for key in table.column_names:
        axis = _axis_from_name(key)
        if axis is None:
            continue
        lowered = key.lower()
        suffixes = (f"_{axis}", f".{axis}", f"/{axis}", f"-{axis}")
        prefix = lowered
        for suffix in suffixes:
            if lowered.endswith(suffix):
                prefix = lowered[: -len(suffix)]
                break
        by_prefix.setdefault(prefix, {})[axis] = key
    for prefix, axes in by_prefix.items():
        if "x" not in axes or "y" not in axes:
            continue
        x = np.asarray(table[axes["x"]].to_pylist(), dtype=np.float32)
        y = np.asarray(table[axes["y"]].to_pylist(), dtype=np.float32)
        if x.size == 0 or y.size == 0:
            continue
        source = f"{axes['x']},{axes['y']}"
        return np.stack([x, y], axis=1), source, _is_image_source(source)
    return None


def _xy_array(table, features: dict | None = None) -> tuple[np.ndarray, str, bool]:
    result = _scalar_xy_array(table) or _vector_xy_array(table, features)
    if result is None:
        return np.empty((0, 0), dtype=np.float32), "", False
    return result


def _best_vector_column(table, keys: list[str]) -> tuple[str, np.ndarray] | None:
    for key in keys:
        if key not in table.column_names:
            continue
        arr = _column_to_2d(table[key])
        if arr.ndim == 2 and arr.shape[0] > 0 and arr.shape[1] >= 16:
            return key, arr
    return None


def _h10w_arm_blocks(arr: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    if arr.shape[1] < 16:
        return {}
    return {
        "left": (arr[:, 0:7], arr[:, 7]),
        "right": (arr[:, 8:15], arr[:, 15]),
    }


def _active_h10w_arm(blocks: dict[str, tuple[np.ndarray, np.ndarray]]) -> str:
    scores = {}
    for arm, (joints, gripper) in blocks.items():
        joint_score = float(np.nan_to_num(np.std(joints, axis=0)).sum())
        gripper_score = float(np.nan_to_num(np.std(gripper))) * 2.0
        scores[arm] = joint_score + gripper_score
    if not scores:
        return "left"
    return max(scores, key=scores.get)


def _h10w_planar_fk(joints: np.ndarray, arm: str) -> np.ndarray:
    q = np.asarray(joints[:, :4], dtype=np.float32)
    angles = np.cumsum(q, axis=1)
    side = -1.0 if arm == "left" else 1.0
    lateral = np.cos(angles) @ _H10W_LINK_LENGTHS
    y = np.sin(angles) @ _H10W_LINK_LENGTHS
    base_x = -0.32 if arm == "left" else 0.32
    return np.stack([base_x + side * lateral, y], axis=1).astype(np.float32)


def _gripper_event_index(gripper: np.ndarray, points: np.ndarray) -> int:
    gripper = np.asarray(gripper, dtype=np.float32)
    if gripper.shape[0] > 1:
        changes = np.abs(np.diff(gripper))
        if float(np.nanmax(changes)) > 1e-4:
            return int(np.nanargmax(changes)) + 1
    if points.shape[0] > 1:
        motion = np.linalg.norm(np.diff(points[:, :2], axis=0), axis=1)
        return int(np.nanargmax(motion)) + 1
    return 0


def _h10w_projection(table, robot_type: str | None = None) -> dict | None:
    robot_type = (robot_type or "").lower()
    vector = _best_vector_column(table, ["state", "observation.state", "action", "observation.action"])
    if vector is None:
        return None
    key, arr = vector
    if "h10" not in robot_type and arr.shape[1] not in (16, 17):
        return None
    blocks = _h10w_arm_blocks(arr)
    if not blocks:
        return None
    arm = _active_h10w_arm(blocks)
    joints, gripper = blocks[arm]
    points = _h10w_planar_fk(joints, arm)
    grasp_idx = _gripper_event_index(gripper, points)
    return {
        "points": points,
        "source": f"h10w {key} {arm} arm planar FK projection",
        "projection": "workspace",
        "active_arm": arm,
        "gripper_source": f"{key}[{7 if arm == 'left' else 15}]",
        "grasp_index": grasp_idx,
        "grasp_point": points[grasp_idx].tolist() if points.shape[0] else None,
    }


def _details_from_points(
    points: np.ndarray,
    source: str,
    projection: str,
    max_points: int,
    active_arm: str | None = None,
    gripper_source: str | None = None,
    grasp_index: int | None = None,
    grasp_point: list[float] | None = None,
    bounds: list[float] | None = None,
) -> dict:
    if points.ndim != 2 or points.shape[0] == 0 or points.shape[1] < 2:
        return {
            "points": [],
            "source": "",
            "projection": "unavailable",
            "reason": "No explicit x/y coordinate columns or feature dimension names were found.",
            "active_arm": None,
            "gripper_source": None,
            "grasp_index": None,
            "grasp_point": None,
            "bounds": None,
        }
    xy = points[:, :2]
    original_grasp_index = grasp_index
    if xy.shape[0] > max_points:
        indices = np.linspace(0, xy.shape[0] - 1, max_points).astype(int)
        if grasp_index is not None:
            grasp_index = int(np.argmin(np.abs(indices - grasp_index)))
        xy = xy[indices]
    out_points = [[round(float(x), 6), round(float(y), 6)] for x, y in xy]
    if grasp_point is None and original_grasp_index is not None and 0 <= original_grasp_index < points.shape[0]:
        grasp_point = points[original_grasp_index].tolist()
    return {
        "points": out_points,
        "source": source,
        "projection": projection,
        "reason": "",
        "active_arm": active_arm,
        "gripper_source": gripper_source,
        "grasp_index": grasp_index,
        "grasp_point": [round(float(grasp_point[0]), 6), round(float(grasp_point[1]), 6)] if grasp_point else None,
        "bounds": bounds,
    }


def grasp_xy_from_trajectory(table, features: dict | None = None, robot_type: str | None = None) -> list[float] | None:
    details = trajectory_xy_details_from_table(table, features, robot_type=robot_type)
    grasp_point = details.get("grasp_point")
    if grasp_point is not None:
        return grasp_point
    arr = np.asarray(details.get("points") or [], dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] < 2:
        return None
    if arr.shape[0] > 1:
        motion = np.linalg.norm(np.diff(arr[:, :2], axis=0), axis=1)
        idx = int(np.argmax(motion)) + 1
    else:
        idx = 0
    return [round(float(arr[idx, 0]), 6), round(float(arr[idx, 1]), 6)]


def trajectory_xy_details_from_table(
    table,
    features: dict | None = None,
    max_points: int = 160,
    robot_type: str | None = None,
) -> dict:
    arr, source, is_image = _xy_array(table, features)
    if arr.ndim == 2 and arr.shape[0] > 0 and arr.shape[1] >= 2:
        return _details_from_points(arr, source, "image" if is_image else "xy", max_points)
    h10w = _h10w_projection(table, robot_type)
    if h10w is not None:
        return _details_from_points(
            h10w["points"],
            h10w["source"],
            h10w["projection"],
            max_points,
            active_arm=h10w["active_arm"],
            gripper_source=h10w["gripper_source"],
            grasp_index=h10w["grasp_index"],
            grasp_point=h10w["grasp_point"],
            bounds=_H10W_WORKSPACE_BOUNDS,
        )
    return _details_from_points(np.empty((0, 0), dtype=np.float32), "", "unavailable", max_points)


def trajectory_xy_from_table(
    table,
    features: dict | None = None,
    max_points: int = 160,
    robot_type: str | None = None,
) -> list[list[float]]:
    return trajectory_xy_details_from_table(table, features, max_points, robot_type)["points"]


def build_grasp_heatmap(points: list[list[float]], size: int = 240) -> bytes:
    img = Image.new("RGB", (size, size), "#020617")
    draw = ImageDraw.Draw(img, "RGBA")
    valid = np.asarray([point for point in points if point is not None and len(point) >= 2], dtype=np.float32)
    if valid.size:
        mins = valid.min(axis=0)
        maxs = valid.max(axis=0)
        span = np.maximum(maxs - mins, 1e-6)
        coords = (valid - mins) / span
        coords[:, 1] = 1.0 - coords[:, 1]
        for x, y in coords:
            px = int(np.clip(x, 0, 1) * (size - 1))
            py = int(np.clip(y, 0, 1) * (size - 1))
            draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=(56, 189, 248, 120))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

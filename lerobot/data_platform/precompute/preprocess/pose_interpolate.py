"""Reviewed pose interpolation with independent outputs and episode-scoped shard writes.

Algorithm reference: data_laundry pose-jump-interpolate, commit
6ba72ff6ca4ccb143eeeb15bcafd3047aac14845. Dataset IO and publication use platform contracts.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.data_platform.precompute.dataset_io import load_episode_records
from lerobot.data_platform.precompute.preprocess.common import (
    PreprocessResult,
    default_preprocess_path,
    emit,
    ensure_output_root,
    load_json,
    validate_dataset_root,
)
from lerobot.data_platform.precompute.preprocess.value_edit import _finish_stats, _update_stats, _write_stats

SOURCE_COMMIT = "6ba72ff6ca4ccb143eeeb15bcafd3047aac14845"


@dataclass(frozen=True)
class PoseRepair:
    episode_index: int
    field: str
    start_frame: int
    end_frame: int
    position_indices: tuple[int, ...]
    quaternion_indices: tuple[int, ...]

    @classmethod
    def parse(cls, value: dict, features: dict) -> PoseRepair:
        allowed = {
            "episode_index",
            "field",
            "start_frame",
            "end_frame",
            "position_indices",
            "quaternion_indices",
            "quaternion_order",
            "position_unit",
            "episode_uid",
        }
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Invalid pose repair")
        for key in ("episode_index", "start_frame", "end_frame"):
            if type(value.get(key)) is not int or value[key] < 0:
                raise ValueError(f"{key} must be a non-negative integer")
        if value["start_frame"] < 1 or value["end_frame"] <= value["start_frame"]:
            raise ValueError("Repair intervals are half-open and require a preceding anchor")
        field = value.get("field")
        feature = features.get(field, {})
        if not str(feature.get("dtype", "")).startswith("float") or len(feature.get("shape", [])) != 1:
            raise ValueError("Select a declared floating point pose vector")
        size = feature["shape"][0]
        names = feature.get("names", [])
        if isinstance(names, list) and {"x", "y", "z", "qx", "qy", "qz", "qw"} <= set(names):
            position = tuple(names.index(name) for name in ("x", "y", "z"))
            quaternion = tuple(names.index(name) for name in ("qx", "qy", "qz", "qw"))
        else:
            position = tuple(value.get("position_indices", ()))
            quaternion = tuple(value.get("quaternion_indices", ()))
            order = value.get("quaternion_order")
            if order not in {"xyzw", "wxyz"}:
                raise ValueError("Packed pose fields require an explicit quaternion order and index mapping")
            if order == "wxyz" and len(quaternion) == 4:
                quaternion = (*quaternion[1:], quaternion[0])
        if (
            len(position) != 3
            or len(quaternion) != 4
            or len({*position, *quaternion}) != 7
            or any(type(index) is not int or not 0 <= index < size for index in (*position, *quaternion))
        ):
            raise ValueError(
                "Pose indices must describe three position and four distinct quaternion dimensions"
            )
        unit = value.get("position_unit") or feature.get("position_unit") or feature.get("unit")
        if unit not in {"m", "cm", "mm"}:
            raise ValueError("Pose position units must be explicitly declared")
        return cls(
            value["episode_index"], field, value["start_frame"], value["end_frame"], position, quaternion
        )


def _paths(root, info):
    pattern = info.get("data_path", "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet")
    import re

    pattern = re.sub(r"\{[^}]+\}", "*", pattern)
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("Dataset data paths must stay inside the dataset")
    return sorted(root.glob(pattern))


def _episode(root, paths, episode, fields):
    pieces = []
    for path in paths:
        parquet = pq.ParquetFile(path)
        required = {"episode_index", "frame_index", "timestamp", *fields}
        if not required <= set(parquet.schema_arrow.names):
            raise ValueError("Pose repair requires timestamps, indices and declared pose fields")
        table = pq.read_table(path, columns=sorted(required), filters=[("episode_index", "=", episode)])
        if table.num_rows:
            pieces.append(table)
    if not pieces:
        raise ValueError(f"Episode {episode} is unavailable")
    table = pa.concat_tables(pieces).sort_by("frame_index")
    if table["frame_index"].to_pylist() != list(range(table.num_rows)):
        raise ValueError("Pose repair requires complete, contiguous frame indices")
    timestamps = np.asarray(table["timestamp"].to_pylist(), dtype=float).reshape(-1)
    if not np.isfinite(timestamps).all() or np.any(np.diff(timestamps) <= 0):
        raise ValueError("Pose repair requires finite, increasing timestamps")
    return table, timestamps


def _slerp(left, right, weights):
    norms = np.linalg.norm([left, right], axis=1)
    if not np.isfinite(norms).all() or np.any(norms < 1e-12):
        raise ValueError("Quaternion anchors must be finite and nonzero")
    left, right = left / norms[0], right / norms[1]
    dot = float(np.dot(left, right))
    if dot < 0:
        right, dot = -right, -dot
    angle = np.arccos(np.clip(dot, -1, 1))
    if angle < 1e-6:
        result = (1 - weights[:, None]) * left + weights[:, None] * right
    else:
        result = (
            np.sin((1 - weights[:, None]) * angle) * left + np.sin(weights[:, None] * angle) * right
        ) / np.sin(angle)
    return result / np.linalg.norm(result, axis=1)[:, None]


def apply_pose_repairs(root: Path, repairs: list[dict], progress_callback=None, *, dry_run=False) -> dict:
    root = Path(root)
    info = load_json(root / "meta/info.json")
    if not isinstance(repairs, list) or not repairs or len(repairs) > 10000:
        raise ValueError("Specify 1–10000 explicitly reviewed pose intervals")
    parsed = [PoseRepair.parse(value, info.get("features", {})) for value in repairs]
    paths = _paths(root, info)
    replacements, intervals = {}, {}
    episodes = {int(row["episode_index"]): row for row in load_episode_records(root)}
    for repair in parsed:
        key = (repair.episode_index, repair.field)
        if key not in replacements:
            table, times = _episode(root, paths, repair.episode_index, [repair.field])
            if table.num_rows != episodes.get(repair.episode_index, {}).get("length"):
                raise ValueError("Episode row count does not match metadata")
            replacements[key] = (np.asarray(table[repair.field].to_pylist(), dtype=float), times)
        values, times = replacements[key]
        start, end = repair.start_frame, repair.end_frame
        if end >= len(values) or any(start < hi and end > lo for lo, hi in intervals.get(key, [])):
            raise ValueError("Repair intervals must be disjoint and have two valid anchors")
        intervals.setdefault(key, []).append((start, end))
        dims = [*repair.position_indices, *repair.quaternion_indices]
        if not np.isfinite(values[[start - 1, end]][:, dims]).all():
            raise ValueError("Repair anchors must contain finite poses")
        weights = (times[start:end] - times[start - 1]) / (times[end] - times[start - 1])
        pos = list(repair.position_indices)
        quat = list(repair.quaternion_indices)
        values[start:end, pos] = (1 - weights[:, None]) * values[start - 1, pos] + weights[:, None] * values[
            end, pos
        ]
        values[start:end, quat] = _slerp(values[start - 1, quat], values[end, quat], weights)
        emit(progress_callback, message=f"Prepared pose repair for episode {repair.episode_index}")
    if not dry_run:
        for path in paths:
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Pose writes cannot follow data symlinks outside the output dataset")
            table = pq.read_table(path)
            ids = table["episode_index"].to_pylist()
            frames = table["frame_index"].to_pylist()
            changed = False
            for (episode, field), (values, _) in replacements.items():
                positions = [i for i, value in enumerate(ids) if value == episode]
                if not positions:
                    continue
                column = table[field].to_pylist()
                for index in positions:
                    column[index] = values[int(frames[index])].tolist()
                table = table.set_column(
                    table.column_names.index(field),
                    table.schema.field(field),
                    pa.array(column, type=table.schema.field(field).type),
                )
                changed = True
            if changed:
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                pq.write_table(table, temporary)
                temporary.replace(path)
        # Recompute every episode for changed fields, preserving complete global statistics.
        stats = {}
        for field in {item.field for item in parsed}:
            for episode in episodes:
                table, _ = _episode(root, paths, episode, [field])
                accumulator = {}
                _update_stats(accumulator, np.asarray(table[field].to_pylist(), dtype=float))
                stats.setdefault(field, {})[episode] = _finish_stats(accumulator)
        _write_stats(root, stats)
    return {
        "repairs": len(parsed),
        "modified_episodes": sorted({item.episode_index for item in parsed}),
        "algorithm": "linear-position+slerp",
        "algorithm_version": "1.0.0",
        "source_commit": SOURCE_COMMIT,
    }


def run_pose_interpolate(
    src_root: Path,
    *,
    repairs: list[dict],
    out_root: Path | None = None,
    dry_run: bool = False,
    progress_callback=None,
) -> PreprocessResult:
    src_root = validate_dataset_root(src_root).resolve()
    target = Path(out_root or default_preprocess_path(src_root, "pose_interpolate")).resolve()
    if target == src_root or target.is_relative_to(src_root) or src_root.is_relative_to(target):
        raise ValueError("Pose repairs require an independent sibling output")
    ensure_output_root(target, dry_run=dry_run)
    info = load_json(src_root / "meta/info.json")
    result = PreprocessResult(
        "pose_interpolate",
        [src_root],
        target,
        f"local/{target.name}",
        info.get("total_episodes", 0),
        info.get("total_frames", 0),
        dry_run,
    )
    if dry_run:
        result.summary = apply_pose_repairs(src_root, repairs, progress_callback, dry_run=True)
        return result
    stage = target.with_name(f".{target.name}.building-{uuid.uuid4().hex}")
    try:
        shutil.copytree(src_root, stage, symlinks=True)
        if not (stage / "meta").resolve().is_relative_to(stage):
            raise ValueError("Dataset metadata cannot be a symlink outside the output")
        if any(
            path.is_symlink() and not path.resolve().is_relative_to(stage)
            for path in (stage / "meta").rglob("*")
        ):
            raise ValueError("Dataset metadata cannot link outside the output")
        result.summary = apply_pose_repairs(stage, repairs, progress_callback)
        from lerobot.data_platform.lifecycle import validate_materialized_dataset

        validate_materialized_dataset(stage, expected_episode_count=int(info["total_episodes"]))
        result.episode_lineage = [
            {
                "source_root": str(src_root),
                "source_episode_index": int(row["episode_index"]),
                "output_episode_index": int(row["episode_index"]),
            }
            for row in load_episode_records(stage)
        ]
        stage.rename(target)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return result

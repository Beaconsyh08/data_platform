"""Read-only end-effector checks with a selected-dataset motion baseline."""

from __future__ import annotations

import contextlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.lib.stride_tricks import sliding_window_view


@dataclass(frozen=True)
class EefQualityConfig:
    quantile: float = 0.99
    local_ratio: float = 8.0
    window: int = 31
    quaternion_tolerance: float = 0.001
    strategy: str = "dataset_quantile"
    position_unit: str | None = None
    linear_speed: float = 0.5
    angular_speed: float = 1.2
    spike_ratio: float = 3.0
    angular_spike_ratio: float = 5.0

    @classmethod
    def parse(cls, value: dict) -> EefQualityConfig:
        from lerobot.data_platform.precompute.quality_signals import finite_number

        if not isinstance(value, dict) or set(value) - {
            "quantile",
            "local_ratio",
            "window",
            "quaternion_tolerance",
            "strategy",
            "position_unit",
            "linear_speed",
            "angular_speed",
            "spike_ratio",
            "angular_spike_ratio",
        }:
            raise ValueError("Unknown end-effector configuration")
        quantile = finite_number(value.get("quantile", 0.99), "eef.quantile", minimum=0.5)
        ratio = finite_number(value.get("local_ratio", 8), "eef.local_ratio", minimum=1)
        tolerance = finite_number(
            value.get("quaternion_tolerance", 0.001), "eef.quaternion_tolerance", minimum=0
        )
        window = value.get("window", 31)
        if quantile >= 1 or type(window) is not int or not 3 <= window <= 301 or window % 2 != 1:
            raise ValueError("EEF requires quantile < 1 and an odd window between 3 and 301")
        strategy = value.get("strategy", "dataset_quantile")
        unit = value.get("position_unit")
        if strategy not in {"dataset_quantile", "laundry_spike"} or unit not in {None, "m", "cm", "mm"}:
            raise ValueError("Invalid EEF strategy or position unit")
        speeds = [
            finite_number(value.get(key, default), f"eef.{key}", minimum=1e-12)
            for key, default in (
                ("linear_speed", 0.5),
                ("angular_speed", 1.2),
                ("spike_ratio", 3),
                ("angular_spike_ratio", 5),
            )
        ]
        return cls(quantile, ratio, window, tolerance, strategy, unit, *speeds)


def pose_features(features: dict) -> dict:
    required = {"x", "y", "z", "qx", "qy", "qz", "qw"}
    return {
        key: feature
        for key, feature in features.items()
        if key.endswith("_quaternion_pose")
        and feature.get("dtype", "").startswith("float")
        and feature.get("shape") in ([7], (7,))
        and isinstance(feature.get("names"), (list, tuple))
        and len(feature["names"]) == 7
        and set(feature["names"]) == required
    }


def _arrays(table, features):
    n = table.num_rows
    timestamps = np.full(n, np.nan)
    if "timestamp" in table.column_names:
        for i, value in enumerate(table["timestamp"].to_pylist()):
            with contextlib.suppress(ValueError, TypeError):
                timestamps[i] = np.asarray(value, dtype=float).reshape(1)[0]
    poses = {}
    for key, feature in features.items():
        values = np.full((n, 7), np.nan)
        if key in table.column_names:
            for i, row in enumerate(table[key].to_pylist()):
                with contextlib.suppress(ValueError, TypeError):
                    values[i] = np.asarray(row, dtype=float).reshape(7)
        names = feature["names"]
        poses[key] = values[:, [names.index(name) for name in ("x", "y", "z", "qx", "qy", "qz", "qw")]]
    return timestamps, poses


def motion_steps(values, timestamps, max_dt=None):
    dt = np.diff(timestamps)
    valid_dt = np.isfinite(dt) & (dt > 0)
    if max_dt is not None:
        valid_dt &= dt <= max_dt
    norm = np.linalg.norm(values[:, 3:], axis=1)
    valid_q = np.isfinite(norm) & (norm > 1e-12)
    q = np.divide(
        values[:, 3:], norm[:, None], out=np.full_like(values[:, 3:], np.nan), where=valid_q[:, None]
    )
    angles = np.rad2deg(2 * np.arccos(np.clip(np.abs(np.sum(q[1:] * q[:-1], axis=1)), 0, 1)))
    steps = np.linalg.norm(np.diff(values[:, :3], axis=0), axis=1)
    steps[~valid_dt] = np.nan
    angles[~valid_dt] = np.nan
    return steps, angles, norm


def _episodes(root, paths, selected, fields):
    """Project numeric columns and join episode fragments across batches and v3 shards."""
    pending = []
    current = None
    seen = set()
    for path in paths:
        parquet = pq.ParquetFile(root / path)
        columns = [
            key
            for key in dict.fromkeys(("episode_index", "frame_index", "timestamp", *fields))
            if key in parquet.schema_arrow.names
        ]
        if "episode_index" not in columns:
            raise ValueError("EEF checks require episode_index in Parquet")
        for batch in parquet.iter_batches(columns=columns, batch_size=65536):
            ids = np.asarray(batch.column("episode_index").to_pylist()).reshape(-1)
            cuts = np.r_[0, np.flatnonzero(ids[1:] != ids[:-1]) + 1, len(ids)]
            for start, stop in zip(cuts[:-1], cuts[1:], strict=True):
                episode = int(ids[start])
                if episode not in selected:
                    continue
                if current != episode:
                    if pending:
                        yield current, pa.concat_tables(pending, promote_options="default")
                    if episode in seen:
                        raise ValueError("EEF checks require contiguous rows for each episode")
                    seen.add(episode)
                    pending, current = [], episode
                pending.append(pa.Table.from_batches([batch.slice(int(start), int(stop - start))]))
    if pending:
        yield current, pa.concat_tables(pending, promote_options="default")
    if seen != selected:
        raise ValueError(f"Missing Parquet rows for selected episodes: {sorted(selected - seen)[:20]}")


def scan_eef_quality(root, info, meta, episode_rows, config, progress=None, signal_config=None):
    """Two streaming passes; quantiles use temporary disk arrays, never source files."""
    from lerobot.data_platform.precompute.preprocess.common import emit, format_data_path
    from lerobot.data_platform.precompute.quality_signals import (
        detect_signal_quality,
        numeric_features,
        signal_issue,
    )

    root = Path(root)
    features = pose_features(info.get("features", {}))
    columns = numeric_features(info.get("features", {})) if signal_config is not None else features
    selected = {int(row["episode_index"]) for row in episode_rows}
    expected = {int(row["episode_index"]): int(row["length"]) for row in episode_rows}
    paths = sorted({meta.get_data_file_path(ep) if meta else format_data_path(info, ep) for ep in selected})
    # v3 may split an episode across files; include the entire numeric shard sequence.
    if meta:
        pattern = re.sub(r"\{[^}]+\}", "*", info["data_path"])
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("Dataset data_path must stay within the dataset root")
        paths = sorted(path.relative_to(root) for path in root.glob(pattern))
    fps = float(info.get("fps") or 0)
    max_dt = 1.5 / fps if fps > 0 else None
    thresholds = {}
    issues, total_frames = [], 0
    with tempfile.TemporaryDirectory(prefix="quality-eef-") as temporary:
        files = {
            (key, metric): Path(temporary) / f"{i}-{metric}.bin"
            for i, key in enumerate(features)
            for metric in ("position", "rotation")
        }
        for i, (ep, table) in enumerate(_episodes(root, paths, selected, columns), 1):
            if table.num_rows != expected[ep]:
                raise ValueError(f"Episode {ep} row count does not match metadata")
            if "frame_index" not in table.column_names or not np.array_equal(
                np.asarray(table["frame_index"].to_pylist()).reshape(-1), np.arange(table.num_rows)
            ):
                raise ValueError(
                    f"Episode {ep} has invalid frame indices; repair the source index before motion review"
                )
            timestamps, poses = _arrays(table, features)
            for key, values in poses.items():
                steps, angles, _ = motion_steps(values, timestamps, max_dt)
                for metric, values in (("position", steps), ("rotation", angles)):
                    with files[key, metric].open("ab") as stream:
                        values[np.isfinite(values)].astype("float64").tofile(stream)
            emit(
                progress,
                status="running",
                current=i,
                total=len(selected) * 2,
                message=f"Calibrating end-effector baseline: episode {ep}",
            )
        for key, path in files.items():
            if path.stat().st_size:
                values = np.memmap(path, dtype="float64", mode="r+")
                thresholds[key] = float(np.quantile(values, config.quantile, overwrite_input=True))
                del values
            else:
                thresholds[key] = None
        for i, (ep, table) in enumerate(_episodes(root, paths, selected, columns), 1):
            timestamps, poses = _arrays(table, features)
            total_frames += len(timestamps)
            if signal_config is not None:
                issues.extend(
                    detect_signal_quality(
                        ep,
                        timestamps,
                        {key: table[key].to_pylist() for key in columns if key in table.column_names},
                        info.get("features", {}),
                        ["s3"],
                        signal_config,
                    )
                )
            dt = np.diff(timestamps)
            bad_time = set(np.flatnonzero(~np.isfinite(timestamps)).tolist())
            bad_time.update((np.flatnonzero(~np.isfinite(dt) | (dt <= 0)) + 1).tolist())
            if fps > 0:
                bad_time.update((np.flatnonzero(dt > 1.5 / fps) + 1).tolist())
            if bad_time:
                issues.append(
                    signal_issue(
                        ep, "eef", "invalid_timestamps", bad_time, timestamps, field="timestamp", unit="s"
                    )
                )
            for key, values in poses.items():
                steps, angles, norms = motion_steps(values, timestamps, max_dt)
                invalid = ~np.isfinite(values).all(axis=1)
                if invalid.any():
                    issues.append(
                        signal_issue(
                            ep, "eef", "invalid_pose", np.flatnonzero(invalid), timestamps, field=key
                        )
                    )
                bad_q = (
                    (~np.isfinite(norms))
                    | (norms <= 1e-12)
                    | (np.abs(norms - 1) > config.quaternion_tolerance)
                )
                if bad_q.any():
                    issues.append(
                        signal_issue(
                            ep,
                            "eef",
                            "invalid_quaternion",
                            np.flatnonzero(bad_q),
                            timestamps,
                            field=key,
                            metrics={"norm_tolerance": config.quaternion_tolerance},
                        )
                    )
                for metric, values in (("position", steps), ("rotation", angles)):
                    if config.strategy == "laundry_spike":
                        scale_unit = (
                            config.position_unit
                            or features[key].get("position_unit")
                            or features[key].get("unit")
                        )
                        if scale_unit not in {"m", "cm", "mm"}:
                            raise ValueError("Laundry EEF detection requires explicit position units")
                        scale = (
                            {"m": 1, "cm": 0.01, "mm": 0.001}[scale_unit]
                            if metric == "position"
                            else np.pi / 180
                        )
                        speed = np.divide(values * scale, dt, out=np.full_like(values, np.nan), where=dt > 0)
                        threshold = config.linear_speed if metric == "position" else config.angular_speed
                        ratio = config.spike_ratio if metric == "position" else config.angular_spike_ratio
                        local = np.full(len(speed), np.nan)
                        half = config.window // 2
                        for position in range(len(speed)):
                            neighbors = np.r_[
                                speed[max(0, position - half) : position],
                                speed[position + 1 : position + half + 1],
                            ]
                            neighbors = neighbors[np.isfinite(neighbors)]
                            if len(neighbors):
                                local[position] = np.median(neighbors)
                        flags = np.flatnonzero(
                            np.isfinite(speed)
                            & (speed > threshold)
                            & (speed > ratio * np.maximum(local, 1e-3))
                        )
                        if len(flags):
                            issues.append(
                                signal_issue(
                                    ep,
                                    "eef",
                                    f"eef_{metric}_jump",
                                    flags + 1,
                                    timestamps,
                                    field=key,
                                    unit="m/s" if metric == "position" else "rad/s",
                                    metrics={
                                        "algorithm": "laundry_spike",
                                        "algorithm_version": "1.0.0",
                                        "source_commit": "6ba72ff6ca4ccb143eeeb15bcafd3047aac14845",
                                        "threshold": threshold,
                                        "spike_ratio": ratio,
                                        "peak_speed": float(speed[flags].max()),
                                        "position_unit": scale_unit,
                                    },
                                )
                            )
                        continue
                    threshold = thresholds[key, metric]
                    if threshold is None or not len(values):
                        continue
                    # Invalid samples are excluded from baseline comparisons, never padded into valid poses.
                    padded = np.pad(values, config.window // 2, mode="edge")
                    windows = sliding_window_view(padded, config.window)
                    local = np.full(len(values), np.nan)
                    valid_windows = np.isfinite(windows).all(axis=1)
                    local[valid_windows] = np.median(windows[valid_windows], axis=1)
                    flags = np.flatnonzero(
                        np.isfinite(values)
                        & (values > threshold)
                        & (values > config.local_ratio * np.maximum(local, 1e-12))
                    )
                    if not len(flags):
                        continue
                    unit = "deg" if metric == "rotation" else "source_position_unit"
                    peak = int(flags[np.argmax(values[flags])])
                    issues.append(
                        signal_issue(
                            ep,
                            "eef",
                            f"eef_{metric}_jump",
                            flags + 1,
                            timestamps,
                            field=key,
                            unit=unit,
                            severity="candidate",
                            metrics={
                                "quantile": config.quantile,
                                "baseline_threshold": threshold,
                                "local_ratio": config.local_ratio,
                                "window": config.window,
                                "candidate_frames": len(flags),
                                "peak_frame": peak + 1,
                                "peak_step": float(values[peak]),
                                "peak_speed": float(values[peak] / dt[peak]),
                                "speed_unit": f"{unit}/s",
                                "baseline_episodes": len(selected),
                            },
                        )
                    )
            emit(
                progress,
                status="running",
                current=len(selected) + i,
                total=len(selected) * 2,
                message=f"Checked end-effector motion: episode {ep}",
            )
    return total_frames, [issue.to_dict() for issue in issues]

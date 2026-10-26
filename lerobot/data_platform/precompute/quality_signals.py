"""Read-only signal quality checks and their portable evidence records."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field

import numpy as np

from lerobot.data_platform.precompute.timeseries import GRIPPER_NORMALIZE_INDICES, feature_vector_dim

QUALITY_CHECKS = ("legacy", "control", "annotations", "s1", "eef", "s3", "c3")
QUALITY_PROTOCOL = 1
QC_CONFIG_VERSION = 1
INDEX_FIELDS = {"index", "frame_index", "episode_index", "task_index", "subtask_state"}


def numeric_features(features: dict) -> dict[str, dict]:
    return {
        key: feature
        for key, feature in features.items()
        if key not in INDEX_FIELDS
        and str(feature.get("dtype", "")).startswith(("float", "int", "uint"))
        and isinstance(feature.get("shape", [1]), (int, list, tuple))
        and (isinstance(feature.get("shape"), int) or len(feature.get("shape", [1])) == 1)
    }


def finite_number(value, name: str, *, minimum: float | None = None) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return float(value)


@dataclass(frozen=True)
class SignalBound:
    field: str
    dimension: int
    unit: str
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: tuple[float, ...] = ()


@dataclass(frozen=True)
class SignalQualityConfig:
    max_step: float = 0.5
    max_speed: float = 20.0
    bounds: tuple[SignalBound, ...] = ()

    @classmethod
    def parse(cls, s1: dict, s3: dict) -> SignalQualityConfig:
        if not isinstance(s1, dict) or set(s1) - {"max_step", "max_speed"}:
            raise ValueError("Unknown S1 configuration")
        if not isinstance(s3, dict) or set(s3) - {"bounds"}:
            raise ValueError("Unknown S3 configuration")
        step = finite_number(s1.get("max_step", 0.5), "s1.max_step", minimum=1e-12)
        speed = finite_number(s1.get("max_speed", 20.0), "s1.max_speed", minimum=1e-12)
        rows = s3.get("bounds", [])
        if not isinstance(rows, list):
            raise ValueError("s3.bounds must be a list")
        bounds = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - {
                "field",
                "dimension",
                "unit",
                "min",
                "max",
                "allowed_values",
            }:
                raise ValueError("Unknown S3 bound configuration")
            if not isinstance(row.get("field"), str) or not row["field"]:
                raise ValueError("S3 bounds require a field")
            if type(row.get("dimension")) is not int or row["dimension"] < 0:
                raise ValueError("S3 bounds require a non-negative dimension")
            if not isinstance(row.get("unit"), str) or not row["unit"]:
                raise ValueError("S3 bounds require an explicit unit (use 'unitless' for codes)")
            low = finite_number(row["min"], "bound.min") if "min" in row else None
            high = finite_number(row["max"], "bound.max") if "max" in row else None
            codes = row.get("allowed_values", [])
            if not isinstance(codes, list):
                raise ValueError("allowed_values must be a list")
            codes = tuple(finite_number(value, "allowed_value") for value in codes)
            if low is None and high is None and not codes:
                raise ValueError("S3 bounds require min, max, or allowed_values")
            if low is not None and high is not None and low > high:
                raise ValueError("S3 bound min must not exceed max")
            bounds.append(SignalBound(row["field"], row["dimension"], row["unit"], low, high, codes))
        return cls(step, speed, tuple(bounds))

    def validate_features(self, features: dict) -> None:
        for bound in self.bounds:
            feature = numeric_features(features).get(bound.field)
            if feature is None or bound.dimension >= (feature_vector_dim(feature) or 1):
                raise ValueError(f"Unknown S3 signal dimension: {bound.field}[{bound.dimension}]")
            unit = feature.get("unit") or feature.get("units")
            if unit != bound.unit:
                raise ValueError(f"S3 bound unit does not match metadata for {bound.field}")

    def to_config(self) -> dict:
        return {
            "s1": {"max_step": self.max_step, "max_speed": self.max_speed},
            "s3": {
                "bounds": [
                    {
                        "field": bound.field,
                        "dimension": bound.dimension,
                        "unit": bound.unit,
                        **({"min": bound.minimum} if bound.minimum is not None else {}),
                        **({"max": bound.maximum} if bound.maximum is not None else {}),
                        **({"allowed_values": list(bound.allowed_values)} if bound.allowed_values else {}),
                    }
                    for bound in self.bounds
                ]
            },
        }


@dataclass(frozen=True)
class QualityIssue:
    episode: int
    detector: str
    reason: str
    frame_intervals: tuple[tuple[int, int], ...]
    time_intervals: tuple[tuple[float, float], ...] = ()
    field: str | None = None
    dimension: int | None = None
    camera: str | None = None
    unit: str | None = None
    severity: str = "error"
    metrics: dict = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict:
        value = asdict(self)
        value["type"] = "quality_flag"
        # Keep the legacy frame field bounded; intervals contain the complete evidence.
        value["frames"] = [start for start, _ in self.frame_intervals[:256]]
        return value


def signal_issue(
    episode: int, detector: str, reason: str, frames, timestamps: np.ndarray, **kwargs
) -> QualityIssue:
    indices = sorted({int(index) for index in frames})
    intervals = []
    for index in indices:
        if intervals and intervals[-1][1] == index:
            intervals[-1] = (intervals[-1][0], index + 1)
        else:
            intervals.append((index, index + 1))
    times = []
    positive_dt = np.diff(timestamps)
    positive_dt = positive_dt[np.isfinite(positive_dt) & (positive_dt > 0)]
    dt = float(np.median(positive_dt)) if len(positive_dt) else None
    for start, end in intervals:
        if dt is not None and np.isfinite(timestamps[start:end]).all():
            stop = timestamps[end] if end < len(timestamps) else timestamps[end - 1] + dt
            if np.isfinite(stop) and stop > timestamps[start]:
                times.append((float(timestamps[start]), float(stop)))
    return QualityIssue(episode, detector, reason, tuple(intervals), tuple(times), **kwargs)


def detect_signal_quality(
    episode: int,
    timestamps: np.ndarray,
    columns: dict[str, list],
    features: dict,
    checks: list[str],
    config: SignalQualityConfig,
) -> list[QualityIssue]:
    """Inspect raw values before padding; ragged rows are schema errors, not synthetic NaNs."""
    issues = []
    for key, feature in numeric_features(features).items():
        if key in columns:
            values = columns[key]
        elif key == "timestamp":
            values = timestamps.tolist()
        else:
            if "s3" in checks:
                issues.append(
                    signal_issue(
                        episode, "s3", "missing_signal", range(len(timestamps)), timestamps, field=key
                    )
                )
            elif "s1" in checks and key in {"action", "state"}:
                issues.append(
                    signal_issue(
                        episode,
                        "s1",
                        "missing_signal",
                        range(len(timestamps)),
                        timestamps,
                        field=key,
                        severity="warning",
                    )
                )
            continue
        dim = feature_vector_dim(feature) or 1
        array = np.full((len(values), dim), np.nan)
        valid = np.zeros(len(values), dtype=bool)
        bad_shape = []
        missing = []
        for index, value in enumerate(values):
            row = value if isinstance(value, (list, tuple)) else [value]
            if len(row) != dim:
                bad_shape.append(index)
                continue
            if any(item is None for item in row):
                missing.append(index)
                continue
            array[index] = row
            valid[index] = True
        unit = feature.get("unit") or feature.get("units")
        if "s3" in checks:
            for reason, frames in (("signal_shape_mismatch", bad_shape), ("missing_value", missing)):
                if frames:
                    issues.append(
                        signal_issue(
                            episode,
                            "s3",
                            reason,
                            frames,
                            timestamps,
                            field=key,
                            unit=unit,
                            metrics={"expected_dimension": dim},
                        )
                    )
            for dimension in range(dim):
                frames = np.flatnonzero(valid & ~np.isfinite(array[:, dimension]))
                if len(frames):
                    issues.append(
                        signal_issue(
                            episode,
                            "s3",
                            "non_finite",
                            frames,
                            timestamps,
                            field=key,
                            dimension=dimension,
                            unit=unit,
                        )
                    )
            for bound in config.bounds:
                if bound.field != key:
                    continue
                series = array[:, bound.dimension]
                bad = np.zeros(len(series), dtype=bool)
                if bound.minimum is not None:
                    bad |= series < bound.minimum
                if bound.maximum is not None:
                    bad |= series > bound.maximum
                if bound.allowed_values:
                    bad |= ~np.isin(series, bound.allowed_values)
                frames = np.flatnonzero(bad & valid & np.isfinite(series))
                if len(frames):
                    issues.append(
                        signal_issue(
                            episode,
                            "s3",
                            "out_of_bounds",
                            frames,
                            timestamps,
                            field=key,
                            dimension=bound.dimension,
                            unit=unit,
                            metrics={
                                "min": bound.minimum,
                                "max": bound.maximum,
                                "allowed_values": bound.allowed_values,
                                "observed_min": float(series[frames].min()),
                                "observed_max": float(series[frames].max()),
                            },
                        )
                    )
        if "s1" not in checks or key not in {"action", "state"} or dim < 16:
            continue
        if "s3" not in checks:
            unusable = np.flatnonzero(~valid | ~np.isfinite(array[:, :16]).all(axis=1))
            if len(unusable):
                issues.append(
                    signal_issue(
                        episode,
                        "s1",
                        "invalid_signal_values",
                        unusable,
                        timestamps,
                        field=key,
                        severity="warning",
                    )
                )
        dt = np.diff(timestamps)
        bad_dt = np.flatnonzero(~np.isfinite(dt) | (dt <= 0)) + 1
        if len(bad_dt) and key == next((name for name in columns if name in {"action", "state"}), key):
            issues.append(
                signal_issue(
                    episode,
                    "s1",
                    "invalid_timestamps",
                    bad_dt,
                    timestamps,
                    field="timestamp",
                    severity="warning",
                )
            )
        valid_pairs = valid[1:] & valid[:-1] & np.isfinite(dt) & (dt > 0)
        for dimension in range(16):
            if dimension in GRIPPER_NORMALIZE_INDICES:
                continue
            delta = np.abs(np.diff(array[:, dimension]))
            velocity = np.divide(delta, dt, out=np.zeros_like(delta), where=valid_pairs)
            bad = (
                valid_pairs & np.isfinite(delta) & ((delta > config.max_step) | (velocity > config.max_speed))
            )
            frames = np.flatnonzero(bad) + 1
            if len(frames):
                issues.append(
                    signal_issue(
                        episode,
                        "s1",
                        "joint_jump",
                        frames,
                        timestamps,
                        field=key,
                        dimension=dimension,
                        unit="rad",
                        severity="warning",
                        metrics={
                            "max_step": config.max_step,
                            "max_speed": config.max_speed,
                            "observed_step": float(delta[frames - 1].max()),
                            "observed_speed": float(velocity[frames - 1].max()),
                        },
                    )
                )
    return issues

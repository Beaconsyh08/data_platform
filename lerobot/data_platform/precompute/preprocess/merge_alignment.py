"""Plan and apply named signal projections for dataset merge."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from lerobot.data_platform.precompute.preprocess.action_dim import _trim_arrow_type

SIGNAL_FIELDS = ("action", "state", "observation.state")
DIMENSION_POLICIES = ("strict", "min")


@dataclass(frozen=True)
class SignalProjection:
    field: str
    source_names: tuple[str, ...]
    target_names: tuple[str, ...]
    indices: tuple[int, ...]

    def summary(self) -> dict:
        return {
            "source_dim": len(self.source_names),
            "target_dim": len(self.target_names),
            "target_names": list(self.target_names),
            "source_indices": list(self.indices),
            "dropped_names": [name for name in self.source_names if name not in self.target_names],
        }


def signal_names(feature: dict, field: str) -> tuple[str, ...]:
    shape = feature.get("shape")
    if isinstance(shape, int):
        shape = [shape]
    if not isinstance(shape, (list, tuple)) or len(shape) != 1 or int(shape[0]) < 1:
        raise ValueError(f"{field}: min merge requires a one-dimensional signal")
    names = feature.get("names")
    while isinstance(names, dict) and len(names) == 1:
        names = next(iter(names.values()))
    if (
        not isinstance(names, (list, tuple))
        or len(names) != int(shape[0])
        or any(not isinstance(name, str) or not name.strip() for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError(
            f"{field}: min merge requires complete, unique dimension names; cannot infer indices"
        )
    dtype = str(feature.get("dtype") or "")
    if not dtype.startswith(("float", "int", "uint")):
        raise ValueError(f"{field}: min merge requires a numeric signal, got {dtype!r}")
    return tuple(names)


def _project_feature(feature: dict, projection: SignalProjection) -> dict:
    result = deepcopy(feature)
    result["shape"] = [len(projection.indices)]
    result["names"] = list(projection.target_names)
    # Units may be scalar, per-dimension lists, or maps keyed by dimension name.
    for key in ("unit", "units"):
        value = result.get(key)
        if isinstance(value, list):
            if len(value) != len(projection.source_names):
                raise ValueError(f"{projection.field}: {key} does not match the source dimensions")
            result[key] = [value[index] for index in projection.indices]
        elif isinstance(value, dict):
            if set(value) != set(projection.source_names):
                raise ValueError(f"{projection.field}: {key} must describe every source dimension")
            result[key] = {name: value[name] for name in projection.target_names}
    return result


def plan_signal_alignment(
    infos: list[dict], policy: str
) -> tuple[list[dict], list[tuple[SignalProjection, ...]]]:
    if policy not in DIMENSION_POLICIES:
        raise ValueError(f"dimension_policy must be one of {DIMENSION_POLICIES}")
    if policy == "strict":
        return infos, [() for _ in infos]
    aligned_infos = deepcopy(infos)
    projections: list[list[SignalProjection]] = [[] for _ in infos]
    for field in SIGNAL_FIELDS:
        features = [(info.get("features") or {}).get(field) for info in infos]
        if not any(feature is not None for feature in features):
            continue
        if any(not isinstance(feature, dict) for feature in features):
            raise ValueError(f"{field}: every source must contain this signal for min merge")
        try:
            names = [signal_names(feature, field) for feature in features]
        except ValueError as exc:
            semantics = [
                {key: value for key, value in feature.items() if key != "fps"} for feature in features
            ]
            if all(feature == semantics[0] for feature in semantics[1:]):
                # Identical schemas need no projection, just as in strict merge.
                continue
            raise ValueError(
                f"{exc}. Source schemas differ; provide complete dimension names in "
                f"meta/info.json features.{field}.names before aligning signals."
            ) from exc
        target = min(names, key=len)  # Ties use the first source's ordering.
        target_feature = None
        for position, (feature, source_names) in enumerate(zip(features, names, strict=True)):
            missing = sorted(set(target) - set(source_names))
            if missing:
                raise ValueError(f"{field}: source {position} is missing target dimensions {missing}")
            projection = SignalProjection(
                field, source_names, target, tuple(source_names.index(name) for name in target)
            )
            projected = _project_feature(feature, projection)
            # fps belongs to the physical format (v3 includes it in each feature).
            semantic_feature = {key: value for key, value in projected.items() if key != "fps"}
            if target_feature is not None and semantic_feature != target_feature:
                raise ValueError(f"{field}: dtype, units or other feature semantics differ after alignment")
            target_feature = semantic_feature
            aligned_infos[position]["features"][field] = projected
            projections[position].append(projection)
    return aligned_infos, [tuple(items) for items in projections]


def project_signals(table: pa.Table, projections: tuple[SignalProjection, ...]) -> tuple[pa.Table, dict]:
    stats = {}
    for projection in projections:
        name = projection.field
        if name not in table.column_names:
            raise ValueError(f"Missing signal column {name} in source Parquet")
        field = table.schema.field(name)
        if not (
            pa.types.is_list(field.type)
            or pa.types.is_large_list(field.type)
            or pa.types.is_fixed_size_list(field.type)
        ):
            raise ValueError(f"{name}: source Parquet must contain numeric vectors")
        values = table[name].to_pylist()
        if not values or any(
            not isinstance(row, list)
            or len(row) != len(projection.source_names)
            or any(value is None or isinstance(value, list) for value in row)
            for row in values
        ):
            raise ValueError(f"{name}: actual vector dimensions/nulls do not match feature metadata")
        projected = [[row[index] for index in projection.indices] for row in values]
        array = np.asarray(projected, dtype=np.float64)
        if not np.isfinite(array).all():
            raise ValueError(f"{name}: non-finite values in aligned signal")
        target_type = _trim_arrow_type(field.type, len(projection.indices))
        target_field = pa.field(name, target_type, nullable=field.nullable, metadata=field.metadata)
        table = table.set_column(
            table.column_names.index(name), target_field, pa.array(projected, type=target_type)
        )
        stats[name] = numeric_stats(array)
    if projections and table.schema.metadata and b"huggingface" in table.schema.metadata:
        # Old embedded feature shapes/names must not override the projected Arrow schema.
        table = table.replace_schema_metadata(
            {key: value for key, value in table.schema.metadata.items() if key != b"huggingface"}
        )
    return table, stats


def numeric_stats(values) -> dict:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    return {
        "min": array.min(axis=0).tolist(),
        "max": array.max(axis=0).tolist(),
        "mean": array.mean(axis=0).tolist(),
        "std": array.std(axis=0).tolist(),
        "count": [len(array)],
    }

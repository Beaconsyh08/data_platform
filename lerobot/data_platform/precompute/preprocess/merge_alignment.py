"""Plan and apply named signal projections for dataset merge."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from lerobot.data_platform.merge_options import DIMENSION_POLICIES as DIMENSION_POLICIES
from lerobot.data_platform.merge_options import SIGNAL_FIELDS, validate_alignment_options
from lerobot.data_platform.precompute.preprocess.action_dim import _trim_arrow_type


@dataclass(frozen=True)
class SignalProjection:
    field: str
    source_names: tuple[str, ...]
    target_names: tuple[str, ...]
    indices: tuple[int | None, ...]
    padding_value: float = 0.0

    def summary(self) -> dict:
        return {
            "source_dim": len(self.source_names),
            "target_dim": len(self.target_names),
            "target_names": list(self.target_names),
            "source_indices": list(self.indices),
            "padded_names": [
                name for name, index in zip(self.target_names, self.indices, strict=True) if index is None
            ],
            "padding_value": self.padding_value,
            "dropped_names": [name for name in self.source_names if name not in self.target_names],
        }


def signal_names(feature: dict, field: str) -> tuple[str, ...]:
    shape = feature.get("shape")
    if isinstance(shape, int):
        shape = [shape]
    if not isinstance(shape, (list, tuple)) or len(shape) != 1 or int(shape[0]) < 1:
        raise ValueError(f"{field}: aligned merge requires a one-dimensional signal")
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
            f"{field}: aligned merge requires complete, unique dimension names; cannot infer indices"
        )
    dtype = str(feature.get("dtype") or "")
    if not dtype.startswith(("float", "int", "uint")):
        raise ValueError(f"{field}: aligned merge requires a numeric signal, got {dtype!r}")
    return tuple(names)


def _project_feature(feature: dict, projection: SignalProjection, unit_defaults: dict) -> dict:
    result = deepcopy(feature)
    result["shape"] = [len(projection.indices)]
    result["names"] = list(projection.target_names)
    # Units may be scalar, per-dimension lists, or maps keyed by dimension name.
    for key in ("unit", "units"):
        value = result.get(key)
        if isinstance(value, list):
            if len(value) != len(projection.source_names):
                raise ValueError(f"{projection.field}: {key} does not match the source dimensions")
            result[key] = [
                value[index] if index is not None else unit_defaults[key][name]
                for name, index in zip(projection.target_names, projection.indices, strict=True)
            ]
        elif isinstance(value, dict):
            if set(value) != set(projection.source_names):
                raise ValueError(f"{projection.field}: {key} must describe every source dimension")
            result[key] = {name: unit_defaults[key][name] for name in projection.target_names}
    return result


def _index_dimension_names(
    infos: list[dict], mappings: list[dict], policy: str
) -> tuple[list[dict], dict[str, tuple[str, ...]]]:
    """Translate user-selected positions into names for the shared projection/units checks."""
    names = [{} for _ in infos]
    targets = {}
    for position, (info, mapping) in enumerate(zip(infos, mappings, strict=True)):
        fields = set(info.get("features", {})) & set(SIGNAL_FIELDS)
        if set(mapping) != fields:
            raise ValueError(f"Source {position}: index mappings must cover every action/state field")
    for field in SIGNAL_FIELDS:
        groups = [mapping[field] for mapping in mappings if field in mapping]
        if not groups:
            continue
        if len(groups) != len(infos) or len({len(indices) for indices in groups}) != 1:
            raise ValueError(f"{field}: every source must have the same output dimension count")
        target = tuple(f"index_{i + 1}" for i in range(len(groups[0])))
        if any(all(indices[i] is None for indices in groups) for i in range(len(target))):
            raise ValueError(f"{field}: each output position must have a real signal in at least one source")
        targets[field] = target
        for position, (info, indices) in enumerate(zip(infos, groups, strict=True)):
            feature = info["features"][field]
            shape = feature.get("shape")
            if isinstance(shape, int):
                shape = [shape]
            if not isinstance(shape, (list, tuple)) or len(shape) != 1 or int(shape[0]) < 1:
                raise ValueError(f"{field}: index mapping requires a one-dimensional signal")
            dimension = int(shape[0])
            used = [index for index in indices if index is not None]
            if any(index >= dimension for index in used):
                raise ValueError(f"{field}: source index exceeds {dimension} dimensions")
            if policy == "pad" and used != list(range(dimension)):
                raise ValueError(f"{field}: padding must preserve every source dimension")
            source_names = [f"dropped_{position}_{i}" for i in range(dimension)]
            for name, index in zip(target, indices, strict=True):
                if index is not None:
                    source_names[index] = name
            names[position][field] = source_names
    return names, targets


def plan_signal_alignment(
    infos: list[dict],
    policy: str,
    dimension_names: list[dict] | None = None,
    padding_value: float = 0,
    dimension_indices: list[dict] | None = None,
) -> tuple[list[dict], list[tuple[SignalProjection, ...]]]:
    validate_alignment_options(policy, dimension_names, padding_value, len(infos), dimension_indices)
    if policy == "strict":
        return infos, [() for _ in infos]
    index_targets = {}
    if dimension_indices is not None:
        dimension_names, index_targets = _index_dimension_names(infos, dimension_indices, policy)
    aligned_infos = deepcopy(infos)
    for position, mapping in enumerate(dimension_names or []):
        for field, names in mapping.items():
            feature = (aligned_infos[position].get("features") or {}).get(field)
            if not isinstance(feature, dict):
                raise ValueError(f"Source {position}: missing mapped field {field}")
            for key in ("unit", "units"):
                if isinstance(feature.get(key), dict):
                    old_names = signal_names(feature, field)
                    if len(old_names) != len(names) or set(feature[key]) != set(old_names):
                        raise ValueError(f"{field}: cannot remap per-dimension units")
                    feature[key] = {new: feature[key][old] for old, new in zip(old_names, names, strict=True)}
            feature["names"] = names
            signal_names(feature, field)  # Validate exact dimension count and numeric dtype.
    projections: list[list[SignalProjection]] = [[] for _ in infos]
    for field in SIGNAL_FIELDS:
        features = [(info.get("features") or {}).get(field) for info in aligned_infos]
        if not any(feature is not None for feature in features):
            continue
        if any(not isinstance(feature, dict) for feature in features):
            raise ValueError(f"{field}: every source must contain this signal for aligned merge")
        try:
            names = [signal_names(feature, field) for feature in features]
        except ValueError as exc:
            semantics = [
                {key: value for key, value in feature.items() if key != "fps"} for feature in features
            ]
            if all(feature == semantics[0] for feature in semantics[1:]):
                continue
            raise ValueError(
                f"{exc}. Source schemas differ; configure explicit dimension mappings or provide complete "
                f"dimension names in meta/info.json features.{field}.names before aligning signals."
            ) from exc
        target = min(names, key=len)
        if policy == "pad":
            # Preserve the widest source layout (including trailing shared signals).
            target = tuple(
                dict.fromkeys([*max(names, key=len), *(name for source in names for name in source)])
            )
        if field in index_targets:
            target = index_targets[field]
        unit_defaults = {}
        for feature, source_names in zip(features, names, strict=True):
            for key in ("unit", "units"):
                value = feature.get(key)
                if isinstance(value, list):
                    if len(value) != len(source_names):
                        raise ValueError(f"{field}: {key} does not match the source dimensions")
                    values = dict(zip(source_names, value, strict=True))
                elif isinstance(value, dict):
                    if set(value) != set(source_names):
                        raise ValueError(f"{field}: {key} must describe every source dimension")
                    values = value
                else:
                    continue
                defaults = unit_defaults.setdefault(key, {})
                for name, unit in values.items():
                    if name in target and name in defaults and defaults[name] != unit:
                        raise ValueError(f"{field}: units differ for {name}")
                    defaults[name] = unit
        target_feature = None
        for position, (feature, source_names) in enumerate(zip(features, names, strict=True)):
            missing = sorted(set(target) - set(source_names))
            if missing and policy != "pad":
                raise ValueError(f"{field}: source {position} is missing target dimensions {missing}")
            if (
                missing
                and str(feature.get("dtype", "")).startswith(("int", "uint"))
                and int(padding_value) != padding_value
            ):
                raise ValueError(f"{field}: integer signals require an integer padding value")
            if missing:
                dtype = np.dtype(feature["dtype"])
                limits = np.iinfo(dtype) if np.issubdtype(dtype, np.integer) else np.finfo(dtype)
                lower = limits.min.item() if isinstance(limits.min, np.generic) else limits.min
                upper = limits.max.item() if isinstance(limits.max, np.generic) else limits.max
                if not lower <= padding_value <= upper:
                    raise ValueError(f"{field}: padding value is outside the {dtype} range")
            projection = SignalProjection(
                field,
                source_names,
                target,
                tuple(source_names.index(name) if name in source_names else None for name in target),
                padding_value,
            )
            projected = _project_feature(feature, projection, unit_defaults)
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
        projected = [
            [row[index] if index is not None else projection.padding_value for index in projection.indices]
            for row in values
        ]
        array = np.asarray(projected, dtype=np.float64)
        if not np.isfinite(array).all():
            raise ValueError(f"{name}: non-finite values in aligned signal")
        target_type = (
            pa.list_(field.type.value_type, len(projection.indices))
            if pa.types.is_fixed_size_list(field.type)
            else _trim_arrow_type(field.type, len(projection.indices))
        )
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

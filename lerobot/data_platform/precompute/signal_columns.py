"""One column description shared by CSV writers, manifests and the viewer."""

from __future__ import annotations

from collections import Counter

SIGNAL_COLUMNS_VERSION = 2


def signal_columns(features: dict, *, qualify: bool = False) -> list[dict]:
    columns = []
    for key, feature in features.items():
        dtype = str(feature.get("dtype") or "")
        if key in {"timestamp", "subtask_state", "index", "frame_index", "episode_index", "task_index"}:
            continue
        if not dtype.startswith(("float", "int", "uint")):
            continue
        shape = feature.get("shape") or [1]
        if isinstance(shape, int):
            shape = [shape]
        if len(shape) != 1:
            continue
        dim = int(shape[0])
        names = feature.get("names")
        while isinstance(names, dict) and names:
            names = next(iter(names.values()))
        if not isinstance(names, (list, tuple)) or len(names) != dim:
            names = [str(i) for i in range(dim)]
            labels = (
                ["exist_label"] if key == "exist_label" and dim == 1 else [f"{key}_{i}" for i in range(dim)]
            )
        else:
            labels = [str(name) for name in names]
        if qualify:
            labels = [key] if dim == 1 else [f"{key}.{name}" for name in names]
        columns.append(
            {
                "key": key,
                "value": labels,
                "unit": feature.get("unit") or feature.get("units"),
                "coordinate_frame": feature.get("coordinate_frame"),
                "group": "head"
                if key.startswith("head_")
                else "left"
                if key.startswith("left_")
                else "right"
                if key.startswith("right_")
                else key,
            }
        )
    counts = Counter(label for column in columns for label in column["value"])
    for column in columns:
        column["value"] = [
            f"{column['key']}.{label}" if counts[label] > 1 else label for label in column["value"]
        ]
    used = set()
    for column in columns:
        for i, label in enumerate(column["value"]):
            candidate = label
            while candidate in used:
                candidate += f"_{i}"
            column["value"][i] = candidate
            used.add(candidate)
    return columns

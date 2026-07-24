from __future__ import annotations

import numpy as np
import pyarrow as pa


def _column_to_2d(column: pa.ChunkedArray) -> np.ndarray:
    rows = []
    for value in column.to_pylist():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            rows.append([float(v) for v in value])
        else:
            rows.append([float(value)])
    if not rows:
        return np.empty((0, 0), dtype=np.float32)
    width = max(len(row) for row in rows)
    padded = [row + [0.0] * (width - len(row)) for row in rows]
    return np.asarray(padded, dtype=np.float32)


def arm_from_action(table) -> str:
    if "action" not in table.column_names:
        return "unclear"
    arr = _column_to_2d(table["action"])
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] < 2:
        return "unclear"

    split = arr.shape[1] // 2
    if split == 0 or split == arr.shape[1]:
        return "unclear"
    motion = np.abs(np.diff(arr, axis=0)).sum(axis=0) if arr.shape[0] > 1 else np.abs(arr[0])
    left = float(motion[:split].sum())
    right = float(motion[split:].sum())
    if max(left, right) < 1e-6:
        return "unclear"
    ratio = max(left, right) / max(1e-6, min(left, right))
    if ratio < 1.25:
        return "both"
    return "left" if left > right else "right"


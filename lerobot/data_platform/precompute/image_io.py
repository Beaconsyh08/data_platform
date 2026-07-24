from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterator

import pyarrow.parquet as pq


@lru_cache(maxsize=8)
def get_parquet_file(path: str) -> pq.ParquetFile:
    return pq.ParquetFile(path)


@lru_cache(maxsize=8)
def get_row_group_offsets(path: str) -> list[int]:
    parquet_file = get_parquet_file(path)
    offsets: list[int] = []
    total = 0
    for row_group_idx in range(parquet_file.metadata.num_row_groups):
        total += parquet_file.metadata.row_group(row_group_idx).num_rows
        offsets.append(total)
    return offsets


def _find_row_group(offsets: list[int], row_index: int) -> tuple[int, int]:
    for row_group_idx, end in enumerate(offsets):
        if row_index < end:
            start = 0 if row_group_idx == 0 else offsets[row_group_idx - 1]
            return row_group_idx, row_index - start
    raise IndexError(f"Row index out of range: {row_index}")


def _resolve_image_bytes(dataset_root: Path, image_key: str, value: object) -> bytes | None:
    if not isinstance(value, dict):
        return None

    image_bytes = value.get("bytes")
    if image_bytes is not None:
        return image_bytes

    image_path = value.get("path")
    if not image_path:
        return None

    candidates = [
        dataset_root / image_path,
        dataset_root / "images" / image_path,
        dataset_root / "images" / image_key / image_path,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes()

    return None


def read_image_bytes(
    parquet_path: Path,
    dataset_root: Path,
    image_key: str,
    frame_index: int,
) -> bytes | None:
    parquet_file = get_parquet_file(str(parquet_path))
    offsets = get_row_group_offsets(str(parquet_path))
    row_group_idx, local_idx = _find_row_group(offsets, frame_index)
    table = parquet_file.read_row_group(row_group_idx, columns=[image_key])
    if len(table) == 0:
        return None

    return _resolve_image_bytes(dataset_root, image_key, table[image_key][local_idx].as_py())


@lru_cache(maxsize=256)
def cached_image_bytes(
    parquet_path: str,
    dataset_root: str,
    image_key: str,
    frame_index: int,
) -> bytes | None:
    return read_image_bytes(Path(parquet_path), Path(dataset_root), image_key, frame_index)


def iter_image_bytes(
    parquet_path: Path,
    dataset_root: Path,
    image_key: str,
    max_frames: int | None = None,
) -> Iterator[bytes]:
    parquet_file = get_parquet_file(str(parquet_path))
    total = 0
    for row_group_idx in range(parquet_file.metadata.num_row_groups):
        table = parquet_file.read_row_group(row_group_idx, columns=[image_key])
        column = table[image_key]
        for row_idx in range(len(column)):
            if max_frames is not None and total >= max_frames:
                return
            image_bytes = _resolve_image_bytes(dataset_root, image_key, column[row_idx].as_py())
            if image_bytes is not None:
                yield image_bytes
                total += 1

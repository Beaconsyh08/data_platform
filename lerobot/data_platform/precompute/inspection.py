"""Bounded, read-only inspection of source records and published Viewer CSV files."""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from lerobot.data_platform.cache_files import cached_csv_files, logical_csv_path, open_cached_csv
from lerobot.data_platform.precompute.dataset_io import is_v3_info

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CELL_BYTES = 16 * 1024
MAX_ROWS = 200


def contained_path(root: Path, path: Path) -> Path:
    """Resolve symlinks and reject reads outside the registered root."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PermissionError("Inspection file is outside the registered root")
    return resolved


def read_inspection_json(root: Path, path: Path) -> dict:
    path = contained_path(root, path)
    if path.stat().st_size > MAX_RESPONSE_BYTES:
        raise ValueError("Inspection metadata exceeds the 2 MiB limit")
    return json.loads(path.read_text())


@dataclass(frozen=True)
class InspectColumn:
    id: str
    name: str
    dtype: str
    shape: list | None = None
    names: list | dict | None = None
    previewable: bool = True


def _contains_binary(dtype: pa.DataType) -> bool:
    if pa.types.is_binary(dtype) or pa.types.is_large_binary(dtype) or pa.types.is_fixed_size_binary(dtype):
        return True
    if pa.types.is_struct(dtype):
        return any(_contains_binary(field.type) for field in dtype)
    if pa.types.is_list(dtype) or pa.types.is_large_list(dtype) or pa.types.is_fixed_size_list(dtype):
        return _contains_binary(dtype.value_type)
    if pa.types.is_map(dtype):
        return _contains_binary(dtype.key_type) or _contains_binary(dtype.item_type)
    return False


def _bounded_value(value: Any, budget: list[int], depth: int = 0) -> Any:
    """Convert Arrow scalars without materializing large arrays or binary payloads."""
    if isinstance(value, pa.Scalar):
        if not value.is_valid:
            return None
        if depth >= 8:
            return {"truncated": True, "dtype": str(value.type)}
        if pa.types.is_struct(value.type):
            result = {}
            for field in value.type:
                if budget[0] < 128:
                    result["__inspect_truncated__"] = True
                    break
                budget[0] -= len(field.name.encode()) + 8
                result[field.name] = _bounded_value(value[field.name], budget, depth + 1)
            return result
        if isinstance(value, (pa.ListScalar, pa.LargeListScalar, pa.FixedSizeListScalar, pa.MapScalar)):
            items = []
            for item in value.values:
                if len(items) >= 256 or budget[0] < 128:
                    break
                items.append(_bounded_value(item, budget, depth + 1))
            if len(items) < len(value.values):
                return {"preview": items, "length": len(value.values), "truncated": True}
            return items
        if _contains_binary(value.type):
            return {"dtype": str(value.type), "omitted": "Binary data is not loaded"}
        value = value.as_py()
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        allowance = max(0, min(budget[0] // 2, MAX_CELL_BYTES))
        budget[0] -= min(len(encoded) * 2 + 8, budget[0])
        if len(encoded) > allowance:
            return {
                "preview": encoded[:allowance].decode("utf-8", errors="ignore"),
                "length": len(value),
                "truncated": True,
            }
        return value
    budget[0] -= 32
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite": str(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return value
    return str(value)


@dataclass
class DatasetInspection:
    root: Path | None
    static_dir: Path | None
    info: dict
    episodes: dict[int, dict]
    remote: dict | None = None

    @classmethod
    def local(cls, root: Path, static_dir: Path | None) -> DatasetInspection:
        info = read_inspection_json(root, root / "meta/info.json")
        episodes = {}
        if is_v3_info(info):
            for path in sorted((root / "meta/episodes").rglob("*.parquet")):
                parquet = pq.ParquetFile(contained_path(root, path))
                columns = [
                    name
                    for name in ("episode_index", "length", "data/chunk_index", "data/file_index")
                    if name in parquet.schema_arrow.names
                ]
                for batch in parquet.iter_batches(batch_size=256, columns=columns):
                    for record in batch.to_pylist():
                        episodes[int(record["episode_index"])] = record
        else:
            path = contained_path(root, root / "meta/episodes.jsonl")
            with path.open() as handle:
                for line in handle:
                    if line.strip():
                        record = json.loads(line)
                        episodes[int(record["episode_index"])] = record
        return cls(root, static_dir, info, episodes)

    def csv_files(self) -> dict[int, Path]:
        files: dict[int, Path] = {}
        if self.static_dir is None:
            return files
        directory = contained_path(self.static_dir, self.static_dir / "csv")
        for path in cached_csv_files(directory, "episode_*_ds*.csv"):
            match = re.fullmatch(r"episode_(\d+)_ds([1-9]\d*)\.csv(?:\.gz)?", path.name)
            if match:
                episode, step = map(int, match.groups())
                current = files.get(episode)
                if current is None or step < int(logical_csv_path(current).stem.rsplit("_ds", 1)[1]):
                    files[episode] = path
        return files

    def _file(self, source: str, episode: int) -> Path:
        if source not in {"parquet", "csv"}:
            raise ValueError("source must be parquet or csv")
        if source == "parquet" and self.root is None:
            raise ValueError("Remote source Parquet inspection is not supported in this release")
        if episode not in self.episodes:
            raise FileNotFoundError("Episode is not registered in this dataset")
        if source == "csv":
            path = self.csv_files().get(episode)
            if path is None:
                raise FileNotFoundError("CSV cache is missing for this episode. Open Cache to prepare it.")
            return contained_path(self.static_dir, path)
        record = self.episodes[episode]
        chunk = episode // int(self.info.get("chunks_size") or 1000)
        relative = self.info["data_path"].format(
            episode_index=episode,
            episode_chunk=chunk,
            chunk_index=int(record.get("data/chunk_index", chunk)),
            file_index=int(record.get("data/file_index", episode)),
        )
        return contained_path(self.root, self.root / relative)

    def columns(self, source: str, episode: int, path: Path | None = None) -> list[InspectColumn]:
        path = path if path is not None else self._file(source, episode)
        if source == "csv":
            with open_cached_csv(path, newline="") as handle:
                header = next(csv.reader(handle), [])
            return [InspectColumn(str(index), name, "CSV text") for index, name in enumerate(header)]
        features = self.info.get("features") or {}
        result = []
        for field in pq.read_schema(path):
            feature = features.get(field.name) or {}
            result.append(
                InspectColumn(
                    field.name,
                    field.name,
                    str(field.type),
                    feature.get("shape"),
                    feature.get("names"),
                    not _contains_binary(field.type) and feature.get("dtype") != "image",
                )
            )
        return result

    def provenance(self, source: str, episode: int, path: Path | None = None) -> dict:
        path = path if path is not None else self._file(source, episode)
        record = self.episodes[episode]
        length = record.get("length")
        if length is None and "dataset_to_index" in record:
            length = record["dataset_to_index"] - record.get("dataset_from_index", 0)
        return {
            "source": source,
            "file": str(path.relative_to((self.root if source == "parquet" else self.static_dir).resolve())),
            "episode_index": episode,
            "source_frames": length,
            "downsample": int(logical_csv_path(path).stem.rsplit("_ds", 1)[1]) if source == "csv" else 1,
            "cache_status": "stale" if self.remote and not self.remote.get("viewer_ready") else "available",
            "remote": self.remote,
        }

    def schema(self, source: str, episode: int | None = None) -> dict:
        if source not in {"parquet", "csv"} or (source == "parquet" and self.root is None):
            raise ValueError("This data source is not supported")
        ids = sorted(self.episodes)
        if episode is None:
            cached = self.csv_files() if source == "csv" else {}
            episode = next((item for item in ids if item in cached), ids[0] if ids else None)
        path = self._file(source, episode) if episode is not None else None
        columns = self.columns(source, episode, path) if episode is not None else []
        return {
            "source": source,
            "episode_index": episode,
            "episodes": ids,
            "columns": [asdict(column) for column in columns],
            "default_columns": [column.id for column in columns],
            "dataset_format": self.info.get("codebase_version"),
            "dataset_frames": self.info.get("total_frames"),
            "provenance": self.provenance(source, episode, path) if episode is not None else None,
        }

    def _parquet_rows(self, path: Path, episode: int, columns: list[InspectColumn]) -> Iterator[list]:
        parquet = pq.ParquetFile(path)
        selected = [column.id for column in columns if column.previewable]
        if "episode_index" not in selected:
            selected.append("episode_index")
        # Column projection never reads embedded images or binary media. Stop consuming batches
        # once the requested window and one lookahead row have been found.
        for batch in parquet.iter_batches(batch_size=256, columns=selected):
            if is_v3_info(self.info):
                batch = batch.filter(pc.equal(batch.column("episode_index"), episode))
            for index in range(batch.num_rows):
                yield [
                    batch.column(column.id)[index]
                    if column.previewable
                    else {"omitted": "Media/binary payload is not loaded", "dtype": column.dtype}
                    for column in columns
                ]

    def rows(self, source: str, episode: int, offset: int, limit: int, fields: list[str] | None) -> dict:
        if offset < 0 or not 1 <= limit <= MAX_ROWS:
            raise ValueError("offset must be nonnegative and limit must be between 1 and 200")
        path = self._file(source, episode)
        available = self.columns(source, episode, path)
        by_id = {column.id: column for column in available}
        if fields is not None and (
            not fields or len(fields) != len(set(fields)) or set(fields) - by_id.keys()
        ):
            raise ValueError("columns must contain distinct column IDs from the schema")
        columns = [by_id[field] for field in fields] if fields is not None else available[:12]
        result = {
            "columns": [asdict(column) for column in columns],
            "rows": [],
            "offset": offset,
            "next_offset": offset,
            "has_more": False,
            "truncated": False,
            "provenance": self.provenance(source, episode, path),
            "cache_rows": None,
        }
        size = len(json.dumps(result, ensure_ascii=False).encode()) + 1024
        if size >= MAX_RESPONSE_BYTES:
            raise ValueError("Select fewer fields to stay within the 2 MiB response limit")

        def consume(records):
            nonlocal size
            for position, record in enumerate(records):
                if position < offset:
                    continue
                if len(result["rows"]) >= limit:
                    result["has_more"] = True
                    break
                allowance = min(MAX_CELL_BYTES, max(128, (MAX_RESPONSE_BYTES - size) // (len(columns) + 1)))
                values = [_bounded_value(value, [allowance]) for value in record]
                row_size = len(json.dumps(values, ensure_ascii=False).encode()) + 2
                if size + row_size >= MAX_RESPONSE_BYTES:
                    result.update(has_more=True, truncated=True)
                    if not result["rows"]:
                        raise ValueError("Select fewer fields to stay within the 2 MiB response limit")
                    break
                result["rows"].append(values)
                size += row_size
            else:
                if source == "csv":
                    result["cache_rows"] = position + 1 if "position" in locals() else 0

        if source == "csv":
            with open_cached_csv(path, newline="") as handle:
                reader = csv.reader(handle)
                if next(reader, []) != [column.name for column in available]:
                    raise ValueError("CSV schema changed while loading the preview. Reload the data source.")
                consume(
                    (
                        [row[int(column.id)] if int(column.id) < len(row) else None for column in columns]
                        for row in reader
                    )
                )
        else:
            consume(self._parquet_rows(path, episode, columns))
        result["next_offset"] = offset + len(result["rows"])
        return result

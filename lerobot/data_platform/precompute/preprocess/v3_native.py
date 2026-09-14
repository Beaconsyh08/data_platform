"""Episode-aware copies of v3 embedded-image datasets, without image re-encoding."""

from __future__ import annotations

import copy
import io
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from lerobot.data_platform.precompute.data_profile import (
    require_dataset_operation,
    resolve_data_profile,
    write_data_profile,
)
from lerobot.data_platform.precompute.dataset_io import V3DatasetMetadata, read_episode_table
from lerobot.data_platform.precompute.preprocess.common import PreprocessResult, emit, write_json
from lerobot.data_platform.precompute.preprocess.dataset_version import (
    DEFAULT_DATA_PATH,
    _write_episode_metadata,
    validate_v3_dataset,
)


def uses_native_images(info: dict) -> bool:
    features = info.get("features") or {}
    return (
        str(info.get("codebase_version", "")).removeprefix("v").startswith("3.")
        and any(f.get("dtype") == "image" for f in features.values())
        and not any(f.get("dtype") == "video" for f in features.values())
    )


def validate_native_schemas(metas: list[V3DatasetMetadata]) -> None:
    """Check Parquet headers too: declared features alone do not establish Arrow compatibility."""
    expected = None
    for meta in metas:
        paths = {meta.root / meta.get_data_file_path(index) for index in meta.episodes}
        for path in sorted(paths):
            schema = pq.read_schema(path)
            if expected is not None and not schema.equals(expected):
                raise ValueError(f"Native v3 Arrow schema mismatch: {path}")
            expected = schema


def _stats(values: np.ndarray) -> dict:
    if values.ndim == 1:
        values = values[:, None]
    return {
        "min": values.min(axis=0).tolist(),
        "max": values.max(axis=0).tolist(),
        "mean": values.mean(axis=0).tolist(),
        "std": values.std(axis=0).tolist(),
        "count": [len(values)],
    }


def _episode_stats(table: pa.Table, info: dict, previous: dict) -> dict:
    stats = copy.deepcopy(previous)
    for key, feature in info["features"].items():
        if key not in table.column_names:
            raise ValueError(f"Missing source column: {key}")
        if str(feature["dtype"]).startswith(("float", "int", "uint")):
            values = np.asarray(table[key].to_pylist(), dtype=np.float64)
            if not np.isfinite(values).all():
                raise ValueError(f"Non-finite source signal: {key}")
            stats[key] = _stats(values)
        elif feature["dtype"] == "image":
            # Existing image statistics remain valid: neither pixels nor episode membership changed.
            values = table[key].to_pylist()
            if any(not isinstance(v, dict) or not v.get("bytes") for v in values):
                raise ValueError(f"Native image rewrite requires embedded bytes: {key}")
            if key not in stats:
                images = np.stack(
                    [
                        np.asarray(Image.open(io.BytesIO(v["bytes"])).convert("RGB"), dtype=np.float64) / 255
                        for v in values
                    ]
                )
                stats[key] = {
                    name: getattr(images, name)(axis=(0, 1, 2)).reshape(3, 1, 1).tolist()
                    for name in ("min", "max", "mean", "std")
                }
                stats[key]["count"] = [len(values)]
    return stats


def rewrite_native_images(
    roots: list[Path],
    out_root: Path,
    selections: list[tuple[int, int]],
    *,
    op: str,
    dry_run: bool = False,
    progress_callback=None,
) -> PreprocessResult:
    """Selections contain (source position, episode index) in the intended output order."""
    out_root = Path(out_root).expanduser()
    if out_root.exists():
        raise FileExistsError(f"Output dataset already exists: {out_root}")
    for root in roots:
        require_dataset_operation(root, op)
        source = root.resolve()
        target = out_root.resolve()
        if target == source or source in target.parents or target in source.parents:
            raise ValueError("Output must be outside every source dataset")
    metas = [V3DatasetMetadata(f"local/{root.name}", root) for root in roots]
    if not selections:
        raise ValueError("No episodes selected")
    lineage = [
        {
            "source_position": position,
            "source_root": str(roots[position]),
            "source_episode_index": episode,
            "output_episode_index": i,
            "provenance_path": f"provenance/source_{position:03d}",
        }
        for i, (position, episode) in enumerate(selections)
    ]
    total = sum(int(metas[p].episodes[e]["length"]) for p, e in selections)
    result = PreprocessResult(
        op,
        roots,
        out_root,
        f"local/{out_root.name}",
        len(selections),
        total,
        dry_run,
        {"output_format": "v3.0", "native_images": True, "selected_episodes": len(selections)},
        lineage,
    )
    for meta in metas:
        validate_v3_dataset(meta.root)
    validate_native_schemas(metas)
    if dry_run:
        emit(progress_callback, status="done", message="Native v3 dry run complete")
        return result
    out_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out_root.name}.staging-", dir=out_root.parent) as temp:
        staging = Path(temp) / "dataset"
        staging.mkdir()
        info = copy.deepcopy(metas[0].info)
        tasks = []
        task_indices = {}
        episodes = []
        statistics = []
        data_metadata = []
        offset = 0
        for i, (position, episode_id) in enumerate(selections):
            meta = metas[position]
            original = meta.episodes[episode_id]
            table = read_episode_table(roots[position], meta, episode_id)
            n = len(table)
            if n != int(original["length"]) or n == 0:
                raise ValueError(f"Episode {episode_id} row count does not match metadata")
            timestamps = np.asarray(table["timestamp"].to_pylist())
            if not np.isfinite(timestamps).all() or np.any(np.diff(timestamps) <= 0):
                raise ValueError(f"Invalid episode timestamps: {episode_id}")
            if table["frame_index"].to_pylist() != list(range(n)):
                raise ValueError(f"Invalid frame_index in episode {episode_id}")
            source_tasks = table["task_index"].to_pylist()
            task_map = {}
            for task_id in dict.fromkeys(source_tasks):
                task = meta.tasks[int(task_id)]
                if task not in task_indices:
                    task_indices[task] = len(tasks)
                    tasks.append({"task_index": len(tasks), "task": task})
                task_map[task_id] = task_indices[task]
            replacements = {
                "episode_index": [i] * n,
                "index": list(range(offset, offset + n)),
                "task_index": [task_map[t] for t in source_tasks],
            }
            for key, values in replacements.items():
                idx = table.schema.get_field_index(key)
                table = table.set_column(idx, table.schema.field(idx), pa.array(values, type=table[key].type))
            chunk, file = divmod(i, int(info.get("chunks_size") or 1000))
            path = staging / DEFAULT_DATA_PATH.format(chunk_index=chunk, file_index=file)
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, path)
            if pq.read_table(path).equals(table) is False:
                raise ValueError(f"Output validation failed for episode {i}")
            record = {
                key: value
                for key, value in original.items()
                if not key.startswith(("data/", "meta/", "stats/"))
            }
            record.update(
                episode_index=i,
                length=n,
                dataset_from_index=offset,
                dataset_to_index=offset + n,
                tasks=list(dict.fromkeys(meta.tasks[int(t)] for t in source_tasks)),
            )
            episodes.append(record)
            data_metadata.append({"data/chunk_index": chunk, "data/file_index": file})
            statistics.append(
                {
                    "episode_index": i,
                    "stats": _episode_stats(table, info, meta.episodes_stats.get(episode_id, {})),
                }
            )
            offset += n
            emit(
                progress_callback,
                status="running",
                current=i + 1,
                total=len(selections),
                message=f"Copied native v3 episode {i + 1}/{len(selections)}",
            )
        info.update(
            total_episodes=len(episodes),
            total_frames=offset,
            total_tasks=len(tasks),
            splits={"train": f"0:{len(episodes)}"},
            data_path=DEFAULT_DATA_PATH,
            video_path=None,
        )
        info.pop("total_chunks", None)
        info.pop("total_videos", None)
        write_data_profile(staging, resolve_data_profile(roots[0]), info=info)
        write_json(staging / "meta/info.json", info)
        pq.write_table(pa.Table.from_pylist(tasks), staging / "meta/tasks.parquet")
        _write_episode_metadata(staging, episodes, statistics, data_metadata, [{} for _ in episodes])
        for position, root in enumerate(roots):
            provenance = staging / f"provenance/source_{position:03d}"
            provenance.mkdir(parents=True)
            for child in root.iterdir():
                if child.name in {
                    "data",
                    "meta",
                    "videos",
                    "vis",
                    "outputs",
                    "wandb",
                } or child.name.startswith("."):
                    continue
                target = provenance / child.name
                if child.is_symlink():
                    target.symlink_to(child.readlink(), target_is_directory=child.is_dir())
                elif child.is_dir():
                    shutil.copytree(child, target, symlinks=True)
                else:
                    shutil.copy2(child, target)
            # Preserve additional metadata as source provenance, without reusing stale episode indices.
            shutil.copytree(root / "meta", provenance / "meta", symlinks=True)
        write_json(staging / "meta" / f"preprocess_{op}.json", {**result.summary, "episode_lineage": lineage})
        validate_v3_dataset(staging)
        if out_root.exists():
            raise FileExistsError(f"Output dataset already exists: {out_root}")
        staging.rename(out_root)
    emit(
        progress_callback,
        status="done",
        current=len(selections),
        total=len(selections),
        message="Native v3 dataset committed",
    )
    return result

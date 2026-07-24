from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.data_platform.precompute.construction.types import ConstructionPlan


EXIST_LABEL_FEATURE = {"dtype": "int32", "shape": [1], "names": None}


def _emit(progress_callback: Callable[[dict], None] | None, **payload) -> None:
    if progress_callback is not None:
        progress_callback(payload)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _episode_chunk(info: dict, episode_index: int) -> int:
    return int(episode_index) // int(info.get("chunks_size") or 1000)


def _data_path(info: dict, episode_index: int) -> Path:
    pattern = info.get("data_path") or "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
    return Path(pattern.format(episode_chunk=_episode_chunk(info, episode_index), episode_index=episode_index))


def _video_path(info: dict, episode_index: int, video_key: str) -> Path | None:
    pattern = info.get("video_path")
    if not pattern:
        return None
    return Path(
        pattern.format(
            episode_chunk=_episode_chunk(info, episode_index),
            episode_index=episode_index,
            video_key=video_key,
        )
    )


def _set_column(table: pa.Table, name: str, values, arrow_type: pa.DataType) -> pa.Table:
    field = pa.field(name, arrow_type)
    column = pa.array(values, type=arrow_type)
    if name in table.column_names:
        return table.set_column(table.column_names.index(name), field, column)
    return table.append_column(field, column)


def _copy_or_link(src: Path, dst: Path) -> None:
    if not src.exists() or dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.hardlink_to(src)
    except OSError:
        shutil.copy2(src, dst)


def _link_tree(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.is_dir():
        return
    for src in src_dir.rglob("*"):
        if src.is_dir():
            continue
        _copy_or_link(src, dst_dir / src.relative_to(src_dir))


def _link_episode_videos(src_root: Path, out_root: Path, info: dict, src_episode: int, new_episode: int) -> None:
    video_keys = [key for key, ft in info.get("features", {}).items() if ft.get("dtype") == "video"]
    for video_key in video_keys:
        src_rel = _video_path(info, src_episode, video_key)
        dst_rel = _video_path(info, new_episode, video_key)
        if src_rel is None or dst_rel is None:
            continue
        _copy_or_link(src_root / src_rel, out_root / dst_rel)


def _feature_dim(feature: dict) -> int:
    shape = feature.get("shape") or [1]
    if isinstance(shape, int):
        return int(shape)
    if len(shape) == 0:
        return 1
    return int(shape[0])


def _numeric_stats(values: list, dim: int) -> dict:
    rows = []
    for value in values:
        if value is None:
            rows.append([np.nan] * dim)
        elif isinstance(value, (list, tuple)):
            padded = list(value)[:dim]
            rows.append(padded + [np.nan] * (dim - len(padded)))
        else:
            rows.append([value])
    arr = np.asarray(rows, dtype=np.float64)
    return {
        "min": np.nanmin(arr, axis=0).tolist(),
        "max": np.nanmax(arr, axis=0).tolist(),
        "mean": np.nanmean(arr, axis=0).tolist(),
        "std": np.nanstd(arr, axis=0).tolist(),
        "count": [int(arr.shape[0])],
    }


def _stats_from_table(table: pa.Table, features: dict) -> dict:
    stats = {}
    for key, feature in features.items():
        if key not in table.column_names:
            continue
        dtype = str(feature.get("dtype", ""))
        if dtype in {"image", "video", "string"}:
            continue
        if not dtype.startswith(("float", "int", "uint")) and dtype not in {"bool", "boolean"}:
            continue
        values = table.column(key).to_pylist()
        if values:
            stats[key] = _numeric_stats(values, _feature_dim(feature))
    return stats


def _prepare_table(
    src_table: pa.Table,
    *,
    new_episode_index: int,
    task_index: int,
    exist_label: int,
    global_offset: int,
) -> pa.Table:
    num_rows = src_table.num_rows
    table = src_table
    table = _set_column(table, "episode_index", [new_episode_index] * num_rows, pa.int64())
    table = _set_column(table, "index", list(range(global_offset, global_offset + num_rows)), pa.int64())
    table = _set_column(table, "task_index", [task_index] * num_rows, pa.int64())
    table = _set_column(table, "exist_label", [exist_label] * num_rows, pa.int32())
    return table


def _write_episode_table(out_root: Path, info: dict, episode_index: int, table: pa.Table) -> Path:
    out_path = out_root / _data_path(info, episode_index)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path)
    return out_path


def _task_rows_by_index(rows: list[dict]) -> dict[int, str]:
    return {int(row["task_index"]): str(row["task"]) for row in rows}


def write_synthetic_dataset(
    src_root: Path,
    plans: list[ConstructionPlan],
    out_root: Path,
    include_positives: bool,
    progress_callback: Callable[[dict], None] | None = None,
    source_repo_id: str | None = None,
) -> dict:
    """Build a relabeled dataset from real source episodes.

    Observation and action samples retain their source values. Construction rewrites bookkeeping
    indices, task metadata, and ``exist_label``; it does not fabricate sensor or action samples.
    """
    src_root = Path(src_root)
    out_root = Path(out_root)
    if out_root.exists() and any(out_root.iterdir()):
        raise FileExistsError(f"Output dataset already exists and is not empty: {out_root}")

    src_meta = src_root / "meta"
    info = json.loads((src_meta / "info.json").read_text())
    src_task_rows = _read_jsonl(src_meta / "tasks.jsonl")
    src_episode_rows = sorted(_read_jsonl(src_meta / "episodes.jsonl"), key=lambda row: int(row["episode_index"]))
    src_tasks = _task_rows_by_index(src_task_rows)
    task_to_index: dict[str, int] = {}
    out_task_rows: list[dict] = []
    out_episode_rows: list[dict] = []
    out_stats_rows: list[dict] = []

    if include_positives:
        for row in src_task_rows:
            task = str(row["task"])
            task_index = int(row["task_index"])
            task_to_index[task] = task_index
            out_task_rows.append({"task_index": task_index, "task": task})

    def task_index_for(task: str) -> int:
        if task not in task_to_index:
            task_to_index[task] = len(task_to_index)
            out_task_rows.append({"task_index": task_to_index[task], "task": task})
        return task_to_index[task]

    out_info = dict(info)
    out_info["features"] = dict(info.get("features", {}))
    out_info["features"]["exist_label"] = EXIST_LABEL_FEATURE

    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "meta").mkdir(parents=True, exist_ok=True)
    _link_tree(src_root / "images", out_root / "images")

    total_work = (len(src_episode_rows) if include_positives else 0) + len(plans)
    current = 0
    global_offset = 0
    src_episode_by_index = {int(row["episode_index"]): row for row in src_episode_rows}

    if include_positives:
        for new_idx, episode_row in enumerate(src_episode_rows):
            src_idx = int(episode_row["episode_index"])
            src_table = pq.read_table(src_root / _data_path(info, src_idx))
            task = (episode_row.get("tasks") or [src_tasks.get(0, "")])[0]
            table = _prepare_table(
                src_table,
                new_episode_index=new_idx,
                task_index=task_index_for(task),
                exist_label=1,
                global_offset=global_offset,
            )
            _write_episode_table(out_root, out_info, new_idx, table)
            _link_episode_videos(src_root, out_root, info, src_idx, new_idx)
            out_episode_rows.append({"episode_index": new_idx, "tasks": episode_row.get("tasks", []), "length": table.num_rows})
            out_stats_rows.append({"episode_index": new_idx, "stats": _stats_from_table(table, out_info["features"])})
            global_offset += table.num_rows
            current += 1
            _emit(progress_callback, status="running", step="construction_write", current=current, total=total_work, message=f"Copied positive episode {src_idx}")

    negative_offset = len(out_episode_rows)
    for i, plan in enumerate(plans):
        src_row = src_episode_by_index.get(int(plan.src_episode_index))
        if src_row is None:
            logging.warning("Skipping construction plan with missing source episode %s", plan.src_episode_index)
            continue
        new_idx = negative_offset + i
        plan.new_episode_index = new_idx
        src_table = pq.read_table(src_root / _data_path(info, plan.src_episode_index))
        table = _prepare_table(
            src_table,
            new_episode_index=new_idx,
            task_index=task_index_for(plan.new_task),
            exist_label=0,
            global_offset=global_offset,
        )
        _write_episode_table(out_root, out_info, new_idx, table)
        _link_episode_videos(src_root, out_root, info, plan.src_episode_index, new_idx)
        out_episode_rows.append({"episode_index": new_idx, "tasks": [plan.new_task], "length": table.num_rows})
        out_stats_rows.append({"episode_index": new_idx, "stats": _stats_from_table(table, out_info["features"])})
        global_offset += table.num_rows
        current += 1
        _emit(progress_callback, status="running", step="construction_write", current=current, total=total_work, message=f"Constructed negative episode {new_idx}")

    out_info["total_episodes"] = len(out_episode_rows)
    out_info["total_frames"] = global_offset
    out_info["total_tasks"] = len(out_task_rows)
    out_info["total_chunks"] = max(1, _episode_chunk(out_info, max(0, len(out_episode_rows) - 1)) + 1)
    out_info["splits"] = {"train": f"0:{len(out_episode_rows)}"}
    video_keys = [key for key, ft in out_info["features"].items() if ft.get("dtype") == "video"]
    out_info["total_videos"] = len(out_episode_rows) * len(video_keys)

    (out_root / "meta" / "info.json").write_text(json.dumps(out_info, indent=2, ensure_ascii=False))
    _write_jsonl(out_root / "meta" / "tasks.jsonl", sorted(out_task_rows, key=lambda row: int(row["task_index"])))
    _write_jsonl(out_root / "meta" / "episodes.jsonl", out_episode_rows)
    _write_jsonl(out_root / "meta" / "episodes_stats.jsonl", out_stats_rows)
    (out_root / "meta" / "construction_plan.json").write_text(
        json.dumps(
            {
                "source_root": str(src_root),
                "source_repo_id": source_repo_id or f"local/{src_root.name}",
                "include_positives": include_positives,
                "records": [asdict(plan) for plan in plans],
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    _emit(progress_callback, status="running", step="construction_write", current=total_work, total=total_work, message="Constructed dataset written")
    return {
        "out_root": out_root,
        "episodes": len(out_episode_rows),
        "negative_episodes": len(plans),
        "positive_episodes": len(src_episode_rows) if include_positives else 0,
        "plans": plans,
    }

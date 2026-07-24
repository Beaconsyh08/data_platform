import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.data_platform.precompute.preprocess.common import (
    PreprocessResult,
    ProgressCallback,
    copy_meta_files,
    copy_sidecar_dirs,
    default_preprocess_path,
    emit,
    ensure_output_root,
    load_json,
    load_jsonl,
    parquet_paths,
    validate_dataset_root,
    write_json,
    write_jsonl,
)


ACTION_COLUMN = "action"
STATE_COLUMN = "state"
SMOOTH_ACTION_META = "preprocess_smooth_action.json"


def _validate_window(window: int) -> int:
    window = int(window)
    if window < 1:
        raise ValueError("smoothing window must be >= 1")
    if window % 2 == 0:
        raise ValueError("smoothing window must be odd so the filter is centered")
    return window


def _smooth_array(values: list, window: int) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float32)
    if arr.ndim < 2:
        raise ValueError("action column must contain per-frame vectors")
    if window == 1 or arr.shape[0] <= 1:
        return arr
    pad = window // 2
    pad_width = [(pad, pad)] + [(0, 0)] * (arr.ndim - 1)
    padded = np.pad(arr, pad_width, mode="edge")
    cumsum = np.cumsum(padded, axis=0, dtype=np.float64)
    zero = np.zeros_like(cumsum[:1])
    cumsum = np.concatenate([zero, cumsum], axis=0)
    smoothed = (cumsum[window:] - cumsum[:-window]) / float(window)
    return smoothed.astype(np.float32, copy=False)


def _column_stats(values: np.ndarray) -> dict:
    return {
        "min": values.min(axis=0).tolist(),
        "max": values.max(axis=0).tolist(),
        "mean": values.mean(axis=0).tolist(),
        "std": values.std(axis=0).tolist(),
        "count": [int(values.shape[0])],
    }


def _episode_index(table: pa.Table, fallback: int) -> int:
    if "episode_index" not in table.column_names or table.num_rows == 0:
        return fallback
    return int(table["episode_index"][0].as_py())


def _fallback_episode_index(path: Path) -> int:
    stem = path.stem
    if stem.startswith("episode_"):
        return int(stem.removeprefix("episode_"))
    raise ValueError(f"Cannot infer episode index from parquet path: {path}")


def _rewrite_parquet(src: Path, dst: Path, window: int, columns_to_smooth: tuple[str, ...]) -> tuple[int, dict[str, dict]]:
    table = pq.read_table(src)
    if ACTION_COLUMN not in table.column_names:
        raise ValueError(f"Missing action column in {src}")

    smoothed_by_column = {}
    stats_by_column = {}
    for column in columns_to_smooth:
        if column not in table.column_names:
            continue
        smoothed = _smooth_array(table[column].to_pylist(), window)
        smoothed_by_column[column] = smoothed
        stats_by_column[column] = _column_stats(smoothed)

    arrays = []
    fields = []
    for field in table.schema:
        if field.name in smoothed_by_column:
            arrays.append(pa.array(smoothed_by_column[field.name].tolist(), type=field.type))
            fields.append(field)
        else:
            arrays.append(table[field.name])
            fields.append(field)

    dst.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_arrays(arrays, schema=pa.schema(fields, metadata=table.schema.metadata)), dst)
    return _episode_index(table, _fallback_episode_index(src)), stats_by_column


def _rewrite_parquet_worker(args: tuple[str, str, int, tuple[str, ...]]) -> tuple[int, dict[str, dict]]:
    src, dst, window, columns_to_smooth = args
    return _rewrite_parquet(Path(src), Path(dst), window, columns_to_smooth)


def _resolve_workers(workers: int | None, total: int) -> int:
    if total <= 1:
        return 1
    if workers is None or int(workers) == 0:
        return max(1, min(os.cpu_count() or 1, total, 8))
    workers = int(workers)
    if workers < 1:
        raise ValueError("workers must be 0 for auto or >= 1")
    return min(workers, total)


def _rewrite_stats_file(src: Path, dst: Path, stats_by_episode: dict[int, dict[str, dict]]) -> None:
    rows = load_jsonl(src)
    if not rows:
        rows = [
            {"episode_index": episode_index, "stats": stats}
            for episode_index, stats in sorted(stats_by_episode.items())
        ]
    else:
        for row in rows:
            episode_index = int(row["episode_index"])
            if episode_index not in stats_by_episode:
                continue
            stats = row.setdefault("stats", {})
            stats.update(stats_by_episode[episode_index])
    write_jsonl(dst, rows)


def run_smooth_action(
    src_root: Path,
    out_root: Path | None = None,
    window: int = 5,
    workers: int | None = 0,
    smooth_state: bool = True,
    dry_run: bool = False,
    progress_callback: ProgressCallback = None,
) -> PreprocessResult:
    src_root = validate_dataset_root(src_root)
    window = _validate_window(window)
    out_root = ensure_output_root(out_root or default_preprocess_path(src_root, f"smooth_action_w{window}"), dry_run)
    paths = parquet_paths(src_root)
    if not paths:
        raise FileNotFoundError(f"No parquet files found under {src_root / 'data'}")
    worker_count = _resolve_workers(workers, len(paths))

    info = load_json(src_root / "meta" / "info.json")
    features = info.get("features") or {}
    if ACTION_COLUMN not in features:
        raise ValueError("Dataset metadata does not define an action feature")
    columns_to_smooth = [ACTION_COLUMN]
    if smooth_state and STATE_COLUMN in features:
        columns_to_smooth.append(STATE_COLUMN)
    columns_to_smooth_tuple = tuple(columns_to_smooth)

    result = PreprocessResult(
        op=f"smooth_action_w{window}",
        src_roots=[src_root],
        out_root=out_root,
        repo_id=f"local/{out_root.name}",
        total_episodes=int(info.get("total_episodes") or len(paths)),
        total_frames=int(info.get("total_frames") or 0),
        dry_run=dry_run,
        summary={"window": window, "workers": worker_count, "fields": columns_to_smooth},
    )
    emit(progress_callback, status="running", current=0, total=len(paths), message=f"Planning action smoothing: {result.summary}")
    if dry_run:
        emit(progress_callback, status="done", current=0, total=len(paths), message="Dry run complete")
        return result

    (out_root / "data").mkdir(parents=True, exist_ok=True)
    copy_meta_files(src_root, out_root)
    copy_sidecar_dirs(src_root, out_root)

    tasks = [
        (str(src), str(out_root / "data" / src.relative_to(src_root / "data")), window, columns_to_smooth_tuple)
        for src in paths
    ]
    stats_by_episode: dict[int, dict[str, dict]] = {}
    if worker_count == 1:
        for idx, task in enumerate(tasks, start=1):
            episode_index, stats = _rewrite_parquet_worker(task)
            stats_by_episode[episode_index] = stats
            emit(progress_callback, status="running", current=idx, total=len(paths), message=f"Smoothed action parquet {idx}/{len(paths)}")
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_rewrite_parquet_worker, task) for task in tasks]
            for idx, future in enumerate(as_completed(futures), start=1):
                episode_index, stats = future.result()
                stats_by_episode[episode_index] = stats
                emit(
                    progress_callback,
                    status="running",
                    current=idx,
                    total=len(paths),
                    message=f"Smoothed parquet {idx}/{len(paths)} with {worker_count} workers",
                )

    _rewrite_stats_file(
        src_root / "meta" / "episodes_stats.jsonl",
        out_root / "meta" / "episodes_stats.jsonl",
        stats_by_episode,
    )
    write_json(
        out_root / "meta" / SMOOTH_ACTION_META,
        {
            "op": "smooth_action",
            "field": ACTION_COLUMN,
            "fields": columns_to_smooth,
            "source_root": str(src_root),
            "output_root": str(out_root),
            "window": window,
            "workers": worker_count,
            "smooth_state": STATE_COLUMN in columns_to_smooth,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    emit(progress_callback, status="done", current=len(paths), total=len(paths), message=f"Action smoothing complete: {out_root}")
    return result

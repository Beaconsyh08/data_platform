"""Reproducible tiny data; experiment authorization never accepts arbitrary business roots."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from functools import lru_cache
from pathlib import Path

from lerobot.data_platform.task_catalog import content_digest


def inventory(root):
    root = Path(root)
    result = {}
    for visited, path in enumerate(root.rglob("*")):
        if visited >= 1000:
            raise ValueError("Synthetic fixture has too many directory entries")
        if path.is_symlink():
            raise ValueError("Synthetic fixtures must not contain symlinks")
        if path.is_file():
            if path.stat().st_size > 8 * 1024 * 1024 or len(result) >= 100:
                raise ValueError("Synthetic fixture exceeds its size bound")
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def create_fixture(root, fmt):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    with_video = fmt == "v2.1-video"
    fmt = "v2.1" if with_video else fmt
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    (root / "meta").mkdir()
    (root / "data/chunk-000").mkdir(parents=True)
    n = 32
    values = [[float(i) / 32, float(i**2) / 1024] for i in range(n)]
    features = {
        k: {"dtype": "float32", "shape": [2], "names": ["joint_a", "joint_b"]}
        for k in ("action", "observation.state")
    }
    for key in ("timestamp", "episode_index", "frame_index", "index", "task_index"):
        features[key] = {"dtype": "float32" if key == "timestamp" else "int64", "shape": [1]}
    table = pa.table(
        {
            "action": values,
            "observation.state": values,
            "timestamp": np.tile(np.arange(16) / 10, 2),
            "episode_index": np.repeat([0, 1], 16),
            "frame_index": np.tile(np.arange(16), 2),
            "index": np.arange(n),
            "task_index": np.zeros(n, dtype=int),
        }
    )
    info = {
        "robot_type": "synthetic",
        "codebase_version": fmt,
        "fps": 10,
        "total_episodes": 2,
        "total_frames": n,
        "total_tasks": 1,
        "chunks_size": 1000,
        "features": features,
        "splits": {"train": "0:2"},
    }
    episodes = [{"episode_index": i, "length": 16, "tasks": ["Synthetic motion"]} for i in range(2)]

    def stats_for(part):
        result = {}
        for name in part.column_names:
            values = np.asarray(part[name].to_pylist())
            if values.ndim == 1:
                values = values[:, None]
            result[name] = {
                "min": values.min(axis=0).tolist(),
                "max": values.max(axis=0).tolist(),
                "mean": values.mean(axis=0).tolist(),
                "std": values.std(axis=0).tolist(),
                "count": [len(values)],
            }
        return result

    episode_stats = [stats_for(table.slice(i * 16, 16)) for i in range(2)]
    if fmt == "v2.1":
        info["data_path"] = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
        for i in range(2):
            pq.write_table(table.slice(i * 16, 16), root / f"data/chunk-000/episode_{i:06d}.parquet")
        (root / "meta/episodes.jsonl").write_text("".join(json.dumps(e) + "\n" for e in episodes))
        (root / "meta/episodes_stats.jsonl").write_text(
            "".join(json.dumps({"episode_index": i, "stats": episode_stats[i]}) + "\n" for i in range(2))
        )
        (root / "meta/tasks.jsonl").write_text(
            json.dumps({"task_index": 0, "task": "Synthetic motion"}) + "\n"
        )
    elif fmt == "v3.0":
        info["data_path"] = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
        pq.write_table(table, root / "data/chunk-000/file-000.parquet")
        (root / "meta/episodes/chunk-000").mkdir(parents=True)
        for i, episode in enumerate(episodes):
            episode.update(
                {
                    f"stats/{feature}/{metric}": data
                    for feature, metrics in episode_stats[i].items()
                    for metric, data in metrics.items()
                }
            )
            episode.update(
                {
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                    "dataset_from_index": i * 16,
                    "dataset_to_index": (i + 1) * 16,
                }
            )
        pq.write_table(pa.Table.from_pylist(episodes), root / "meta/episodes/chunk-000/file-000.parquet")
        pq.write_table(
            pa.table({"task_index": [0], "task": ["Synthetic motion"]}), root / "meta/tasks.parquet"
        )
    else:
        raise ValueError("Unsupported synthetic format")
    if with_video:
        import av

        key = "observation.images.camera"
        info["features"][key] = {
            "dtype": "video",
            "shape": [16, 16, 3],
            "names": ["height", "width", "channels"],
        }
        info["video_path"] = "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
        info["total_videos"] = 2
        for episode in range(2):
            path = root / "videos/chunk-000" / key / f"episode_{episode:06d}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            with av.open(str(path), mode="w") as container:
                stream = container.add_stream("libx264", rate=10)
                stream.width, stream.height, stream.pix_fmt = 16, 16, "yuv420p"
                # Initialize plane padding too: uninitialized conversion buffers can affect
                # encoded bytes even when decoded pixels agree. Authorization compares bytes.
                stream.codec_context.thread_count = 1
                stream.options = {"preset": "ultrafast", "crf": "0", "threads": "1"}
                for index in range(16):
                    frame = av.VideoFrame(16, 16, "yuv420p")
                    for channel, plane in enumerate(frame.planes):
                        value = episode * 96 + index * 4 if channel == 0 else 128
                        plane.update(bytes([value]) * plane.buffer_size)
                    frame.pts = index
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
    (root / "meta/info.json").write_text(json.dumps(info, sort_keys=True))
    (root / "meta/stats.json").write_text(json.dumps(stats_for(table), sort_keys=True))
    return root


@lru_cache(maxsize=3)
def expected_inventory(fmt):
    with tempfile.TemporaryDirectory(prefix="strategy-reference-") as directory:
        return inventory(create_fixture(Path(directory) / "fixture", fmt))


def validate_fixture(root, fmt):
    path = Path(root)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError("Use a real absolute synthetic fixture root")
    actual = inventory(path)
    if actual != expected_inventory(fmt):
        raise ValueError("The test location is not an unchanged generated synthetic fixture")
    return content_digest(actual)


def verify_output(root, source_root, *, expected_episodes, expected_format):
    import numpy as np

    from lerobot.data_platform.precompute.dataset_io import (
        load_episode_records,
        load_task_records,
        read_episode_table,
    )

    root, source_root = Path(root), Path(source_root)
    info = json.loads((root / "meta/info.json").read_text())
    episodes = load_episode_records(root)
    if (
        info["codebase_version"] != expected_format
        or len(episodes) != len(expected_episodes)
        or info["total_episodes"] != len(episodes)
    ):
        raise ValueError("Unexpected output metadata")
    source_features = json.loads((source_root / "meta/info.json").read_text())["features"]
    output_features = {
        k: {field: value for field, value in f.items() if field != "fps"} for k, f in info["features"].items()
    }
    if output_features != source_features:
        raise ValueError("Output feature schema changed")
    from lerobot.data_platform.dataset_metadata import MetaOnlyDataset

    meta = MetaOnlyDataset("synthetic/output", root=root).meta
    source_meta = MetaOnlyDataset("synthetic/source", root=source_root).meta
    numeric_features = [name for name, feature in info["features"].items() if feature["dtype"] != "video"]
    video_features = [name for name, feature in info["features"].items() if feature["dtype"] == "video"]

    def video_frames(dataset_root, metadata, episode, key):
        import av

        path = dataset_root / metadata.get_video_file_path(episode, key)
        if not path.is_file() or not path.resolve().is_relative_to(dataset_root.resolve()):
            raise ValueError("Invalid output video reference")
        bounds = (
            metadata.get_video_time_range(episode, key) if getattr(metadata, "is_v3", False) else (0, 1.6)
        )
        with av.open(str(path)) as container:
            return [
                frame.to_ndarray(format="rgb24")
                for frame in container.decode(video=0)
                if bounds[0] - 1e-6 <= float(frame.time) < bounds[1] - 1e-6
            ]

    def check_stats(record, name, values):
        if values.ndim == 1:
            values = values[:, None]
        for metric, expected in (
            ("min", values.min(axis=0)),
            ("max", values.max(axis=0)),
            ("mean", values.mean(axis=0)),
            ("std", values.std(axis=0)),
        ):
            if not np.allclose(record[name][metric], expected, atol=1e-6):
                raise ValueError("Output statistics are inconsistent")
        if int(np.asarray(record[name]["count"]).flat[0]) != len(values):
            raise ValueError("Output statistics count is inconsistent")

    total = 0
    for index, source_index in enumerate(expected_episodes):
        frame = read_episode_table(root, meta, index).to_pandas()
        original = read_episode_table(source_root, source_meta, source_index).to_pandas()
        if len(frame) != len(original) or len(frame) != episodes[index]["length"]:
            raise ValueError("Output row count changed")
        for name in ("action", "observation.state", "timestamp", "task_index"):
            if not np.array_equal(np.asarray(frame[name].tolist()), np.asarray(original[name].tolist())):
                raise ValueError("Untouched observation, action, task or timestamp changed")
        if (
            list(frame["episode_index"]) != [index] * len(frame)
            or list(frame["frame_index"]) != list(range(len(frame)))
            or list(frame["index"]) != list(range(total, total + len(frame)))
        ):
            raise ValueError("Output index fields are inconsistent")
        for name in numeric_features:
            check_stats(meta.episodes_stats[index], name, np.asarray(frame[name].tolist()))
        for key in video_features:
            actual = video_frames(root, meta, index, key)
            original_frames = video_frames(source_root, source_meta, source_index, key)
            if (
                len(actual) != len(frame)
                or len(actual) != len(original_frames)
                or any(not np.array_equal(a, b) for a, b in zip(actual, original_frames, strict=True))
            ):
                raise ValueError("Output video frames or time references changed")
        total += len(frame)
    if info["total_frames"] != total or not load_task_records(root) or not meta.stats:
        raise ValueError("Output tasks or statistics are missing")
    for name in numeric_features:
        values = np.concatenate(
            [np.asarray(read_episode_table(root, meta, i)[name].to_pylist()) for i in range(len(episodes))]
        )
        check_stats(meta.stats, name, values)
    return {
        "rows": total,
        "episodes": len(episodes),
        "format": expected_format,
        "video_streams": len(video_features),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    for fmt in ("v2.1", "v3.0", "v2.1-video"):
        root = create_fixture(args.root / fmt, fmt)
        print(fmt, validate_fixture(root.resolve(), fmt))


if __name__ == "__main__":
    main()

"""Standalone worker: run by the isolated official LeRobot runtime, never as a platform module.

The sibling contract has no platform dependencies. Do not import lerobot.data_platform here:
the lerobot package in this interpreter is the pinned upstream v3 writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import queue
import shutil
import sys
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from fractions import Fraction
from pathlib import Path

from mcap_contract import McapImportConfig

SIDES = ("left", "right")
TOPICS = {
    f"{side}_{kind}": f"/robot{robot}/{suffix}"
    for robot, side in enumerate(SIDES)
    for kind, suffix in (
        ("image", "sensor/camera0/compressed"),
        ("pose", "vio/eef_pose"),
        ("width", "sensor/magnetic_encoder"),
    )
}
PROFILE = {
    "schema_version": 2,
    "robot_profile": "genrobot_das",
    "signal_schema": "genrobot_das_bimanual_eef_v1",
    "gripper_encoding": "width_m",
    "stage_profile": "",
    "legacy_data_version": None,
    "resolution_source": "genrobot_mcap_importer",
    "confirmed": True,
}


_EVENT_QUEUE = None
_STOP_EVENT = None


def stop_check():
    if _STOP_EVENT is not None and _STOP_EVENT.is_set():
        raise InterruptedError("Import stopped")
    stop = os.environ.get("DATA_PLATFORM_EXECUTION_STOP_FILE")
    if stop and Path(stop).exists():
        raise InterruptedError("Import stopped")


def emit(phase, **values):
    stop_check()
    event = {"phase": phase, **values}
    if _EVENT_QUEUE is not None:
        while True:
            stop_check()
            try:
                _EVENT_QUEUE.put(event, timeout=0.1)
                return
            except queue.Full:
                continue
    print(json.dumps(event), flush=True)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            stop_check()
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_manifest(root):
    return {
        str(path.relative_to(root)): file_hash(path) for path in sorted(root.rglob("*")) if path.is_file()
    }


def load_checkpoint(root, receipt):
    if not root.is_dir() or not receipt.is_file():
        return None
    try:
        record = json.loads(receipt.read_text())
        if record.get("manifest") == checkpoint_manifest(root):
            return record["lineage"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def messages(path, topics):
    from mcap.reader import make_reader
    from mcap_protobuf.decoder import DecoderFactory

    with path.open("rb") as handle:
        # A fresh factory per file prevents schema-id collisions between recordings.
        reader = make_reader(handle, decoder_factories=[DecoderFactory()])
        for _, channel, message, decoded in reader.iter_decoded_messages(topics=topics, log_time_order=False):
            stop_check()
            yield channel.topic, message, decoded


def image_frames(path, topic):
    import av

    decoder = av.CodecContext.create("h264", "r")
    for _, _, msg in messages(path, [topic]):
        if msg.format.lower() != "h264":
            raise ValueError("GenDAS requires H.264 camera streams")
        packet = av.Packet(msg.data)
        packet.pts = packet.dts = int(msg.header.timestamp)
        packet.time_base = Fraction(1, 1_000_000_000)
        try:
            frames = decoder.decode(packet)
        except av.error.InvalidDataError:
            # Prefixes before the first decodable keyframe are outside valid coverage.
            continue
        for frame in frames:
            if frame.pts is None:
                raise ValueError("Decoded video frame has no source timestamp")
            yield int(frame.pts), frame
    for frame in decoder.decode(None):
        if frame.pts is None:
            raise ValueError("Decoded video frame has no source timestamp")
        yield int(frame.pts), frame


def resized_shape(width, height, short_side):
    scale = short_side / min(width, height)
    content = (round(width * scale), round(height * scale))
    return content, tuple(size + size % 2 for size in content)


def nearest_indices(times, query):
    import numpy as np

    right = np.clip(np.searchsorted(times, query), 0, len(times) - 1)
    left = np.maximum(right - 1, 0)
    return np.where(abs(times[left] - query) <= abs(times[right] - query), left, right)


def inspect_file(path, config):
    import av
    import numpy as np

    streams = {key: {"times": [], "values": []} for key in TOPICS}
    names = {topic: key for key, topic in TOPICS.items()}
    frames = {}
    calibration = {}
    camera_topics = [f"/robot{i}/sensor/camera0/camera_info" for i in range(2)]
    for topic, _, msg in messages(path, [*TOPICS.values(), *camera_topics]):
        if topic in camera_topics:
            calibration[topic] = str(msg)
            continue
        key = names[topic]
        stream = streams[key]
        stamp = int(msg.header.timestamp)
        if stamp <= 0 or (stream["times"] and stamp <= stream["times"][-1]):
            raise ValueError(f"Non-increasing or missing capture timestamps: {topic}")
        stream["times"].append(stamp)
        if key.endswith("pose"):
            p, q = msg.pose.position, msg.pose.orientation
            stream["values"].append([p.x, p.y, p.z, q.x, q.y, q.z, q.w])
            frame_id = str(msg.frame_id)
            if key in frames and frames[key] != frame_id:
                raise ValueError(f"Coordinate frame changes within {topic}")
            frames[key] = frame_id
        elif key.endswith("width"):
            stream["values"].append([msg.value])
        elif msg.format.lower() != "h264":
            raise ValueError(f"Unsupported camera codec: {msg.format}")
    missing = [TOPICS[key] for key, stream in streams.items() if len(stream["times"]) < 2]
    if missing:
        raise ValueError(f"Missing required signals: {', '.join(missing)}")
    shapes = {}
    first_decoded = {}
    for side in SIDES:
        iterator = image_frames(path, TOPICS[f"{side}_image"])
        try:
            stamp, frame = next(iterator)
        except (StopIteration, av.error.FFmpegError) as exc:
            raise ValueError(f"Cannot decode {side} camera") from exc
        finally:
            iterator.close()
        shapes[side] = [frame.width, frame.height]
        first_decoded[side] = stamp
    for stream in streams.values():
        stream["times"] = np.asarray(stream["times"], dtype=np.int64)
        stream["values"] = np.asarray(stream["values"], dtype=np.float64)
    start = max([int(s["times"][0]) for s in streams.values()] + list(first_decoded.values()))
    end = min(int(s["times"][-1]) for s in streams.values())
    count = max(0, (end - start) * config.fps // 1_000_000_000 + 1)
    grid = start + np.arange(count, dtype=np.int64) * 1_000_000_000 // config.fps
    valid = np.ones(count, dtype=bool)
    selected = {}
    brackets = {}
    for key, stream in streams.items():
        ts, values = stream["times"], stream["values"]
        if key.endswith("image"):
            indices = nearest_indices(ts, grid)
            selected[key] = indices
            valid &= abs(ts[indices] - grid) <= config.image_tolerance_ms * 1_000_000
            continue
        right = np.clip(np.searchsorted(ts, grid, side="right"), 1, len(ts) - 1)
        left = right - 1
        good = np.isfinite(values).all(axis=1)
        if key.endswith("pose"):
            norm = np.linalg.norm(values[:, 3:], axis=1)
            good &= (norm > 0.99) & (norm < 1.01)
        else:
            # Recorded upper stops are approximately 0.103001997 m; retain their precision.
            good &= (values[:, 0] >= -1e-5) & (values[:, 0] <= 0.10301)
        valid &= good[left] & good[right]
        valid &= ts[right] - ts[left] <= config.interpolation_gap_ms * 1_000_000
        brackets[key] = (left, right)
    cuts = np.flatnonzero(np.diff(np.r_[False, valid, False])).reshape(-1, 2)
    segments = [(int(a), int(b)) for a, b in cuts if b - a >= config.min_frames]
    if not segments:
        raise ValueError("No continuous interval contains all required signals")
    total_frames = sum(b - a for a, b in segments)
    image_start = min(int(streams[f"{s}_image"]["times"][0]) for s in SIDES)
    image_end = max(int(streams[f"{s}_image"]["times"][-1]) for s in SIDES)
    summary = {
        "file": path.name,
        "source_bytes": path.stat().st_size,
        "source_duration_s": (image_end - image_start) / 1e9,
        "valid_duration_s": total_frames / config.fps,
        "frames": total_frames,
        "segments": [
            {"start_ns": int(grid[a]), "last_frame_ns": int(grid[b - 1]), "frames": b - a}
            for a, b in segments
        ],
        "source_shapes": shapes,
        "output_shapes": {
            side: list(resized_shape(*shape, config.short_side)[1]) for side, shape in shapes.items()
        },
        "signal_counts": {key: len(stream["times"]) for key, stream in streams.items()},
        "coordinate_frames": frames,
        "shared_frame_verified": False,
        "calibration": calibration,
        "calibration_verified": False,
        "trimmed": total_frames < min(len(streams[f"{s}_image"]["times"]) for s in SIDES),
    }
    return summary, streams, grid, segments, selected, brackets


def feature_schema(summary):
    features = {}
    for side in SIDES:
        width, height = summary["output_shapes"][side]
        features[f"observation.images.{side}_wrist"] = {
            "dtype": "video",
            "shape": (height, width, 3),
            "names": ["height", "width", "channels"],
        }
        features[f"{side}_arm_quaternion_pose"] = {
            "dtype": "float32",
            "shape": (7,),
            "names": ["x", "y", "z", "qx", "qy", "qz", "qw"],
        }
        features[f"{side}_gripper_width"] = {"dtype": "float32", "shape": (1,), "names": ["width"]}
    return features


def blocking_encoder(fps):
    import queue

    from lerobot.datasets.video_utils import StreamingVideoEncoder

    class OfflineEncoder(StreamingVideoEncoder):
        def feed_frame(self, video_key, image):
            # Upstream's capture-time queue drops frames after 100 ms. Offline imports must wait.
            copied = image.copy()
            while True:
                stop_check()
                if not self._threads[video_key].is_alive():
                    raise RuntimeError(f"Video encoder exited: {video_key}")
                try:
                    self._frame_queues[video_key].put(copied, timeout=0.1)
                    return
                except queue.Full:
                    continue

    return OfflineEncoder(
        fps=fps,
        vcodec="libsvtav1",
        pix_fmt="yuv420p",
        g=2,
        crf=30,
        preset=12,
        queue_maxsize=8,
        encoder_threads=1,
    )


def selected_images(path, topic, timestamps, shape, short_side):
    import cv2
    import numpy as np

    iterator = image_frames(path, topic)
    current = None
    try:
        for target in timestamps:
            while current is None or current[0] < target:
                current = next(iterator)
            if current[0] != target:
                raise ValueError(f"Missing decoded frame at source timestamp {target}: {topic}")
            content, output = resized_shape(current[1].width, current[1].height, short_side)
            if list(output) != shape:
                raise ValueError("Video shape changes within the input")
            rgb = cv2.resize(current[1].to_ndarray(format="rgb24"), content, interpolation=cv2.INTER_AREA)
            yield np.pad(rgb, ((0, output[1] - content[1]), (0, output[0] - content[0]), (0, 0)))
    except StopIteration as exc:
        raise ValueError(f"Video ended before the required timestamp: {topic}") from exc
    finally:
        iterator.close()


def write_file(path, destination, config, inspected, task):
    import numpy as np
    from scipy.spatial.transform import Rotation, Slerp

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    summary, streams, grid, segments, selected, brackets = inspected
    dataset = LeRobotDataset.create(
        repo_id=f"local/{destination.name}",
        root=destination,
        fps=config.fps,
        features=feature_schema(summary),
        robot_type="genrobot_das",
        streaming_encoding=False,
    )
    dataset._streaming_encoder = blocking_encoder(config.fps)
    kept = np.concatenate([np.arange(a, b) for a, b in segments])
    images = {
        side: selected_images(
            path,
            TOPICS[f"{side}_image"],
            streams[f"{side}_image"]["times"][selected[f"{side}_image"][kept]],
            summary["output_shapes"][side],
            config.short_side,
        )
        for side in SIDES
    }
    lineage = []
    try:
        for segment_no, (a, b) in enumerate(segments):
            columns = {}
            mapping = {"source_timestamp_ns": grid[a:b].tolist()}
            for side in SIDES:
                for kind in ("pose", "width"):
                    key = f"{side}_{kind}"
                    stream = streams[key]
                    lo, hi = [values[a:b] for values in brackets[key]]
                    t0, t1 = stream["times"][lo], stream["times"][hi]
                    alpha = (grid[a:b] - t0) / (t1 - t0)
                    values = (
                        stream["values"][lo] * (1 - alpha[:, None]) + stream["values"][hi] * alpha[:, None]
                    )
                    if kind == "pose":
                        # Interpolate each valid bracket; never pass unrelated invalid samples to Rotation.
                        for i in range(len(values)):
                            values[i, 3:] = Slerp(
                                [0, 1], Rotation.from_quat(stream["values"][[lo[i], hi[i]], 3:])
                            )([alpha[i]]).as_quat()[0]
                    columns[key] = values.astype(np.float32)
                    mapping[f"{key}_before_ns"] = t0.tolist()
                    mapping[f"{key}_after_ns"] = t1.tolist()
                mapping[f"{side}_image_ns"] = streams[f"{side}_image"]["times"][
                    selected[f"{side}_image"][a:b]
                ].tolist()
            for index in range(b - a):
                frame = {"task": task}
                for side in SIDES:
                    frame[f"observation.images.{side}_wrist"] = next(images[side])
                    frame[f"{side}_arm_quaternion_pose"] = columns[f"{side}_pose"][index]
                    frame[f"{side}_gripper_width"] = columns[f"{side}_width"][index]
                dataset.add_frame(frame)
                if index % 300 == 0:
                    emit("converting", file=path.name, frame=index, segment_frames=b - a)
            dataset.save_episode()
            import pyarrow as pa
            import pyarrow.parquet as pq

            provenance = destination / "provenance"
            provenance.mkdir(exist_ok=True)
            pq.write_table(pa.table(mapping), provenance / f"alignment_{segment_no:06d}.parquet")
            lineage.append(
                {"episode_index": segment_no, "source_file": path.name, **summary["segments"][segment_no]}
            )
        dataset.finalize()
    finally:
        for iterator in images.values():
            iterator.close()
        dataset.finalize()
    return lineage


def validate_videos(root):
    import av
    import numpy as np
    import pandas as pd
    import pyarrow.parquet as pq

    episodes = pd.concat(
        [pd.read_parquet(p) for p in sorted((root / "meta/episodes").rglob("*.parquet"))], ignore_index=True
    )
    info = json.loads((root / "meta/info.json").read_text())
    data_count = sum(pq.read_metadata(p).num_rows for p in (root / "data").rglob("*.parquet"))
    if data_count != info["total_frames"] or int(episodes.length.sum()) != data_count:
        raise ValueError("Parquet and episode frame counts disagree")
    for key, feature in info["features"].items():
        if feature["dtype"] != "video":
            continue
        for (chunk, file), group in episodes.groupby(
            [f"videos/{key}/chunk_index", f"videos/{key}/file_index"]
        ):
            path = root / info["video_path"].format(
                video_key=key, chunk_index=int(chunk), file_index=int(file)
            )
            with av.open(str(path)) as container:
                times = []
                for frame in container.decode(video=0):
                    stop_check()
                    if [frame.height, frame.width, 3] != feature["shape"]:
                        raise ValueError("Decoded video dimensions disagree with feature metadata")
                    times.append(float(frame.pts * frame.time_base))
                stamps = np.array(times)
            if len(stamps) != int(group.length.sum()) or (
                len(stamps) > 1 and not np.allclose(np.diff(stamps), 1 / info["fps"], atol=1e-4)
            ):
                raise ValueError(f"Video frame count or timestamps invalid: {path.name}")
            offset = 0
            for idx in group.sort_values("episode_index").index:
                length = int(episodes.at[idx, "length"])
                episodes.at[idx, f"videos/{key}/from_timestamp"] = float(stamps[offset])
                episodes.at[idx, f"videos/{key}/to_timestamp"] = float(
                    stamps[offset + length - 1] + 1 / info["fps"]
                )
                offset += length
    # Preserve the original metadata shard boundaries while replacing timestamp columns.
    offset = 0
    for path in sorted((root / "meta/episodes").rglob("*.parquet")):
        length = pq.read_metadata(path).num_rows
        episodes.iloc[offset : offset + length].to_parquet(path, index=False)
        offset += length


def initialize_file_worker(events, stopped):
    global _EVENT_QUEUE, _STOP_EVENT
    _EVENT_QUEUE, _STOP_EVENT = events, stopped
    # Cancellation must not wait for a child log queue to flush after the parent stops draining.
    events.cancel_join_thread()
    import cv2
    import pyarrow as pa

    cv2.setNumThreads(1)
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)


def process_file(path, config, convert, cache, i, total):
    source = path.parent
    emit("inspecting", file_index=i, total=total, file=path.name)
    try:
        inspected = inspect_file(path, config)
        summary = inspected[0]
        shapes = summary["output_shapes"]
        emit(
            "inspected",
            file_index=i,
            total=total,
            file=path.name,
            message=(
                f"{path.name}: {summary['source_duration_s']:.1f}s recorded, "
                f"{summary['valid_duration_s']:.1f}s valid; {len(summary['segments'])} segment(s), "
                f"{summary['frames']} frames; output {json.dumps(shapes)}"
            ),
        )
    except ValueError as exc:
        emit("rejected", file_index=i, total=total, file=path.name, message=f"{path.name}: {exc}")
        return {"rejected": {"file": path.name, "reason": str(exc)}}
    if convert:
        sha = file_hash(path)
        key = hashlib.sha256(f"{path.name}:{sha}:{config.digest}".encode()).hexdigest()
        destination = cache / key
        receipt = cache / f"{key}.json"
        file_lineage = load_checkpoint(destination, receipt)
        reused = file_lineage is not None
        if not reused:
            temporary = cache / f".{key}.partial"
            if temporary.exists():
                shutil.rmtree(temporary)
            try:
                file_lineage = write_file(
                    path, temporary, config, inspected, source.parent.name.replace("_", " ")
                )
                validate_videos(temporary)
                if file_hash(path) != sha:
                    raise RuntimeError("Source changed during conversion")
                if destination.exists():
                    shutil.rmtree(destination)
                temporary.replace(destination)
                atomic_json(receipt, {"lineage": file_lineage, "manifest": checkpoint_manifest(destination)})
            except ValueError as exc:
                shutil.rmtree(temporary, ignore_errors=True)
                emit(
                    "rejected",
                    file_index=i,
                    total=total,
                    file=path.name,
                    message=f"{path.name}: {exc}",
                )
                return {"rejected": {"file": path.name, "reason": str(exc)}}
    return {
        "summary": summary,
        "root": destination if convert else None,
        "sha": sha if convert else None,
        "reused": reused if convert else False,
        "lineage": file_lineage if convert else [],
    }


def file_results(files, config, convert, cache, workers):
    if workers == 1:
        for i, path in enumerate(files):
            yield i, process_file(path, config, convert, cache, i, len(files))
        return
    context = multiprocessing.get_context("spawn")
    events, stopped = context.Queue(maxsize=128), context.Event()
    pool = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=context,
        initializer=initialize_file_worker,
        initargs=(events, stopped),
    )
    pending = {}
    remaining = iter(enumerate(files))

    def submit_next():
        item = next(remaining, None)
        if item is not None:
            i, path = item
            pending[pool.submit(process_file, path, config, convert, cache, i, len(files))] = i

    try:
        for _ in range(workers):
            submit_next()
        while pending:
            stop_check()
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                emit(event.pop("phase"), **event)
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                i = pending.pop(future)
                yield i, future.result()
                submit_next()
    finally:
        stopped.set()
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        events.close()
        events.join_thread()


def run(request):
    # Never depend on a service account having a writable home or a shared Hub cache.
    work = Path(request["result"]).parent
    os.environ["HF_DATASETS_CACHE"] = str(work / "datasets-cache")
    os.environ["HF_LEROBOT_HOME"] = str(work / "lerobot-cache")
    config = McapImportConfig.from_dict(request["config"])
    source = Path(request["source"])
    files = sorted(p for p in source.glob("*.mcap") if not p.is_symlink())
    if config.max_files:
        files = files[: config.max_files]
    if not files:
        raise ValueError("No MCAP files in this source directory")
    report = {
        "source": str(source),
        "config": config.to_dict(),
        "config_digest": config.digest,
        "files": [],
        "rejected": [],
        "source_files": len(files),
        "input_fingerprint": request["input_fingerprint"],
    }
    roots, expected, lineage = [], None, []
    convert = request["operation"] == "convert"
    cache = Path(request["checkpoint_root"])
    if convert:
        cache.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(int(request.get("workers", 1)), len(files), 4)) if convert else 1
    emit("resources", workers=workers, message=f"MCAP conversion: {workers} file worker(s)")
    completed = {}
    for i, result in file_results(files, config, convert, cache, workers):
        completed[i] = result
        if result.get("reused"):
            emit("resuming", file=files[i].name, message=f"{files[i].name}: reused verified checkpoint")
        emit(
            "converting" if convert else "inspecting",
            current=len(completed),
            total=len(files),
            file=files[i].name,
            message=f"Completed {len(completed)}/{len(files)} files ({workers} workers)",
        )
    # Completion order must never change dataset episode order or identity.
    for i, result in sorted(completed.items()):
        if "rejected" in result:
            report["rejected"].append(result["rejected"])
            continue
        summary = result["summary"]
        shapes = summary["output_shapes"]
        if expected is not None and expected != shapes:
            reason = "Incompatible output dimensions within this dataset"
            report["rejected"].append({"file": files[i].name, "reason": reason})
            emit("rejected", file=files[i].name, message=f"{files[i].name}: {reason}")
            continue
        expected = shapes
        if convert:
            roots.append(result["root"])
            sha = result["sha"]
            for row in result["lineage"]:
                lineage.append(
                    {
                        **row,
                        "episode_index": len(lineage),
                        "source_sha256": sha,
                        "episode_uid": hashlib.sha256(
                            f"{files[i].name}:{sha}:{row['start_ns']}:{row['last_frame_ns']}".encode()
                        ).hexdigest(),
                    }
                )
        report["files"].append(summary)
    if not report["files"]:
        raise ValueError("No valid MCAP files: " + json.dumps(report["rejected"][:5]))
    report["total_frames"] = sum(item["frames"] for item in report["files"])
    report["total_episodes"] = sum(len(item["segments"]) for item in report["files"])
    emit(
        "summary",
        message=(
            f"{len(report['files'])} valid files, {report['total_episodes']} episodes, "
            f"{report['total_frames']} frames; {len(report['rejected'])} files rejected"
        ),
    )
    if convert:
        from lerobot.datasets.aggregate import aggregate_datasets

        target = Path(request["output"])
        if target.exists():
            raise FileExistsError("Import output already exists")
        emit("aggregating", current=len(roots), total=len(files))
        aggregate_datasets(
            [f"local/{p.name}" for p in roots],
            f"local/{target.name}",
            roots=roots,
            aggr_root=target,
            data_files_size_in_mb=100,
            video_files_size_in_mb=200,
        )
        emit("validating")
        validate_videos(target)
        info = json.loads((target / "meta/info.json").read_text())
        info["data_profile"] = PROFILE
        for side in SIDES:
            info["features"][f"{side}_gripper_width"]["unit"] = "m"
            info["features"][f"{side}_arm_quaternion_pose"]["coordinate_frame"] = "source_world_unverified"
        atomic_json(target / "meta/info.json", info)
        atomic_json(target / "meta/data_profile.json", PROFILE)
        provenance = target / "provenance"
        provenance.mkdir(exist_ok=True)
        index = 0
        for root in roots:
            for alignment in sorted((root / "provenance").glob("alignment_*.parquet")):
                shutil.copyfile(alignment, provenance / f"alignment_{index:06d}.parquet")
                index += 1
        atomic_json(provenance / "episodes.json", lineage)
        atomic_json(provenance / "import_report.json", report)
    atomic_json(Path(request["result"]), report)
    emit("complete", current=len(files), total=len(files), episodes=report["total_episodes"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", nargs="?")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        from importlib.metadata import version

        import av
        import scipy
        from mcap_protobuf.decoder import DecoderFactory

        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        assert LeRobotDataset and DecoderFactory and scipy
        if version("lerobot") != "0.4.4":
            raise RuntimeError("MCAP runtime requires official LeRobot 0.4.4")
        if version("numpy") != "2.2.6":
            raise RuntimeError("MCAP runtime requires NumPy 2.2.6 for the v3 scalar writer")
        av.Codec("libsvtav1", "w")
        print(json.dumps({"ready": True, "lerobot": version("lerobot"), "av": av.__version__}))
        return
    started = time.monotonic()
    try:
        run(json.loads(Path(args.request).read_text()))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(
            json.dumps({"phase": "error", "error": str(exc), "elapsed_s": time.monotonic() - started}),
            flush=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

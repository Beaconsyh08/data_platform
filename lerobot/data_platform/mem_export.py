"""Read-only, versioned MEM export. No model inference or source annotation changes."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

SCHEMA = "mem_dataset_export/v1"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def contained(root: Path, path: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or escaping source file: {path}")
    return path


def _metadata(root: Path):
    from lerobot.data_platform.precompute.dataset_io import V3DatasetMetadata

    info = json.loads((root / "meta/info.json").read_text())
    if info["codebase_version"] == "v3.0":
        return V3DatasetMetadata(f"local/{root.name}", root)
    if info["codebase_version"] != "v2.1":
        raise ValueError("Only LeRobot v2.1 and v3.0 are supported")
    episodes = {
        row["episode_index"]: row
        for row in map(json.loads, (root / "meta/episodes.jsonl").read_text().splitlines())
    }

    def data_path(index):
        return Path(
            info["data_path"].format(
                episode_index=index, episode_chunk=index // int(info.get("chunks_size", 1000))
            )
        )

    return SimpleNamespace(info=info, episodes=episodes, get_data_file_path=data_path)


def _video_images(path: Path, targets: list[float], tolerance: float):
    """Decode once in timestamp order, choosing the nearest PTS, never frame-number fallback."""
    import av

    with av.open(str(path)) as container:
        iterator = iter(container.decode(video=0))
        previous = None
        following = next(iterator, None)
        for target in targets:
            while following is not None and following.time is not None and following.time < target:
                previous, following = following, next(iterator, None)
            choices = [
                frame for frame in (previous, following) if frame is not None and frame.time is not None
            ]
            if not choices:
                raise ValueError(f"Missing video timestamp at {target}")
            frame = min(choices, key=lambda item: abs(item.time - target))
            if abs(frame.time - target) > tolerance:
                raise ValueError(f"Video timestamp mismatch at {target}: decoded {frame.time}")
            yield frame.to_image().convert("RGB"), float(frame.time)


def inspect_selection(selection: dict) -> dict:
    from lerobot.data_platform.precompute.dataset_io import read_episode_table
    from lerobot.data_platform.temporal_caption import validate_result

    root = Path(selection["root"]).resolve()
    if not isinstance(selection.get("task"), str) or not selection["task"].strip():
        raise ValueError("Explicit nonempty task goal required")
    metadata_hashes = {
        str(p.relative_to(root)): digest(contained(root, p))
        for p in (root / "meta").rglob("*")
        if p.is_file()
    }
    meta = _metadata(root)
    episode = selection["episode_index"]
    if type(episode) is not int or episode not in meta.episodes:
        raise ValueError("Unknown episode_index")
    cameras = selection["cameras"]
    if not cameras or len(cameras) != len(set(cameras)):
        raise ValueError("Explicit unique camera order required")
    features = meta.info["features"]
    if any(features.get(camera, {}).get("dtype") not in {"image", "video"} for camera in cameras):
        raise ValueError("Unknown image/video camera")
    data_path = contained(root, root / meta.get_data_file_path(episode))
    data_hash = digest(data_path)
    rows = read_episode_table(root, meta, episode).sort_by("frame_index").to_pylist()
    if not rows or any(
        row["episode_index"] != episode or row["frame_index"] != i for i, row in enumerate(rows)
    ):
        raise ValueError("Episode rows must be isolated and contiguous from frame zero")
    timestamps = [float(row["timestamp"]) for row in rows]
    if not all(math.isfinite(t) for t in timestamps) or any(
        b <= a for a, b in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise ValueError("Timestamps must be finite and strictly increasing")
    if meta.episodes[episode].get("length", len(rows)) != len(rows):
        raise ValueError("Episode metadata length mismatch")
    fps = float(meta.info["fps"])
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("Invalid FPS")
    artifact_path = Path(selection["annotation"]).resolve()
    annotation_hash = digest(artifact_path)
    artifact = json.loads(artifact_path.read_text())
    if (
        artifact.get("dataset_key") != selection["dataset_key"]
        or artifact.get("episode_index") != episode
        or artifact.get("frame_count") != len(rows)
        or not math.isclose(float(artifact.get("fps", 0)), fps)
        or artifact.get("variant") not in {"refined", "caption", "fusion_reviewed"}
    ):
        raise ValueError("Annotation identity/frame_count/FPS/variant mismatch")
    validate_result(artifact["result"], len(rows))
    if selection.get("annotation_sha256") and digest(artifact_path) != selection["annotation_sha256"]:
        raise ValueError("Annotation revision changed")
    sources = {path for path in (root / "meta").rglob("*") if path.is_file()}
    sources.add(data_path)
    videos = {}
    for camera in cameras:
        if features[camera]["dtype"] == "video":
            if getattr(meta, "is_v3", False):
                path = root / meta.get_video_file_path(episode, camera)
                start, end = meta.get_video_time_range(episode, camera)
            else:
                path = root / meta.info["video_path"].format(
                    episode_index=episode,
                    episode_chunk=episode // int(meta.info.get("chunks_size", 1000)),
                    video_key=camera,
                )
                start, end = 0.0, timestamps[-1] - timestamps[0] + 1 / fps
            path = contained(root, path)
            if not math.isfinite(start) or not math.isfinite(end) or not start < end:
                raise ValueError("Invalid video episode time range")
            targets = [start + t - timestamps[0] for t in timestamps]
            if any(t < start or t >= end for t in targets):
                raise ValueError("Video timestamps outside selected episode")
            videos[camera] = (path, targets)
            sources.add(path)
        else:
            for row in rows:
                value = row[camera]
                if not isinstance(value, dict) or not (value.get("bytes") or value.get("path")):
                    raise ValueError("Image feature must contain bytes or a local path")
                if not value.get("bytes"):
                    sources.add(contained(root, root / value["path"]))
    hashes = {str(path.relative_to(root)): digest(contained(root, path)) for path in sorted(sources)}
    if (
        any(hashes.get(p) != sha for p, sha in metadata_hashes.items())
        or hashes[str(data_path.relative_to(root))] != data_hash
        or digest(artifact_path) != annotation_hash
    ):
        raise ValueError("Source or annotation changed during inspection")
    if selection.get("source_files_sha256") and selection["source_files_sha256"] != hashes:
        raise ValueError("Source snapshot changed")
    identity = {key: selection.get(key) for key in ("dataset_version_id", "episode_uid", "lineage_group")}
    if bool(identity["dataset_version_id"]) != bool(identity["episode_uid"]):
        raise ValueError("dataset_version_id and episode_uid must be provided together")
    return {
        "root": root,
        "rows": rows,
        "cameras": cameras,
        "videos": videos,
        "fps": fps,
        "artifact": artifact,
        "annotation_path": artifact_path,
        "hashes": hashes,
        "identity": identity,
        "annotation_sha256": annotation_hash,
        "selection": selection,
    }


def export(manifest: Path, output: Path, *, dry_run: bool = False) -> dict:
    selection_hash = digest(manifest)
    specification = json.loads(manifest.read_text())
    if specification.get("schema") != "mem_export_selection/v1" or not specification.get("episodes"):
        raise ValueError("Expected mem_export_selection/v1 with explicit episodes")
    selections = specification["episodes"]
    identities = [(str(Path(s["root"]).resolve()), s["episode_index"]) for s in selections]
    if len(identities) != len(set(identities)):
        raise ValueError("Select exactly one annotation per episode")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(output)
    for source, _ in identities:
        if output.is_relative_to(Path(source)):
            raise ValueError("Output must be outside source datasets")
    items = []
    estimated_bytes = 0
    for selection in selections:
        item = inspect_selection(selection)
        item["frame_count"] = len(item["rows"])
        for camera in item["cameras"]:
            if camera in item["videos"]:
                path, targets = item["videos"][camera]
                first = _video_images(path, targets[:1], 0.51 / item["fps"])
                image, _ = next(first)
                first.close()
            else:
                value = item["rows"][0][camera]
                source = (
                    io.BytesIO(value["bytes"])
                    if value.get("bytes")
                    else contained(item["root"], item["root"] / value["path"])
                )
                with Image.open(source) as im:
                    image = im.copy()
            estimated_bytes += image.width * image.height * 3 * len(item["rows"])
        del item["rows"]  # Bound decoded Parquet residency to one episode, not the whole pilot.
        items.append(item)
    report = {
        "schema": SCHEMA,
        "episodes": len(items),
        "frames": sum(item["frame_count"] for item in items),
        "images": sum(item["frame_count"] * len(item["cameras"]) for item in items),
        "source_bytes": sum(sum((item["root"] / p).stat().st_size for p in item["hashes"]) for item in items),
        "source_snapshots": [
            {"source_files_sha256": item["hashes"], "annotation_sha256": item["annotation_sha256"]}
            for item in items
        ],
    }
    report["estimated_rgb_bytes"] = estimated_bytes
    report["space_note"] = "Uncompressed RGB estimate from the first frame per camera; PNG size varies."
    if dry_run:
        return report
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    episodes = []
    try:
        for index, inspected in enumerate(items):
            item = inspect_selection(inspected["selection"])
            if (
                item["hashes"] != inspected["hashes"]
                or item["annotation_sha256"] != inspected["annotation_sha256"]
            ):
                raise ValueError("Source changed since preflight")
            eid = f"episode-{index:06d}"
            frames = [
                {"frame_index": i, "timestamp": float(row["timestamp"]), "images": []}
                for i, row in enumerate(item["rows"])
            ]
            media_hash = hashlib.sha256()
            for camera_index, camera in enumerate(item["cameras"]):
                if camera in item["videos"]:
                    path, targets = item["videos"][camera]
                    images = _video_images(path, targets, 0.51 / item["fps"])
                else:

                    def embedded_images(camera=camera, item=item):
                        for row in item["rows"]:
                            value = row[camera]
                            source = (
                                io.BytesIO(value["bytes"])
                                if value.get("bytes")
                                else contained(item["root"], item["root"] / value["path"])
                            )
                            with Image.open(source) as image:
                                yield image.convert("RGB"), float(row["timestamp"])

                    images = embedded_images()
                    targets = [float(row["timestamp"]) for row in item["rows"]]
                for frame_index, (image, actual_time) in enumerate(images):
                    relative = f"media/{eid}/camera-{camera_index:02d}/{frame_index:06d}.png"
                    path = staging / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if shutil.disk_usage(staging).free < image.width * image.height * 4 + 1024 * 1024:
                        raise OSError("Insufficient output space")
                    image.save(path, format="PNG")
                    media_hash.update(f"{camera}:{frame_index}:{image.size}".encode())
                    media_hash.update(image.tobytes())
                    frames[frame_index]["images"].append(
                        {
                            "camera": camera,
                            "path": relative,
                            "width": image.width,
                            "height": image.height,
                            "source_timestamp": actual_time,
                            "decode_error_s": actual_time - targets[frame_index],
                        }
                    )
            frames_path = f"media/{eid}/frames.jsonl"
            (staging / frames_path).write_text("".join(json.dumps(row) + "\n" for row in frames))
            original = f"annotations/{eid}.original.json"
            normalized = f"annotations/{eid}.json"
            (staging / "annotations").mkdir(exist_ok=True)
            shutil.copyfile(item["annotation_path"], staging / original)
            write_json(
                staging / normalized,
                {
                    "interval_convention": "half-open",
                    "result": item["artifact"]["result"],
                    "model_review": item["artifact"].get("review"),
                    "human_review": None,
                    "annotation_binding": "metadata_checked_requires_human_source_confirmation",
                },
            )
            content_id = media_hash.hexdigest()
            episodes.append(
                {
                    "id": eid,
                    "dataset_key": item["selection"]["dataset_key"],
                    "episode_index": item["selection"]["episode_index"],
                    **item["identity"],
                    "group_id": item["identity"]["lineage_group"] or content_id,
                    "group_confirmed": bool(item["identity"]["lineage_group"]),
                    "content_sha256": content_id,
                    "frame_count": len(frames),
                    "fps": item["fps"],
                    "cameras": item["cameras"],
                    "frames_file": frames_path,
                    "task": item["selection"]["task"],
                    "annotation": normalized,
                    "original_annotation": original,
                    "annotation_sha256": item["annotation_sha256"],
                    "source_files_sha256": item["hashes"],
                }
            )
            if (
                any(digest(item["root"] / p) != sha for p, sha in item["hashes"].items())
                or digest(item["annotation_path"]) != item["annotation_sha256"]
            ):
                raise ValueError("Source or annotation changed during export")
        (staging / "episodes.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in episodes)
        )
        files = {str(p.relative_to(staging)): digest(p) for p in sorted(staging.rglob("*")) if p.is_file()}
        write_json(
            staging / "manifest.json",
            {
                "schema": SCHEMA,
                "selection_sha256": selection_hash,
                "episode_count": len(episodes),
                "files": files,
                "interval_convention": "half-open",
                "media": "lossless RGB PNG, no annotation overlays",
            },
        )
        if output.exists():
            raise FileExistsError(output)
        os.rename(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(export(args.manifest, args.output, dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

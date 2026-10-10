"""Read experimental captions in output/<method>/episode_* without source mutation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from contextlib import nullcontext
from pathlib import Path

from lerobot.data_platform.caption_reads import MAX_REPLY_BYTES, VIDEO_CHUNK_BYTES
from lerobot.data_platform.temporal_caption import validate_result
from lerobot.data_platform.temporal_caption_demo import review_context


def _regular(path: Path, root: Path) -> Path:
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Caption file escapes its dataset")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Caption artifacts must be regular files")
    return path


def _json(path: Path, root: Path) -> tuple[dict, bytes]:
    path = _regular(path, root)
    with path.open("rb") as handle:
        data = handle.read(MAX_REPLY_BYTES + 1)
    if len(data) > MAX_REPLY_BYTES:
        raise ValueError("Caption JSON is too large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Expected a caption object")
    return value, data


def _episodes(root: Path):
    output = root / "output"
    if not output.exists():
        return
    if output.is_symlink() or not output.is_dir():
        raise ValueError("Caption output must be a dataset directory")
    for method in sorted(output.iterdir()):
        if (
            method.is_symlink()
            or not method.is_dir()
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", method.name)
        ):
            continue
        for episode in sorted(method.iterdir()):
            if (
                episode.is_symlink()
                or not episode.is_dir()
                or not re.fullmatch(r"episode_[0-9]+", episode.name)
            ):
                continue
            relative = episode.relative_to(root).as_posix()
            run = "source-" + hashlib.sha256(relative.encode()).hexdigest()[:24]
            yield run, method.name, episode


def _artifact(root: Path, key: str, method: str, episode: Path) -> dict:
    from lerobot.data_platform.precompute.dataset_io import is_v3_info, load_episode_records

    raw, data = _json(episode / "caption.json", root)
    info, _ = _json(root / "meta/info.json", root)
    if is_v3_info(info):
        _regular(root / "meta/tasks.parquet", root)
        for path in (root / "meta/episodes").rglob("*.parquet"):
            _regular(path, root)
    else:
        _regular(root / "meta/episodes.jsonl", root)
    index = raw["episode_index"]
    if type(index) is not int or episode.name != f"episode_{index:06d}":
        raise ValueError("Caption episode identity does not match its directory")
    records = {item["episode_index"]: item for item in load_episode_records(root)}
    if index not in records or raw["frame_count"] != records[index]["length"]:
        raise ValueError("Caption frame count does not match the dataset episode")
    fps = raw["fps"]
    if type(fps) not in (int, float) or not math.isfinite(fps) or fps <= 0 or fps != info["fps"]:
        raise ValueError("Caption FPS does not match the dataset")
    if raw.get("dataset_name", root.name) != root.name:
        raise ValueError("Caption belongs to another dataset")
    if not isinstance(raw.get("model"), str) or not raw["model"].strip():
        raise ValueError("Caption must retain its model provenance")
    result = raw.get("result") or {field: raw[field] for field in ("summary", "segments", "limitations")}
    validate_result(result, raw["frame_count"])
    _regular(episode / "video.mp4", root)
    if not (episode / "video.mp4").stat().st_size:
        raise ValueError("Review video is empty")
    meta_path = episode / "episode.json"
    context = {"task": "; ".join(raw.get("task_hint", [])), "signals": [], "events": []}
    if meta_path.exists():
        meta, _ = _json(meta_path, root)
        for field in ("episode_index", "frame_count", "fps", "source_digest"):
            if field in raw and raw[field] != meta.get(field):
                raise ValueError("Caption does not match its episode metadata")
        context = review_context(episode)
        if raw.get("include_signals") is False:
            context["signals"], context["events"] = [], []
    digest = hashlib.sha256(data).hexdigest()
    value = {
        field: content
        for field, content in raw.items()
        if field not in {"summary", "segments", "limitations"}
    }
    value.update(
        dataset_key=key,
        source_scheme_id=raw.get("scheme_id"),
        scheme_id=method,
        scheme={"id": method, "name": method, "description": f"Dataset experiment: {method}"},
        variant="caption",
        duration_s=raw["frame_count"] / fps,
        created_at=raw.get("created_at", (episode / "caption.json").stat().st_mtime),
        result=result,
        context=context,
        source_caption_sha256=digest,
        source={"kind": "dataset_output", "path": episode.relative_to(root).as_posix()},
        revision=digest,
    )
    return value


def read_dataset_caption(agent, read: dict) -> tuple[int, bytes, bool]:
    """Accept only registered-root descriptors and four bounded read operations."""
    from lerobot.data_platform.agent import _require_within

    root = _require_within(Path(read["root"]), agent.allowed_roots, "caption dataset")
    key = read["dataset_key"]
    action = read["action"]
    if action not in {"runs", "result", "video-info", "video-chunk"}:
        raise ValueError("Unknown caption read operation")
    episodes = list(_episodes(root))
    if action == "runs":
        runs, warnings = [], []
        for run, method, episode in episodes:
            try:
                value = _artifact(root, key, method, episode)
                runs.append(
                    {
                        "run": run,
                        **{
                            field: value[field]
                            for field in (
                                "scheme",
                                "episode_index",
                                "variant",
                                "created_at",
                                "revision",
                                "source_caption_sha256",
                            )
                        },
                    }
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warnings.append(f"{method}/{episode.name}: {type(exc).__name__}")
        payload = {"runs": runs, "warnings": warnings}
    else:
        match = next((item for item in episodes if item[0] == read.get("run")), None)
        if match is None:
            raise FileNotFoundError("Dataset caption run does not exist")
        _, method, episode = match
        value = _artifact(root, key, method, episode)
        if action == "result":
            payload = value
        else:
            from lerobot.data_platform.camera_layout import ordered_review_video

            info, _ = _json(root / "meta/info.json", root)
            cameras = [
                name for name, feature in info.get("features", {}).items() if feature["dtype"] == "image"
            ]
            if (episode / "episode.json").exists():
                meta, _ = _json(episode / "episode.json", root)
                cameras = meta.get("cameras", cameras)
            with getattr(agent, "_caption_video_lock", nullcontext()):
                video = ordered_review_video(
                    episode / "video.mp4", cameras, agent.state_path.parent / "caption-display"
                )
            size = video.stat().st_size
            video_revision = hashlib.sha256(
                f"{value['revision']}:{size}:{video.stat().st_mtime_ns}".encode()
            ).hexdigest()
            if action == "video-info":
                payload = {"size": size, "revision": video_revision}
            else:
                start, length = read["start"], read["length"]
                if (
                    type(start) is not int
                    or type(length) is not int
                    or start < 0
                    or not 0 < length <= VIDEO_CHUNK_BYTES
                ):
                    raise ValueError("Invalid caption video range")
                if read.get("revision") != video_revision or start + length > size:
                    return 409, b'{"error":"Caption video changed; reload the result"}', False
                with video.open("rb") as handle:
                    handle.seek(start, os.SEEK_SET)
                    data = handle.read(length)
                if len(data) != length:
                    raise ValueError("Caption video changed during reading")
                return 200, data, True
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(body) > MAX_REPLY_BYTES:
        raise ValueError("Caption reply is too large")
    return 200, body, False

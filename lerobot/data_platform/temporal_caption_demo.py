"""Adapt caption_demo artifacts to the shared temporal caption review contract."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import time
from pathlib import Path

from lerobot.data_platform.temporal_caption import validate_result, write_json

SCHEMES = {
    "fusion_review": {
        "id": "fusion_review",
        "name": "Fusion + Review",
        "description": "Independent visual/event hypotheses, focused evidence, blind event audit and targeted repair.",
    },
    "multiview_semantics": {
        "id": "multiview_semantics",
        "name": "Multi-view Semantics",
        "description": "Sampled camera images + measured poses; optional dense boundary review.",
    },
    "video_events": {
        "id": "video_events",
        "name": "Video + Events",
        "description": "Temporal video input + compact motion digest + gripper events; scene and task assessment.",
    },
}


def scheme_info(artifact: dict) -> dict:
    return SCHEMES[artifact.get("scheme_id", "multiview_semantics")]


def normalize_demo(caption: dict, meta: dict, dataset_key: str) -> dict:
    """Convert inclusive demo frame ends once; retain model judgments without inventing confidence."""
    frames, fps = meta["num_frames"], meta["fps"]
    if type(frames) is not int or frames <= 0 or not math.isfinite(fps) or fps <= 0:
        raise ValueError("Invalid episode metadata")
    original_meta = caption.get("_meta", {})
    if any(
        original_meta.get(key, value) != value
        for key, value in (("episode", meta["episode_index"]), ("num_frames", frames), ("fps", fps))
    ):
        raise ValueError("Caption and episode metadata disagree")
    segments = []
    for segment in caption["segments"]:
        if type(segment["end_frame"]) is not int:
            raise ValueError("Demo end_frame must be an integer inclusive endpoint")
        segments.append(
            {
                "start_frame": segment["start_frame"],
                "end_frame": segment["end_frame"] + 1,
                "caption": segment["caption_zh"],
                "caption_en": segment["caption_en"],
                "evidence": segment["evidence"],
                "confidence": "not_reported",
                "uncertainty": "",
                "phase": segment.get("phase", "other"),
                "active_arm": segment.get("active_arm", "unknown"),
            }
        )
    result = {
        "summary": caption["episode_caption"]["zh"],
        "summary_en": caption["episode_caption"]["en"],
        "segments": segments,
        "limitations": [],
        "scene": caption.get("scene", {}),
        "quality": caption.get("quality", {}),
    }
    validate_result(result, frames)
    return {
        "schema_version": 1,
        "dataset_key": dataset_key,
        "scheme_id": "video_events",
        "episode_index": meta["episode_index"],
        "frame_count": frames,
        "fps": fps,
        "duration_s": frames / fps,
        "variant": "caption",
        "created_at": time.time(),
        "model": original_meta.get("model", "not reported"),
        "usage": original_meta.get("usage"),
        "source_frame_convention": "inclusive",
        "result": result,
    }


def import_demo(source: Path, output: Path, dataset_key: str) -> None:
    """Import one completed demo; never execute its scripts or alter its source directory."""
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Import output must be outside the demo source")
    meta = json.loads((source / "episode_meta.json").read_text())
    caption = json.loads((source / "caption.json").read_text())
    artifact = normalize_demo(caption, meta, dataset_key)
    signals = []
    with (source / "traj.csv").open() as stream:
        for row in csv.DictReader(stream):
            signals.append(
                {
                    "frame": int(row["frame"]),
                    **{
                        f"{side}_arm_pose": [float(row[f"{side}_arm_{axis}"]) for axis in ("x", "y", "z")]
                        for side in ("left", "right")
                    },
                    **{f"{side}_gripper_pos": float(row[f"{side}_gripper"]) for side in ("left", "right")},
                }
            )
    if [row["frame"] for row in signals] != list(range(artifact["frame_count"])):
        raise ValueError("Trajectory must cover exactly the selected episode")
    output.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(source / "combined.mp4", output / "video.mp4")
        shutil.copy2(source / "caption.json", output / "original_caption.json")
        artifact["source_digest"] = hashlib.sha256((source / "caption.json").read_bytes()).hexdigest()
        write_json(
            output / "episode.json",
            {
                "fps": artifact["fps"],
                "episode_index": artifact["episode_index"],
                "frame_count": artifact["frame_count"],
                "signals": signals,
                "task_hint": [meta.get("task", "")],
                "gripper_events": meta.get("gripper_events", []),
            },
        )
        write_json(output / "caption.json", artifact)
    except Exception:
        shutil.rmtree(output)
        raise


def review_context(folder: Path) -> dict:
    path = folder / "episode.json"
    if not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
        return {"task": "", "signals": [], "events": []}
    meta = json.loads(path.read_text())
    fps = float(meta["fps"])
    signals = []
    previous = None
    for row in meta.get("signals", []):
        point = {"frame": row["frame"], "t": row["frame"] / fps}
        for side in ("left", "right"):
            point[f"{side}_gripper"] = row.get(f"{side}_gripper_pos")
            pose = row.get(f"{side}_arm_pose")
            old = previous.get(f"{side}_arm_pose") if previous else None
            dt = (row["frame"] - previous["frame"]) / fps if previous else 0
            point[f"{side}_speed"] = math.dist(pose[:3], old[:3]) / dt if pose and old and dt > 0 else 0
        signals.append(point)
        previous = row
    return {
        "task": "; ".join(meta.get("task_hint", [])),
        "signals": signals,
        "events": meta.get("gripper_events", []),
    }


def infer_video_events(
    bundle: Path, output: Path, dataset_key: str, model: str, *, client=None, progress=None
) -> None:
    """Run the demo's video-sequence + event-digest method on one exported episode."""
    import base64

    from lerobot.data_platform.qwen import QwenClient

    meta = json.loads((bundle / "episode.json").read_text())
    context = review_context(bundle)
    count = meta["frame_count"]
    if count < 2:
        raise ValueError("Video event annotation needs at least two frames")
    selected = sorted({round(i * (count - 1) / (min(24, count) - 1)) for i in range(min(24, count))})
    events = []
    for side in ("left", "right"):
        values = [row.get(f"{side}_gripper_pos") for row in meta["signals"]]
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            continue
        low, high = min(values), max(values)
        if high - low < 1e-6:
            continue
        midpoint = (low + high) / 2
        for i in range(1, len(values)):
            if (values[i] >= midpoint) != (values[i - 1] >= midpoint):
                events.append(
                    {
                        "frame": i,
                        "side": side,
                        "event": "gripper_rise" if values[i] >= midpoint else "gripper_fall",
                    }
                )
    digest = []
    for frame in selected:
        row = {"frame": frame, "t": round(frame / meta["fps"], 3)}
        for side in ("left", "right"):
            pose = meta["signals"][frame].get(f"{side}_arm_pose")
            row[f"{side}_pose"] = [round(v, 3) for v in pose] if pose else None
            row[f"{side}_speed"] = round(context["signals"][frame][f"{side}_speed"], 3)
            row[f"{side}_gripper"] = context["signals"][frame][f"{side}_gripper"]
        digest.append(row)
    prompt = (
        "Annotate this ONE dual-arm UMI demonstration for robot learning. Follow the caption_demo method: "
        "read the scene, track the observed interaction, segment semantic subtasks, then assess the recorded "
        "instruction against actual behavior. Describe manipulator actions, not assumed autonomous intent. "
        "The video part is a time-ordered sequence of synchronized three-camera tiles (head | left | right). "
        "Each tile has its episode frame index burned in. Use the exact frame/time mapping in the digest. "
        "Track the SAME object across camera views and handovers; do not double count it. "
        "Numeric poses are observations. Speed is in source position units/s. Gripper rise/fall events are "
        "midrange crossings, not proven contact or open/close labels; determine interaction from the video. "
        "Choose boundaries from visual behavior and motion evidence, not uniform time partitions. "
        "Use concise imperative captions in Chinese and English. State ambiguity in evidence. "
        "Assess instruction_followed and success relative to the nominal task; do not equate successful "
        "motion with task compliance. Quality judgments are model assessments, not deletion decisions. "
        "Return ONLY JSON with this schema:\n"
        '{"scene":{"objects":["object"],"description_en":"scene","description_zh":"场景"},'
        '"episode_caption":{"en":"observed behavior","zh":"实际行为"},'
        '"segments":[{"start_frame":0,"end_frame":10,"phase":"idle|approach|grasp|lift|hold|transfer|release|retreat|other",'
        '"active_arm":"left|right|both|none","caption_en":"action","caption_zh":"动作","evidence":"why"}],'
        '"quality":{"instruction_followed":false,"success":false,"issues":["issue"],'
        '"confidence":0.5,"notes_zh":"说明"}}\n'
        f"Frame ends are INCLUSIVE. Cover frames 0 through {count - 1}, no overlap/gaps; "
        "each next start equals previous end + 1. Do not assert precise contact between sampled frames.\n"
        + json.dumps(
            {
                "task": meta.get("task_hint"),
                "frame_count": count,
                "fps": meta["fps"],
                "motion_digest": digest,
                "candidate_events": events,
            },
            ensure_ascii=False,
        )
    )
    if output.exists():
        raise FileExistsError(output)
    video = [
        "data:image/jpeg;base64,"
        + base64.b64encode((bundle / "frames" / f"{frame:06d}.jpg").read_bytes()).decode()
        for frame in selected
    ]
    started = time.time()
    if progress:
        progress("Video + Events annotation")
    response = (client or QwenClient(timeout_s=600)).post_chat_completion(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video",
                            "video": video,
                            "fps": (len(selected) - 1) * meta["fps"] / (count - 1),
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": 0.2,
            "max_tokens": 16000,
            "response_format": {"type": "json_object"},
        }
    )
    caption = json.loads(response["choices"][0]["message"]["content"])
    caption["_meta"] = {
        "episode": meta["episode_index"],
        "num_frames": count,
        "fps": meta["fps"],
        "model": model,
        "usage": response.get("usage"),
    }
    artifact = normalize_demo(
        caption,
        {"num_frames": count, "fps": meta["fps"], "episode_index": meta["episode_index"]},
        dataset_key,
    )
    artifact.update(
        sampled_frames=selected,
        source_digest=meta.get("source_digest"),
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        elapsed_s=time.time() - started,
    )
    output.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(bundle / "video.mp4", output / "video.mp4")
        write_json(output / "episode.json", {**meta, "gripper_events": events})
        write_json(output / "original_caption.json", caption)
        (output / "caption.prompt.txt").write_text(prompt)
        write_json(output / "caption.json", artifact)
    except Exception:
        shutil.rmtree(output)
        raise
    print(
        json.dumps(
            {
                "scheme": "video_events",
                "episode": meta["episode_index"],
                "segments": len(caption["segments"]),
                "usage": response.get("usage"),
            }
        ),
        flush=True,
    )

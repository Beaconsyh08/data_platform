"""Isolated GPU worker. Run with the administrator's pinned FLORA Python environments."""

from __future__ import annotations

import ctypes
import importlib
import json
import math
import os
import signal
import subprocess
import sys
from pathlib import Path


def save(path, value):
    def encode(item):
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        if isinstance(item, Path):
            return str(item)
        raise TypeError(type(item).__name__)

    path.write_text(json.dumps(value, ensure_ascii=False, default=encode))


def caption_inputs(work, request):
    """Keep model calls within official 120-second limits; retain episode offsets."""
    paths = {key: Path(value) for key, value in request["videos"].items()}
    instruction = ""
    if "main" not in paths:
        montage = work / "wrists.mp4"
        if not montage.exists():
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(paths["left"]),
                    "-i",
                    str(paths["right"]),
                    "-filter_complex",
                    "hstack=inputs=2",
                    "-c:v",
                    "libx264",
                    "-crf",
                    "0",
                    "-pix_fmt",
                    "yuv444p",
                    str(montage),
                ],
                check=True,
            )
        paths = {"main": montage}
        instruction = "The video is a synchronized side-by-side montage: left panel is the left wrist camera, right panel is the right wrist camera. No scene camera is available."
    count, fps = request["frame_count"], request["fps"]
    # Equal chunks avoid a tiny final tail while never stretching or duplicating frames.
    chunks = math.ceil(count / min(7200, math.floor(120 * fps)))
    inputs = []
    for index in range(chunks):
        start, end = index * count // chunks, (index + 1) * count // chunks
        chunk_paths = {}
        for role, path in paths.items():
            target = work / f"chunk-{index}-{role}.mp4" if chunks > 1 else path
            if chunks > 1 and not target.exists():
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-i",
                        str(path),
                        "-vf",
                        f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS",
                        "-an",
                        "-c:v",
                        "libx264",
                        "-crf",
                        "0",
                        "-pix_fmt",
                        "yuv444p",
                        str(target),
                    ],
                    check=True,
                )
            chunk_paths[role] = target
        inputs.append((start / fps, chunk_paths))
    return inputs, instruction


def caption_stage(stage, work, request):
    config = request["runtime"]
    sys.path.insert(0, config["caption_code"])
    import huggingface_hub
    import torch
    from batch_utils import models, pipeline
    from batch_utils.api import DataEnginePipeline

    weights = Path(config["weights"])
    models.snapshot = lambda kind: weights / kind
    original = huggingface_hub.snapshot_download

    def snapshot(repo_id, **kwargs):
        base = models.MODELS["tempseg_base"]
        if repo_id == base["repo"] and kwargs.get("revision") == base["revision"]:
            return str(weights / "tempseg_base")
        return original(repo_id, local_files_only=True, **kwargs)

    huggingface_hub.snapshot_download = snapshot
    inputs, instruction = caption_inputs(work, request)
    torch.set_num_threads(4)

    def progress(message, **kwargs):
        print(message, kwargs.get("desc", ""), flush=True)

    session = models.Session()
    try:
        saved = json.loads((work / "segment.json").read_text())["chunks"] if stage == "caption" else []
        chunks, all_segments, all_audit = [], [], []
        for index, (offset, paths) in enumerate(inputs):
            videos, profile = DataEnginePipeline._inputs(
                paths["main"], "yam", paths.get("left"), paths.get("right"), False, instruction
            )
            if stage == "segment":
                segments, audit = pipeline.segment(videos, "yam", session, instruction, progress)
            else:
                segments = saved[index]["segments"]
                if len(videos) == 3:
                    segments, audit = pipeline.caption_three(
                        videos, segments, session, instruction, progress, profile=profile
                    )
                else:
                    segments, audit = pipeline.caption_single(
                        videos, segments, profile, session, instruction, progress
                    )
                if not segments or any(item["status"] != "complete" for item in segments):
                    raise RuntimeError("Incomplete FLORA captions")
            chunks.append({"offset_s": offset, "segments": segments})
            all_segments.extend(
                {
                    **item,
                    "start_s": item["start_s"] + offset,
                    "end_s": item["end_s"] + offset,
                    "input_chunk": index,
                }
                for item in segments
            )
            all_audit.append({"offset_s": offset, "audit": audit})
        save(
            work / f"{stage}.json",
            {
                "segments": all_segments,
                "chunks": chunks,
                "audit": all_audit,
                "model_revisions": models.MODELS,
                "input_adaptation": {
                    "camera_layout": "two_wrist_montage" if instruction else "native_three_view",
                    "chunk_offsets_s": [offset for offset, _ in inputs],
                    "instruction": instruction,
                    "reward_scope": "whole_episode_original_cameras",
                },
            },
        )
    finally:
        session.close()


def reward_stage(stage, work, request):
    weights = Path(request["runtime"]["weights"]) / "rewarder"
    sys.path.insert(0, str(weights))
    module = importlib.import_module(f"batch_utils.{stage}_reward")
    original_load = module.load_llm

    def native_load(*args, **kwargs):
        llm = original_load(*args, **kwargs)
        config = llm.llm_engine.model_config
        if config.quantization is not None or str(config.dtype) != "torch.bfloat16":
            raise RuntimeError("FLORA Rewarder requires native BF16 without quantization")
        return llm

    module.load_llm = native_load
    jobs = work / "jobs.jsonl"
    save(
        jobs,
        {
            "id": "episode",
            "task": request["task"],
            "videos": [
                request["videos"][key] for key in ("left", "main", "right") if key in request["videos"]
            ],
        },
    )
    target = work / f"{stage}.jsonl"
    args = ["--jobs", str(jobs), "--model", str(weights), "--output", str(target), "--max-num-seqs", "1"]
    args += ["--with-rationale"] if stage == "terminal" else ["--frame-scores"]
    if module.main(args) != 0:
        raise RuntimeError("FLORA Rewarder failed")
    value = json.loads(target.read_text())
    if stage == "terminal":
        if value.get("score") not in {1, 2, 3, 4, 5}:
            raise ValueError("Invalid terminal reward")
        return
    from batch_utils.core import dense_windows, parse_dense, stitch_dense

    count, fps = request["frame_count"], request["fps"]
    if value["invalid_clips"] or value["num_frames"] != count or abs(value["fps"] - fps) > 1e-5:
        raise ValueError("Invalid dense windows or video timing mismatch")
    windows = dense_windows(count, fps)
    if len(value["clips"]) != len(windows):
        raise ValueError("Missing dense windows")
    answers = [parse_dense(clip["output"]) for clip in value["clips"]]
    merged, sources, _ = stitch_dense(windows, answers, count)
    if merged.tolist() != value["frame_scores"]:
        raise ValueError("Official reward merge mismatch")
    raw = []
    for window, answer, clip in zip(windows, answers, value["clips"], strict=True):
        scores, _, _ = stitch_dense([window], [answer], count, tail_trim_s=0)
        covered = [
            frame
            for group in range(1, 33)
            if window.group_valid(group)
            for frame in window.group_frames(group)
        ]
        scores[: min(covered)] = 0
        scores[max(covered) + 1 :] = 0
        raw.append({"scores": scores.tolist(), "output": clip["output"]})
    intervals, start = [], None
    for frame in range(count + 1):
        conflict = frame < count and len({w["scores"][frame] for w in raw if w["scores"][frame]}) > 1
        if conflict and start is None:
            start = frame
        elif not conflict and start is not None:
            intervals.append({"start_frame": start, "end_frame": frame})
            start = None
    save(
        work / "reward.json",
        {
            "terminal": json.loads((work / "terminal.jsonl").read_text()),
            "windows": raw,
            "merged_scores": merged.tolist(),
            "merged_source_window": sources.tolist(),
            "disagreement_intervals": intervals,
            "human_reviewed": False,
            "alignment": "Nearest original sampled frame pair per window, no tail trim; outside coverage is zero. Official merged scores unchanged.",
            "expected_model_revision": "7de68d22c4a9decec5ddb7f86ddcaee8f82d3367",
        },
    )


def guard_parent():
    # Keep vLLM descendants with their worker even when the platform supervisor is force-stopped.
    def terminate_group(signum, frame):
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, terminate_group)
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise RuntimeError("Unable to install FLORA parent-death protection")
    if os.getppid() != int(os.environ["FLORA_PARENT_PID"]):
        terminate_group(None, None)


def main():
    guard_parent()
    stage, folder = sys.argv[1:]
    work = Path(folder)
    request = json.loads((work / "request.json").read_text())
    if stage in {"segment", "caption"}:
        caption_stage(stage, work, request)
    elif stage in {"terminal", "dense"}:
        reward_stage(stage, work, request)
    else:
        raise ValueError("Unknown FLORA stage")


if __name__ == "__main__":
    main()

"""Isolated GPU worker. Run with the administrator's pinned FLORA Python environments."""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import signal
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
    paths = {key: Path(value) for key, value in request["videos"].items()}
    videos, profile = DataEnginePipeline._inputs(
        paths["main"], "yam", paths["left"], paths["right"], False, ""
    )
    torch.set_num_threads(4)

    def progress(message, **kwargs):
        print(message, flush=True)

    session = models.Session()
    try:
        if stage == "segment":
            segments, audit = pipeline.segment(videos, "yam", session, "", progress)
        else:
            segments = json.loads((work / "segment.json").read_text())["segments"]
            segments, audit = pipeline.caption_three(videos, segments, session, "", progress, profile=profile)
            if not segments or any(item["status"] != "complete" for item in segments):
                raise RuntimeError("Incomplete FLORA captions")
        save(work / f"{stage}.json", {"segments": segments, "audit": audit, "model_revisions": models.MODELS})
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
            "videos": [request["videos"][key] for key in ("left", "main", "right")],
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

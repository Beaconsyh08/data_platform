"""Offline FLORA execution and review evidence, using the existing caption job protocol."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from lerobot.data_platform.temporal_caption import validate_result, write_json


@dataclass(frozen=True)
class FloraRuntime:
    caption_python: Path
    reward_python: Path
    caption_code: Path
    weights: Path
    lock_dir: Path
    gpu: int = 0
    stage_timeout_s: int = 3600

    @classmethod
    def load(cls) -> FloraRuntime:
        filename = os.environ.get("DATA_PLATFORM_FLORA_RUNTIME")
        if not filename or not Path(filename).is_absolute():
            raise ValueError("Configure DATA_PLATFORM_FLORA_RUNTIME with an absolute runtime JSON path")
        data = json.loads(Path(filename).read_text())
        config = cls(
            **{
                **data,
                **{
                    key: Path(data[key])
                    for key in ("caption_python", "reward_python", "caption_code", "weights", "lock_dir")
                },
            }
        )
        for name in ("caption_python", "reward_python", "caption_code", "weights", "lock_dir"):
            if not getattr(config, name).is_absolute():
                raise ValueError(f"FLORA {name} must be an absolute path")
        for executable in (config.caption_python, config.reward_python):
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ValueError("FLORA Python environment is unavailable")
        required = [
            config.caption_code / "batch_utils/api.py",
            config.weights / "rewarder/batch_utils/core.py",
        ]
        required += [
            config.weights / name / "config.json" for name in ("tempseg_base", "captioner", "rewarder")
        ]
        required += [config.weights / "tempseg/adapter_model.safetensors"]
        if any(not path.is_file() for path in required):
            raise ValueError("FLORA code or offline weights are incomplete")
        if type(config.gpu) is not int or config.gpu < 0:
            raise ValueError("FLORA GPU must be a non-negative physical index")
        if type(config.stage_timeout_s) is not int or not 60 <= config.stage_timeout_s <= 86400:
            raise ValueError("FLORA stage timeout must be between 60 and 86400 seconds")
        return config

    def payload(self) -> dict:
        return {name: str(value) if isinstance(value, Path) else value for name, value in vars(self).items()}


def runtime_capabilities() -> dict:
    try:
        FloraRuntime.load()
        configured = True
    except (OSError, ValueError, TypeError, KeyError):
        configured = False
    return {"caption_flora_protocol": 1, "caption_flora_configured": configured}


def camera_roles(cameras: list[str]) -> dict[str, str]:
    """Accept named left/right wrists with an optional scene camera."""
    if len(cameras) not in {2, 3}:
        raise ValueError("FLORA requires left/right wrist views and an optional scene view")
    left = [name for name in cameras if "left" in name.lower()]
    right = [name for name in cameras if "right" in name.lower()]
    main = [name for name in cameras if name not in left + right]
    if len(left) != 1 or len(right) != 1 or len(main) != len(cameras) - 2 or left == right:
        raise ValueError("FLORA needs unambiguous left/right camera names")
    return {"left": left[0], **({"main": main[0]} if main else {}), "right": right[0]}


def disagreement_intervals(windows: list[dict], count: int) -> list[dict]:
    spans = []
    start = None
    for frame in range(count + 1):
        conflict = frame < count and len({w["scores"][frame] for w in windows if w["scores"][frame]}) > 1
        if conflict and start is None:
            start = frame
        elif not conflict and start is not None:
            spans.append({"start_frame": start, "end_frame": frame})
            start = None
    return spans


def review_partition(native: list[dict], count: int, fps: float) -> dict:
    """Preserve native seconds; explicitly label gaps/overlaps in the frame-based review partition."""
    if not isinstance(native, list) or not native or len(native) > 500:
        raise ValueError("FLORA requires 1–500 complete native segments")
    intervals = []
    for item in native:
        if not isinstance(item, dict):
            raise ValueError("Invalid native FLORA caption record")
        start, end = item.get("start_s"), item.get("end_s")
        if (
            type(start) not in (float, int)
            or type(end) not in (float, int)
            or not math.isfinite(start)
            or not math.isfinite(end)
            or not 0 <= start < end <= count / fps + 1e-5
            or not isinstance(item.get("caption"), str)
            or not item["caption"].strip()
            or item.get("status") != "complete"
        ):
            raise ValueError("Invalid or incomplete native FLORA caption")
        intervals.append((min(count, math.ceil(start * fps - 1e-7)), min(count, math.ceil(end * fps - 1e-7))))
    boundaries = sorted({0, count, *(v for interval in intervals for v in interval)})
    segments = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        sources = [i for i, (a, b) in enumerate(intervals) if a <= start < b]
        caption = " / ".join(native[i]["caption"] for i in sources) or "Unlabeled interval"
        segments.append(
            {
                "start_frame": start,
                "end_frame": end,
                "caption": caption,
                "caption_en": caption,
                "evidence": "Native segment " + ", ".join(str(i + 1) for i in sources)
                if sources
                else "No caption returned for this interval",
                "uncertainty": "No native caption covers this interval"
                if not sources
                else ("Multiple native captions overlap" if len(sources) > 1 else ""),
                "confidence": "not_reported",
                "native_segment_indices": sources,
            }
        )
    return validate_result(
        {
            "summary": "; ".join(dict.fromkeys(item["caption"] for item in native)),
            "summary_en": "; ".join(dict.fromkeys(item["caption"] for item in native)),
            "segments": segments,
            "limitations": [
                "Model judgments, not human approval. No action/state signals supplied.",
                "Native seconds are retained. Review frame boundaries use ceil(time × FPS); gaps and overlaps are explicit.",
                "Raw dense windows can disagree. Official merged rewards are retained without smoothing.",
                "Two-wrist inputs use a synchronized montage. Long inputs use bounded chunks; chunk cuts are not semantic boundaries. See input_adaptation for layout and offsets.",
            ],
        },
        count,
        max_segments=1000,
    )


def validate_flora(artifact: dict) -> None:
    count, fps = artifact["frame_count"], artifact["fps"]
    if (
        type(count) is not int
        or not 1 <= count <= 36000
        or type(fps) not in (int, float)
        or not math.isfinite(fps)
        or fps <= 0
    ):
        raise ValueError("Invalid FLORA episode timing")
    if artifact.get("include_signals") is not False or artifact.get("pipeline_version") != "flora-1":
        raise ValueError("Invalid FLORA pipeline or signal policy")
    if artifact["result"] != review_partition(artifact["native_segments"], count, fps):
        raise ValueError("FLORA review partition does not match native captions")
    reward = artifact["reward"]
    if not isinstance(reward, dict) or not isinstance(reward.get("terminal"), dict):
        raise ValueError("Missing FLORA reward evidence")
    terminal = reward["terminal"]
    if type(terminal.get("score")) is not int or not 1 <= terminal["score"] <= 5:
        raise ValueError("Invalid terminal reward")
    if not isinstance(terminal.get("rationale"), str) or not terminal["rationale"].strip():
        raise ValueError("Missing terminal reward rationale")
    if reward.get("human_reviewed") is not False:
        raise ValueError("FLORA rewards must remain unreviewed")
    windows = reward["windows"]
    if not isinstance(windows, list) or not 1 <= len(windows) <= 1000:
        raise ValueError("Missing or excessive reward windows")
    if any(not isinstance(w, dict) for w in windows):
        raise ValueError("Invalid reward window record")
    tracks = [reward["merged_scores"], *[w["scores"] for w in windows]]
    for i, scores in enumerate(tracks):
        allowed = {1, 2, 3} if i == 0 else {0, 1, 2, 3}
        if (
            not isinstance(scores, list)
            or len(scores) != count
            or any(type(s) is not int or s not in allowed for s in scores)
        ):
            raise ValueError("Invalid reward frame scores")
    sources = reward["merged_source_window"]
    if len(sources) != count or any(type(i) is not int or not 0 <= i < len(windows) for i in sources):
        raise ValueError("Invalid merged reward provenance")
    if any(not isinstance(w.get("output"), str) or not w["output"] for w in windows):
        raise ValueError("Missing raw reward window output")
    if reward["disagreement_intervals"] != disagreement_intervals(windows, count):
        raise ValueError("Reward disagreement intervals do not match raw windows")


def build_artifact(meta: dict, dataset_key: str, native: dict, reward: dict, metrics: list[dict]) -> dict:
    value = {
        "schema_version": 1,
        "dataset_key": dataset_key,
        "scheme_id": "flora",
        "variant": "caption",
        "episode_index": meta["episode_index"],
        "frame_count": meta["frame_count"],
        "fps": meta["fps"],
        "duration_s": meta["frame_count"] / meta["fps"],
        "created_at": time.time(),
        "model": "FLORA TempSeg / Captioner / Rewarder",
        "pipeline_version": "flora-1",
        "include_signals": False,
        "source_digest": meta.get("source_digest"),
        "native_segments": native["segments"],
        "model_revisions": native.get("model_revisions", {}),
        "result": review_partition(native["segments"], meta["frame_count"], meta["fps"]),
        "reward": reward,
        "stage_metrics": metrics,
        "input_adaptation": native.get("input_adaptation", "native_three_view"),
    }
    validate_flora(value)
    return value


def run_stage(config: FloraRuntime, stage: str, workspace: Path, progress) -> dict:
    """Own the whole subprocess group, including vLLM workers, through cancellation and failure."""
    interpreter = config.caption_python if stage in {"segment", "caption"} else config.reward_python
    env = {
        **os.environ,
        "CUDA_VISIBLE_DEVICES": str(config.gpu),
        "FLORA_PARENT_PID": str(os.getpid()),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HOME": str(config.lock_dir / "hf-cache"),
    }
    started = time.monotonic()
    peak = 0
    worker = Path(__file__).with_name("temporal_caption_flora_worker.py")
    with (workspace / f"{stage}.log").open("w") as log:
        process = subprocess.Popen(
            [str(interpreter), str(worker), stage, str(workspace)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                progress(f"FLORA {stage} · {int(time.monotonic() - started)}s")
                if time.monotonic() - started > config.stage_timeout_s:
                    raise TimeoutError(f"FLORA {stage} exceeded its configured time limit")
                usage = subprocess.run(
                    [
                        "nvidia-smi",
                        "-i",
                        str(config.gpu),
                        "--query-gpu=memory.used",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if usage.returncode == 0:
                    peak = max(peak, int(usage.stdout.strip()))
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            if process.returncode:
                from lerobot.data_platform.operation_log import sanitize_for_log

                log.flush()
                with (workspace / f"{stage}.log").open("rb") as diagnostic:
                    diagnostic.seek(max(0, diagnostic.seek(0, 2) - 4000))
                    detail = sanitize_for_log(diagnostic.read().decode(errors="replace"))
                raise RuntimeError(f"FLORA {stage} failed (exit {process.returncode}): {detail}")
        finally:
            # The leader may already have exited while a model worker still holds GPU memory.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                pass
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return {
        "stage": stage,
        "elapsed_s": time.monotonic() - started,
        "device_peak_mib": peak,
        "precision": "native_bf16_with_fp32_sensitive_parameters"
        if stage in {"segment", "caption"}
        else "native_bf16",
        "quantization": None,
    }


def infer_flora(bundle: Path, output: Path, dataset_key: str, model: str = "", *, client=None, progress=None):
    if output.exists():
        raise FileExistsError(output)
    config = FloraRuntime.load()
    progress = progress or (lambda message: None)
    meta = json.loads((bundle / "episode.json").read_text())
    if meta.get("include_signals") is not False:
        raise ValueError("FLORA requires an explicitly visual-only bundle")
    count, fps = meta["frame_count"], meta["fps"]
    if (
        type(count) is not int
        or not 1 <= count <= 36000
        or type(fps) not in (int, float)
        or not math.isfinite(fps)
        or fps <= 0
        or count / fps > 600
    ):
        raise ValueError("FLORA requires at most 600 seconds / 36000 frames with valid FPS")
    roles = camera_roles(meta["cameras"])
    task = "; ".join(meta.get("task_hint", [])).strip()
    if not task:
        raise ValueError("FLORA Rewarder requires a recorded task instruction")
    config.lock_dir.mkdir(parents=True, exist_ok=True)
    work = bundle / "flora"
    work.mkdir()
    write_json(
        work / "request.json",
        {
            "runtime": config.payload(),
            "frame_count": meta["frame_count"],
            "fps": meta["fps"],
            "task": task,
            "videos": {
                role: str(bundle / "camera-videos" / f"{meta['cameras'].index(camera)}.mp4")
                for role, camera in roles.items()
            },
        },
    )
    metrics = []
    with (config.lock_dir / f"gpu-{config.gpu}.lock").open("a") as lock:
        waiting = time.monotonic()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                progress("FLORA waiting for its configured GPU")
                if time.monotonic() - waiting > config.stage_timeout_s:
                    raise TimeoutError("FLORA GPU remained busy") from None
                time.sleep(5)
        available = subprocess.run(
            [
                "nvidia-smi",
                "-i",
                str(config.gpu),
                "--query-gpu=memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        total, free = map(int, available.stdout.strip().split(","))
        if total < 75000 or free < 73000:
            raise ValueError("Native FLORA pipeline requires a free 80 GB GPU (at least 73000 MiB available)")
        for stage in ("segment", "caption", "terminal", "dense"):
            metrics.append(run_stage(config, stage, work, progress))
    artifact = build_artifact(
        meta,
        dataset_key,
        json.loads((work / "caption.json").read_text()),
        json.loads((work / "reward.json").read_text()),
        metrics,
    )
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(bundle / "video.mp4", output / "video.mp4")
    write_json(output / "episode.json", meta)
    artifact["native_segmentation"] = json.loads((work / "segment.json").read_text())["segments"]
    write_json(output / "caption.json", artifact)

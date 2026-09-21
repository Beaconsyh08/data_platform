"""Independent, single-episode temporal caption experiment and portable review artifacts.

Prepare runs on the data host; infer runs on the host holding the runtime API key.
Neither phase writes into the source dataset. No existing annotation algorithm is used.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import shutil
import subprocess
import time
from pathlib import Path

PROMPT = """You annotate ONE robot demonstration using synchronized camera observations and measured signals.
The three panels are labeled by camera name. Each image is one instant, not three consecutive frames.
Track object identity across cameras and time BEFORE segmenting. The same object can appear in both wrist
cameras; simultaneous views are not evidence of two separate objects. Check for handovers, shared grasps,
regrasping and release between manipulators. Only claim a new object when it is visibly distinct.
UMI demonstrations can be operated by human hands; describe manipulators rather than autonomous robot intent.
Use the actual observed behavior, not the task instruction as a script or proof of success.
Partition the entire episode into contiguous, non-overlapping semantic action segments. Choose the number
of segments from evidence; do not use equal time intervals or a predefined pick/place stage template.
Create boundaries when interaction, object state, hand coordination, or manipulation intent visibly changes.
Include idle/recovery/failed attempts when observed. Distinguish left/right manipulator from image left/right.
Pose values are observations, NOT commanded actions. Coordinate axes and gripper polarity/units are
unverified. Relative motion and gripper transitions suggest candidate boundaries, but do not prove contact,
release, upward motion, or success. Do not name an occluded object more specifically than the images support.
Caption visible actions in concise Chinese and English. State uncertainty and do not invent hidden intent.
Return only JSON: {"summary": "Chinese episode description", "segments": [
{"start_frame": 0, "end_frame": 100, "caption": "Chinese caption", "caption_en": "English caption",
"evidence": "Specific visible events and signal changes with frame references",
"confidence": "high|medium|low", "uncertainty": "What cannot be established"}],
"limitations": ["Limitations of these observations"]}.
Frame intervals are [start_frame, end_frame), integer episode-local frame indices. The first start is 0,
the final end equals frame_count, adjacent endpoints match. Boundary precision must reflect sampling;
confidence is subjective, not a calibrated probability. Avoid oversegmentation of a continuous action.
"""


def artifact_key(dataset_key: str) -> str:
    return hashlib.sha256(dataset_key.encode()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    temporary.replace(path)


def validate_result(result: dict, frame_count: int) -> dict:
    if type(frame_count) is not int or frame_count <= 0:
        raise ValueError("Invalid frame count")
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
        raise ValueError("Missing episode summary")
    segments = result.get("segments")
    if not isinstance(segments, list) or not segments or len(segments) > 40:
        raise ValueError("Expected 1–40 segments")
    previous = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Invalid segment record")
        start, end = segment.get("start_frame"), segment.get("end_frame")
        if (
            type(start) is not int
            or type(end) is not int
            or start != previous
            or not start < end <= frame_count
        ):
            raise ValueError("Segments must cover the episode contiguously using half-open frame intervals")
        for key in ("caption", "caption_en", "evidence", "uncertainty"):
            if not isinstance(segment.get(key), str) or (key != "uncertainty" and not segment[key].strip()):
                raise ValueError(f"Invalid {key}")
        if segment.get("confidence") not in {"high", "medium", "low", "not_reported"}:
            raise ValueError("Invalid confidence")
        previous = end
    if previous != frame_count:
        raise ValueError("Segments do not cover the complete episode")
    if not isinstance(result.get("limitations"), list) or not all(
        isinstance(item, str) for item in result["limitations"]
    ):
        raise ValueError("Invalid limitations")
    return result


def prepare(root: Path, episode: int, output: Path) -> None:
    """Export exactly one image-backed v3 episode using the shared format reader."""
    import numpy as np
    from PIL import Image, ImageDraw

    from lerobot.data_platform.precompute.dataset_io import V3DatasetMetadata, read_episode_table

    info = json.loads((root / "meta/info.json").read_text())
    if not str(info.get("codebase_version", "")).startswith("v3"):
        raise ValueError("This trial exporter supports image-backed v3 datasets")
    meta = V3DatasetMetadata(f"local/{root.name}", root)
    cameras = [key for key, feature in meta.features.items() if feature.get("dtype") == "image"]
    if not cameras or len(cameras) > 4:
        raise ValueError("Expected 1–4 image cameras")
    table = read_episode_table(root, meta, episode).sort_by("frame_index")
    rows = table.to_pylist()
    if not rows or [row["frame_index"] for row in rows] != list(range(len(rows))):
        raise ValueError("Expected contiguous episode-local frame indices")
    fps = float(meta.fps)
    timestamps = np.array([row["timestamp"] for row in rows], dtype=float)
    if (
        not math.isfinite(fps)
        or fps <= 0
        or not np.allclose(timestamps - timestamps[0], np.arange(len(rows)) / fps, atol=0.002)
    ):
        raise ValueError("Export requires regular timestamps matching dataset FPS")
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError("Output must be outside the source dataset")
    output.mkdir(parents=True, exist_ok=False)
    try:
        frames = output / "frames"
        frames.mkdir()
        signal_keys = [key for key in rows[0] if "pose" in key or "gripper" in key or key == "action"]
        signals = [{"frame": i, **{key: row[key] for key in signal_keys}} for i, row in enumerate(rows)]
        source_digest = hashlib.sha256(json.dumps(signals, sort_keys=True).encode())
        for i, row in enumerate(rows):
            canvas = Image.new("RGB", (224 * len(cameras), 248), "#0f172a")
            draw = ImageDraw.Draw(canvas)
            for j, camera in enumerate(cameras):
                value = row[camera]
                if value.get("bytes"):
                    source = io.BytesIO(value["bytes"])
                else:
                    source = (root / value["path"]).resolve()
                    if not source.is_relative_to(root.resolve()):
                        raise ValueError("Image path escapes dataset root")
                with Image.open(source) as image:
                    canvas.paste(image.convert("RGB").resize((224, 224)), (224 * j, 24))
                draw.text((224 * j + 5, 5), f"{camera}  f={i}", fill="white")
            canvas.save(frames / f"{i:06d}.jpg", quality=90)
            source_digest.update((frames / f"{i:06d}.jpg").read_bytes())
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-framerate",
                str(fps),
                "-i",
                str(frames / "%06d.jpg"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "18",
                "-movflags",
                "+faststart",
                str(output / "video.mp4"),
            ],
            check=True,
        )
        write_json(
            output / "episode.json",
            {
                "schema_version": 1,
                "dataset_name": root.name,
                "episode_index": episode,
                "frame_count": len(rows),
                "fps": fps,
                "duration_s": len(rows) / fps,
                "task_hint": meta.episodes[episode].get("tasks", []),
                "cameras": cameras,
                "signal_features": {key: meta.features[key] for key in signal_keys},
                "signals": signals,
                "source_digest": source_digest.hexdigest(),
            },
        )
    except Exception:
        shutil.rmtree(output)
        raise


def sample_frames(frame_count: int, fps: float, previous: dict | None = None) -> list[int]:
    stride = max(1, round(fps / 2))
    selected = set(range(0, frame_count, stride)) | {frame_count - 1}
    if previous:
        for segment in previous["segments"][1:]:
            center = segment["start_frame"]
            selected.update(
                range(
                    max(0, center - round(fps)), min(frame_count, center + round(fps)), max(1, round(fps / 8))
                )
            )
    return sorted(selected)


def infer(
    bundle: Path, output: Path, dataset_key: str, model: str = "qwen3.8-max", *, client=None, progress=None
) -> None:
    from lerobot.data_platform.qwen import QwenClient

    meta = json.loads((bundle / "episode.json").read_text())
    client = client or QwenClient(timeout_s=300)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(bundle / "video.mp4", output / "video.mp4")
    write_json(output / "episode.json", meta)
    previous = None
    for variant in ("coarse", "refined"):
        if progress:
            progress("Initial segmentation" if variant == "coarse" else "Boundary review")
        selected = sample_frames(meta["frame_count"], meta["fps"], previous)
        if len(selected) > 200:
            raise ValueError("Trial exceeds the 200-image budget per request")
        evidence = {key: value for key, value in meta.items() if key != "signals"}
        # 4 Hz signals include the full pose/gripper observations, without assuming physical units.
        evidence["signals"] = [
            {
                key: ([round(float(v), 4) for v in value] if isinstance(value, list) else round(value, 4))
                for key, value in row.items()
                if "quaternion" not in key
            }
            for row in meta["signals"][:: max(1, round(meta["fps"] / 4))]
        ]
        instructions = PROMPT
        if previous:
            instructions += (
                "\nReview your provisional partition against denser observations near each boundary. "
                "Correct captions/boundaries, merge or split only with evidence. "
                "Independently re-evaluate object identities and any handover; earlier captions may be wrong. "
                "Do not claim frame-exact contact between sampled images.\nCandidate boundaries:\n"
                + json.dumps([s["start_frame"] for s in previous["segments"]])
            )
        text = instructions + "\nEpisode observations:\n" + json.dumps(evidence, ensure_ascii=False)
        content = [{"type": "text", "text": text}]
        for frame in selected:
            content.append({"type": "text", "text": f"Frame {frame}; t={frame / meta['fps']:.3f}s"})
            encoded = base64.b64encode((bundle / "frames" / f"{frame:06d}.jpg").read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        started = time.time()
        response = client.post_chat_completion(
            {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "response_format": {"type": "json_object"},
                "max_tokens": 8000,
                "enable_thinking": False,
            }
        )
        parsed = json.loads(response["choices"][0]["message"]["content"])
        result = validate_result(parsed, meta["frame_count"])
        artifact = {
            "schema_version": 1,
            "scheme_id": "multiview_semantics",
            "dataset_key": dataset_key,
            "dataset_name": meta["dataset_name"],
            "episode_index": meta["episode_index"],
            "frame_count": meta["frame_count"],
            "fps": meta["fps"],
            "duration_s": meta["duration_s"],
            "variant": variant,
            "model": model,
            "created_at": time.time(),
            "elapsed_s": time.time() - started,
            "usage": response.get("usage"),
            "sampled_frames": selected,
            "source_digest": meta["source_digest"],
            "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "result": result,
        }
        (output / f"{variant}.prompt.txt").write_text(text)
        write_json(output / f"{variant}.json", artifact)
        print(
            json.dumps(
                {"variant": variant, "segments": len(result["segments"]), "usage": response.get("usage")}
            ),
            flush=True,
        )
        previous = result


def preview(artifact_root: Path, dataset_key: str, port: int) -> None:
    """Serve an isolated local review using the same routes and UI as the console."""
    import os
    from types import SimpleNamespace

    from flask import Flask, redirect

    from lerobot.data_platform.routes.temporal_caption import register_temporal_caption_routes

    if len(dataset_key.split("/")) != 2 or any(part in {"", ".", ".."} for part in dataset_key.split("/")):
        raise ValueError("Expected namespace/dataset")
    os.environ["DATA_PLATFORM_TEMPORAL_CAPTION_ROOT"] = str(artifact_root.resolve())
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    ctx = SimpleNamespace(
        control_plane_store=None,
        repo_key=lambda key: tuple(key.split("/", 1)),
        static_dir_for_key=lambda key: artifact_root if "/".join(key) == dataset_key else None,
    )
    register_temporal_caption_routes(app, ctx)
    app.add_url_rule("/", "home", lambda: redirect(f"/{dataset_key}/temporal-caption"))
    app.run(host="127.0.0.1", port=port, debug=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("prepare")
    export.add_argument("--root", type=Path, required=True)
    export.add_argument("--episode", type=int, required=True)
    export.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("infer")
    run.add_argument("--bundle", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--dataset-key", required=True)
    run.add_argument("--model", default="qwen3.8-max")
    run.add_argument(
        "--scheme",
        choices=["multiview_semantics", "video_events", "fusion_review"],
        default="multiview_semantics",
    )
    review = commands.add_parser("preview")
    review.add_argument("--artifact-root", type=Path, required=True)
    review.add_argument("--dataset-key", required=True)
    review.add_argument("--port", type=int, default=8766)
    demo = commands.add_parser("import-demo")
    demo.add_argument("--source", type=Path, required=True)
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--dataset-key", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.root, args.episode, args.output)
    elif args.command == "infer":
        if args.scheme == "fusion_review":
            from lerobot.data_platform.temporal_caption_fusion import infer_fusion

            infer_fusion(args.bundle, args.output, args.dataset_key, args.model)
        elif args.scheme == "video_events":
            from lerobot.data_platform.temporal_caption_demo import infer_video_events

            infer_video_events(args.bundle, args.output, args.dataset_key, args.model)
        else:
            infer(args.bundle, args.output, args.dataset_key, args.model)
    elif args.command == "import-demo":
        from lerobot.data_platform.temporal_caption_demo import import_demo

        import_demo(args.source, args.output, args.dataset_key)
    else:
        preview(args.artifact_root, args.dataset_key, args.port)


if __name__ == "__main__":
    main()

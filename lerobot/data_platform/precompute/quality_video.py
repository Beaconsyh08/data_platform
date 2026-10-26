"""Bounded, read-only quality inspection of source images and episode video ranges."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np
from PIL import Image

from lerobot.data_platform.execution_pools import check_stop
from lerobot.data_platform.precompute.dataset_io import _decode_image_value, read_episode_table
from lerobot.data_platform.precompute.quality_signals import QualityIssue, finite_number, signal_issue


@dataclass(frozen=True)
class VideoQualityConfig:
    sample_every_seconds: float = 1.0
    black_mean: float = 4.0
    black_pixel: float = 16.0
    black_fraction: float = 0.98
    blur_variance: float = 20.0
    freeze_delta: float = 0.5
    freeze_seconds: float = 2.0

    @classmethod
    def parse(cls, value: dict) -> VideoQualityConfig:
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown C3 configuration")
        values = {key: finite_number(item, f"c3.{key}", minimum=0) for key, item in value.items()}
        result = cls(**values)
        if result.sample_every_seconds <= 0 or result.freeze_seconds <= 0:
            raise ValueError("C3 sampling and freeze durations must be positive")
        if result.black_mean > 255 or result.black_pixel > 255 or result.black_fraction > 1:
            raise ValueError("C3 black thresholds are outside the pixel/fraction range")
        return result


@dataclass(frozen=True)
class VideoScanResult:
    episode: int
    camera: str
    mode: str
    status: str
    expected_frames: int
    decoded_frames: int
    checked_frames: int
    checked_intervals: tuple[tuple[int, int], ...]
    issues: tuple[QualityIssue, ...]

    def coverage(self) -> dict:
        return {
            "episode": self.episode,
            "camera": self.camera,
            "detector": "c3",
            "mode": self.mode,
            "status": self.status,
            "expected_frames": self.expected_frames,
            "decoded_frames": self.decoded_frames,
            "checked_frames": self.checked_frames,
            "coverage_fraction": min(1.0, self.checked_frames / self.expected_frames)
            if self.expected_frames
            else 0.0,
            "checked_intervals": self.checked_intervals,
        }


def scan_camera_quality(
    root: Path,
    meta,
    episode: int,
    camera: str,
    timestamps: np.ndarray,
    mode: str,
    config: VideoQualityConfig,
) -> VideoScanResult:
    """Decode sequentially with one native thread; never inspect a v3 neighbour's frames."""
    feature = meta.features[camera]
    count = len(timestamps)
    issues = []
    flags: dict[str, list[int]] = {"black_frame": [], "blur_candidate": [], "freeze_candidate": []}
    observed = {reason: {} for reason in flags}
    checked = []
    decoded = 0
    previous = None
    previous_frame = None
    freeze_start = None
    freeze_frames = []
    fps = float(meta.info.get("fps") or 0)
    stride = max(1, round(config.sample_every_seconds * fps)) if mode == "sampled" else 1

    def inspect(index: int, image: Image.Image):
        nonlocal previous, previous_frame, freeze_start, freeze_frames
        if index % stride and index != count - 1:
            return
        # Fixed working resolution bounds per-frame allocations and makes thresholds reproducible.
        gray = np.asarray(image.convert("L").resize((160, 120)), dtype=np.float32)
        checked.append(index)
        mean, fraction = float(gray.mean()), float(np.mean(gray <= config.black_pixel))
        if mean <= config.black_mean and fraction >= config.black_fraction:
            flags["black_frame"].append(index)
            observed["black_frame"]["observed_mean_max"] = max(
                mean, observed["black_frame"].get("observed_mean_max", mean)
            )
            observed["black_frame"]["observed_dark_fraction_min"] = min(
                fraction, observed["black_frame"].get("observed_dark_fraction_min", fraction)
            )
        laplacian = gray[1:-1, :-2] + gray[1:-1, 2:] + gray[:-2, 1:-1] + gray[2:, 1:-1] - 4 * gray[1:-1, 1:-1]
        variance = float(laplacian.var())
        if variance < config.blur_variance:
            flags["blur_candidate"].append(index)
            observed["blur_candidate"]["observed_variance_min"] = min(
                variance, observed["blur_candidate"].get("observed_variance_min", variance)
            )
        valid_time = np.isfinite(timestamps[index])
        if previous is not None and valid_time and np.isfinite(timestamps[previous_frame]):
            elapsed = timestamps[index] - timestamps[previous_frame]
            delta = float(np.mean(np.abs(gray - previous)))
            if elapsed > 0 and delta <= config.freeze_delta:
                if freeze_start is None:
                    freeze_start = previous_frame
                    freeze_frames = [previous_frame]
                freeze_frames.append(index)
                if timestamps[index] - timestamps[freeze_start] >= config.freeze_seconds:
                    flags["freeze_candidate"].extend(freeze_frames)
                    observed["freeze_candidate"]["observed_duration_max"] = float(
                        timestamps[index] - timestamps[freeze_start]
                    )
                    freeze_frames = []
            else:
                freeze_start = None
                freeze_frames = []
        else:
            freeze_start = None
            freeze_frames = []
        previous, previous_frame = gray, index

    status = "complete" if mode == "full" else "sampled"
    try:
        if not count:
            raise ValueError("Episode has no frames to inspect")
        if feature.get("dtype") == "video":
            path = root / meta.get_video_file_path(episode, camera)
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Source media path leaves the registered dataset")
            start, end = meta.get_video_time_range(episode, camera)
            if not np.isfinite([start, end]).all() or end <= start or fps <= 0:
                raise ValueError("Invalid episode video time range or FPS")
            with av.open(str(path)) as container:
                stream = container.streams.video[0]
                stream.thread_count = 1
                container.seek(int(start / stream.time_base), stream=stream, backward=True)
                first_time = None
                seen = set()
                for frame in container.decode(stream):
                    check_stop()
                    if frame.pts is None:
                        raise ValueError("Video frame has no presentation timestamp")
                    time = float(frame.pts * frame.time_base)
                    if time < start - 1e-6:
                        continue
                    if time >= end - 1e-6:
                        break
                    if first_time is None:
                        first_time = time
                    index = int(round((time - start) * fps))
                    if index < 0 or index >= count:
                        continue
                    if index in seen:
                        raise ValueError("Duplicate video timestamp mapping")
                    seen.add(index)
                    decoded += 1
                    if index % stride == 0 or index == count - 1:
                        inspect(index, frame.to_image())
                if decoded != count or (first_time is not None and first_time > start + 0.51 / fps):
                    status = "incomplete"
                    issues.append(
                        signal_issue(
                            episode,
                            "c3",
                            "video_frame_count_mismatch",
                            [],
                            timestamps,
                            camera=camera,
                            metrics={"expected_frames": count, "decoded_frames": decoded},
                        )
                    )
        else:
            table = read_episode_table(root, meta, episode, columns=[camera])
            for index, value in enumerate(table[camera].to_pylist()):
                check_stop()
                if index % stride and index != count - 1:
                    continue
                try:
                    image_path = (
                        value.get("path")
                        if isinstance(value, dict) and value.get("bytes") is None
                        else value
                        if isinstance(value, (str, Path))
                        else None
                    )
                    if image_path and not (root / str(image_path)).resolve().is_relative_to(root.resolve()):
                        raise ValueError("Source image path leaves the registered dataset")
                    image = _decode_image_value(value, root)
                    decoded += 1
                    inspect(index, image)
                except (OSError, ValueError):
                    status = "incomplete"
                    issues.append(
                        signal_issue(episode, "c3", "image_decode_error", [index], timestamps, camera=camera)
                    )
    except (OSError, ValueError, IndexError, KeyError, av.error.FFmpegError):
        status = "error"
        issues.append(
            signal_issue(
                episode,
                "c3",
                "media_decode_error",
                [],
                timestamps,
                camera=camera,
                metrics={"decoded_frames": decoded},
            )
        )
    for reason, frames in flags.items():
        if frames:
            issues.append(
                signal_issue(
                    episode,
                    "c3",
                    reason,
                    frames,
                    timestamps,
                    camera=camera,
                    severity="error" if reason == "black_frame" else "warning",
                    metrics={
                        "mode": mode,
                        "analysis_resolution": [160, 120],
                        "black_mean": config.black_mean,
                        "black_fraction": config.black_fraction,
                        "black_pixel": config.black_pixel,
                        "blur_variance": config.blur_variance,
                        "freeze_delta": config.freeze_delta,
                        "freeze_seconds": config.freeze_seconds,
                        **observed[reason],
                    },
                )
            )
    coverage = signal_issue(episode, "c3", "coverage", checked, timestamps)
    return VideoScanResult(
        episode, camera, mode, status, count, decoded, len(checked), coverage.frame_intervals, tuple(issues)
    )

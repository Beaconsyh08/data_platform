"""Consistent left / scene / right camera presentation without changing source videos."""

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path


def camera_rank(name: str) -> int:
    tokens = set(re.split(r"[._/\-]+", name.lower()))
    if "left" in tokens:
        return 0
    if "right" in tokens:
        return 2
    if tokens & {"head", "scene", "image", "front"}:
        return 1
    return 3


def ordered_review_video(source: Path, cameras: list[str], cache: Path) -> Path:
    order = sorted(range(len(cameras)), key=lambda i: camera_rank(cameras[i]))
    if len(cameras) != 3 or order == list(range(3)):
        return source
    stat = source.stat()
    digest = hashlib.sha256(f"{source}:{stat.st_mtime_ns}:{stat.st_size}:{order}".encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{digest}.mp4"
    if target.exists():
        return target
    filters = ";".join(f"[0:v]crop=iw/3:ih:iw*{index}/3:0[v{i}]" for i, index in enumerate(order))
    filters += ";[v0][v1][v2]hstack=inputs=3[out]"
    with tempfile.TemporaryDirectory(dir=cache) as temporary:
        output = Path(temporary) / "video.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(source),
                "-filter_complex",
                filters,
                "-map",
                "[out]",
                "-an",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-preset",
                "fast",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        output.replace(target)
    return target

"""Bounded streaming encoders for native caption camera and review videos."""

from __future__ import annotations

import contextlib
import os
from fractions import Fraction
from pathlib import Path


@contextlib.contextmanager
def video_encoder(path: Path, size: tuple[int, int], fps: float, *, lossless: bool = False):
    """Encode frames directly, retaining native camera dimensions and frame order."""
    import av

    with av.open(str(path), "w", options={"movflags": "+faststart"}) as container:
        stream = container.add_stream("libx264", rate=Fraction(str(fps)).limit_denominator(100000))
        stream.width, stream.height = size
        stream.pix_fmt = "yuv444p" if lossless else "yuv420p"
        stream.thread_count = max(1, int(os.environ.get("DATA_PLATFORM_VIDEO_THREADS", "2")))
        stream.options = {"crf": "0" if lossless else "18", "preset": "veryfast"}

        def encode(image):
            if image.size != size:
                raise ValueError("Camera dimensions changed within the episode")
            frame = av.VideoFrame.from_image(image)
            for packet in stream.encode(frame):
                container.mux(packet)

        yield encode
        for packet in stream.encode():
            container.mux(packet)

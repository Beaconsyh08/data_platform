"""Content invalidation, streaming parity and cross-process GPU ownership."""

import fcntl
import json
import multiprocessing
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lerobot.data_platform import temporal_caption_snapshot as snapshots
from lerobot.data_platform.lifecycle import build_content_manifest
from lerobot.data_platform.temporal_caption_flora import FloraRuntime, acquire_gpu


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "meta").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "meta/info.json").write_text(json.dumps({"codebase_version": "v2.1"}))
    (root / "data/episode.parquet").write_bytes(b"content")
    return root


def test_cached_digest_matches_full_manifest_and_reuses_bytes(source, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    original = build_content_manifest(source)["dataset_fingerprint"]
    assert snapshots.caption_source_fingerprint(source, cache) == original
    monkeypatch.setattr(snapshots, "_file_sha256", lambda path: pytest.fail("unchanged bytes rehashed"))
    assert snapshots.caption_source_fingerprint(source, cache) == original


def test_changes_with_restored_mtime_and_replaced_inode_invalidate(source, tmp_path):
    cache = tmp_path / "cache"
    path = source / "data/episode.parquet"
    before = snapshots.caption_source_fingerprint(source, cache)
    stat = path.stat()
    path.write_bytes(b"changed")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed = snapshots.caption_source_fingerprint(source, cache)
    assert changed != before
    assert changed == build_content_manifest(source)["dataset_fingerprint"]
    replacement = source / "data/new"
    replacement.write_bytes(b"replace")
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(path)
    assert snapshots.caption_source_fingerprint(source, cache) != changed
    path.unlink()
    assert (
        snapshots.caption_source_fingerprint(source, cache)
        == build_content_manifest(source)["dataset_fingerprint"]
    )
    path.write_bytes(b"new")
    assert (
        snapshots.caption_source_fingerprint(source, cache)
        == build_content_manifest(source)["dataset_fingerprint"]
    )


def test_mutation_during_hash_rejected_and_bad_cache_rebuilt(source, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    snapshots.caption_source_fingerprint(source, cache)
    next(cache.glob("*.json")).write_text("[]")
    original = snapshots._file_sha256

    def mutating(path):
        value = original(path)
        if path.name == "episode.parquet":
            path.write_bytes(b"concurrent change")
        return value

    monkeypatch.setattr(snapshots, "_file_sha256", mutating)
    with pytest.raises(ValueError, match="changed"):
        snapshots.caption_source_fingerprint(source, cache)
    monkeypatch.setattr(snapshots, "_file_sha256", original)
    assert (
        snapshots.caption_source_fingerprint(source, cache)
        == build_content_manifest(source)["dataset_fingerprint"]
    )


def test_symlink_target_changes_and_escape_rejected(source, tmp_path):
    path = source / "data/episode.parquet"
    (source / "data/link").symlink_to(path)
    cache = tmp_path / "cache"
    assert (
        snapshots.caption_source_fingerprint(source, cache)
        == build_content_manifest(source)["dataset_fingerprint"]
    )
    outside = tmp_path / "outside"
    outside.write_text("not authorized")
    (source / "data/link").unlink()
    (source / "data/link").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        snapshots.caption_source_fingerprint(source, cache)


def runtime(tmp_path, count=4):
    return FloraRuntime(tmp_path, tmp_path, tmp_path, tmp_path, tmp_path, gpus=tuple(range(count)))


def _hold_gpu(config, ready, release):
    with acquire_gpu(config, lambda _: None) as selected:
        ready.put(selected.gpu)
        assert release.wait(15)


@pytest.mark.parametrize("count", [4, 8])
def test_parallel_processes_get_distinct_gpus(tmp_path, monkeypatch, count):
    from lerobot.data_platform import temporal_caption_flora as flora

    monkeypatch.setattr(flora.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="81920, 81916"))
    context = multiprocessing.get_context("fork")
    ready, release = context.Queue(), context.Event()
    workers = [
        context.Process(target=_hold_gpu, args=(runtime(tmp_path, count), ready, release))
        for _ in range(count)
    ]
    try:
        for worker in workers:
            worker.start()
        assert {ready.get(timeout=10) for _ in workers} == set(range(count))
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=15)
            if worker.is_alive():
                worker.kill()
                worker.join()
    assert all(worker.exitcode == 0 for worker in workers)


def test_old_executor_lock_and_busy_external_gpu_are_skipped(tmp_path, monkeypatch):
    from lerobot.data_platform import temporal_caption_flora as flora

    monkeypatch.setattr(
        flora.subprocess,
        "run",
        lambda args, **kw: SimpleNamespace(stdout="81920, 1000" if args[2] == "1" else "81920, 81916"),
    )
    with (tmp_path / "gpu-0.lock").open("a") as old:
        fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(RuntimeError, match="inference failed"),
            acquire_gpu(runtime(tmp_path), lambda _: None) as selected,
        ):
            assert selected.gpu == 2
            raise RuntimeError("inference failed")
        with acquire_gpu(runtime(tmp_path), lambda _: None) as selected:
            assert selected.gpu == 2


def test_process_exit_releases_gpu_lock(tmp_path, monkeypatch):
    from lerobot.data_platform import temporal_caption_flora as flora

    monkeypatch.setattr(flora.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="81920, 81916"))
    context = multiprocessing.get_context("fork")
    ready, release = context.Queue(), context.Event()
    config = runtime(tmp_path, 1)
    worker = context.Process(target=_hold_gpu, args=(config, ready, release))
    worker.start()
    try:
        assert ready.get(timeout=10) == 0
    finally:
        worker.kill()
        worker.join(timeout=10)
    with acquire_gpu(config, lambda _: None) as selected:
        assert selected.gpu == 0


def test_gpu_pool_timeout_when_all_locked(tmp_path, monkeypatch):
    from lerobot.data_platform import temporal_caption_flora as flora

    ticks = iter([0, 61])
    monkeypatch.setattr(flora.time, "monotonic", lambda: next(ticks))
    with (tmp_path / "gpu-0.lock").open("a") as old:
        fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(TimeoutError),
            acquire_gpu(replace(runtime(tmp_path, 1), stage_timeout_s=60), lambda _: None),
        ):
            pytest.fail("acquired a locked GPU")


def test_streaming_camera_pixels_match_previous_lossless_encoder(tmp_path, monkeypatch):
    import subprocess

    import av
    import numpy as np
    from PIL import Image

    from lerobot.data_platform.temporal_caption_media import video_encoder

    monkeypatch.setenv("DATA_PLATFORM_VIDEO_THREADS", "1")
    rng = np.random.default_rng(2)
    frames = [Image.fromarray(rng.integers(0, 256, (16, 24, 3), dtype=np.uint8)) for _ in range(4)]
    for i, frame in enumerate(frames):
        frame.save(tmp_path / f"{i:06d}.png")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-framerate",
            "30",
            "-i",
            str(tmp_path / "%06d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv444p",
            "-crf",
            "0",
            str(tmp_path / "old.mp4"),
        ],
        check=True,
    )
    with video_encoder(tmp_path / "new.mp4", (24, 16), 30, lossless=True) as encode:
        for frame in frames:
            encode(frame)

    def decoded(path):
        with av.open(str(path)) as container:
            return [(float(f.time), f.to_ndarray(format="rgb24")) for f in container.decode(video=0)]

    old, new = decoded(tmp_path / "old.mp4"), decoded(tmp_path / "new.mp4")
    assert len(old) == len(new) == 4
    for (old_time, old_frame), (new_time, new_frame) in zip(old, new, strict=True):
        assert old_time == new_time
        np.testing.assert_array_equal(old_frame, new_frame)


@pytest.mark.parametrize("pool", [[], [0, 0], [-1], [True], ["0"], list(range(9))])
def test_runtime_rejects_invalid_pool(tmp_path, monkeypatch, pool):
    executable = tmp_path / "python"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    for relative in [
        "batch_utils/api.py",
        "rewarder/batch_utils/core.py",
        "tempseg_base/config.json",
        "captioner/config.json",
        "rewarder/config.json",
        "tempseg/adapter_model.safetensors",
    ]:
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    config = runtime(tmp_path).payload()
    config.update(caption_python=str(executable), reward_python=str(executable), gpus=pool)
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("DATA_PLATFORM_FLORA_RUNTIME", str(path))
    with pytest.raises(ValueError, match="gpus"):
        FloraRuntime.load()
    config.pop("gpus")
    path.write_text(json.dumps(config))
    assert FloraRuntime.load().gpus == ()


def test_parallel_caption_checks_share_one_cold_scan(source, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    original = snapshots._file_sha256
    count = 0
    lock = Lock()

    def counted(path):
        nonlocal count
        with lock:
            count += 1
        return original(path)

    monkeypatch.setattr(snapshots, "_file_sha256", counted)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: snapshots.caption_source_fingerprint(source, tmp_path / "cache"), range(4))
        )
    assert len(set(results)) == 1
    assert count == 2

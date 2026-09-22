#!/usr/bin/env python3
"""Benchmark developer-owned samples with isolated outputs and sanitized JSON measurements."""

from __future__ import annotations

import argparse
import json
import os
import resource
import threading
import time
from pathlib import Path


def cgroup_metrics():
    relative = next(
        (s[3:] for s in Path("/proc/self/cgroup").read_text().splitlines() if s.startswith("0::")), ""
    )
    root = Path("/sys/fs/cgroup") / relative.lstrip("/")
    values = {}
    for filename in ("cpu.stat", "io.stat", "memory.peak", "memory.current", "cpu.max", "memory.max"):
        path = root / filename
        if path.exists():
            values[filename] = path.read_text().strip()
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample", choices=("single", "v21", "v3"), required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument(
        "--operation", choices=("viewer", "smooth", "standardize", "merge", "convert"), default="viewer"
    )
    args = parser.parse_args()
    candidates = []
    for path in sorted(args.datasets.glob("*/meta/info.json")):
        info = json.loads(path.read_text())
        version, count = info.get("codebase_version"), info.get("total_episodes")
        cameras = sum(f.get("dtype") in ("image", "video") for f in info.get("features", {}).values())
        if cameras != 3:
            continue
        if (
            (args.sample == "single" and version == "v2.1" and count == 1)
            or (args.sample == "v21" and version == "v2.1" and count == 48)
            or (args.sample == "v3" and version == "v3.0" and count == 451)
        ):
            candidates.append(path.parent.parent)
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one {args.sample} benchmark fixture; found {len(candidates)}")
    root = candidates[0]
    inputs = [root]
    if args.operation == "merge":
        from lerobot.data_platform.precompute.preprocess.dataset_merge import validate_merge_sources

        for candidate in sorted(args.datasets.glob("*/meta/info.json")):
            other = candidate.parent.parent
            if other == root:
                continue
            other_info = json.loads(candidate.read_text())
            if other_info.get("codebase_version") != "v2.1":
                continue
            try:
                validate_merge_sources([root, other])
            except ValueError:
                continue
            inputs.append(other)
            break
        if len(inputs) != 2:
            raise RuntimeError("No second compatible development dataset for Merge")
    output = args.output.resolve()
    if any(output == source.resolve() or source.resolve() in output.parents for source in inputs):
        raise ValueError("Benchmark output must be outside every source dataset")
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_PLATFORM_EXECUTION_STOP_FILE"] = str(args.output / "stop.json")
    from lerobot.data_platform.execution import input_fingerprint

    try:
        from lerobot.data_platform.execution_resources import configure_native_threads

        configure_native_threads()
    except ImportError:
        pass
    before = input_fingerprint(inputs)
    from lerobot.data_platform.cli import load_platform_metadata, run_precompute

    meta = load_platform_metadata(root, "local/benchmark")
    episodes = sorted(meta.episodes)[:64] if args.sample == "v3" else sorted(meta.episodes)
    episode_count = (
        len(episodes)
        if args.operation == "viewer"
        else sum(len(load_platform_metadata(source, "local/benchmark").episodes) for source in inputs)
    )
    stages = {}

    def progress(payload):
        step = payload.get("step") or payload.get("phase") or "compute"
        stages.setdefault(step, time.monotonic() - started)

    metrics_before = cgroup_metrics()
    usage_before = [resource.getrusage(kind) for kind in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    peak_memory = [0]
    sampler_stop = threading.Event()

    def sample_memory():
        while not sampler_stop.wait(0.2):
            peak_memory[0] = max(peak_memory[0], int(cgroup_metrics().get("memory.current", 0)))

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()

    def cpu_ticks():
        return [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]

    ticks_before = cpu_ticks()
    started = time.monotonic()
    if args.operation == "viewer":
        run_precompute(
            root=root,
            repo_id="local/benchmark",
            output_dir=args.output / "cache",
            episodes=episodes,
            prepare_workers=args.workers,
            visualize_only=True,
            show_progress=False,
            progress_callback=progress,
        )
    elif args.operation == "smooth":
        from lerobot.data_platform.precompute.preprocess.smooth_action import run_smooth_action

        run_smooth_action(
            root, out_root=args.output / "dataset", workers=args.workers, progress_callback=progress
        )
    elif args.operation == "standardize":
        from lerobot.data_platform.precompute.preprocess.standardize import run_standardize_dataset

        run_standardize_dataset(
            root, out_root=args.output / "dataset", workers=args.workers, progress_callback=progress
        )
    elif args.operation == "convert":
        from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3

        run_convert_v3(
            root, out_root=args.output / "dataset", workers=args.workers, progress_callback=progress
        )
    else:
        from lerobot.data_platform.precompute.preprocess.dataset_merge import run_merge

        run_merge(inputs, out_root=args.output / "dataset", workers=args.workers, progress_callback=progress)
    elapsed = time.monotonic() - started
    sampler_stop.set()
    sampler.join()
    ticks_after = cpu_ticks()
    usage_after = [resource.getrusage(kind) for kind in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    if input_fingerprint(inputs) != before:
        raise RuntimeError("Benchmark changed its input")
    files = [p for p in args.output.rglob("*") if p.is_file()]
    report = {
        "sample": args.sample,
        "operation": args.operation,
        "workers": args.workers,
        "episodes": episode_count,
        "elapsed_seconds": elapsed,
        "episodes_per_second": episode_count / elapsed,
        "stages": stages,
        "video_threads": os.environ.get("DATA_PLATFORM_VIDEO_THREADS"),
        "cpu_seconds": sum(
            a.ru_utime + a.ru_stime - b.ru_utime - b.ru_stime
            for a, b in zip(usage_after, usage_before, strict=True)
        ),
        "peak_cgroup_memory_bytes": peak_memory[0],
        "host_iowait_fraction": (ticks_after[4] - ticks_before[4])
        / max(1, sum(ticks_after) - sum(ticks_before)),
        "max_rss_kib": max(u.ru_maxrss for u in usage_after),
        "output_files": len(files),
        "output_bytes": sum(p.stat().st_size for p in files),
        "source_unchanged": True,
        "metrics_before": metrics_before,
        "metrics_after": cgroup_metrics(),
    }
    (args.output / "measurement.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()

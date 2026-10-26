#!/usr/bin/env python3
"""Run a reproducible sequential H100 CPU matrix in isolated systemd services (root required)."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-python", type=Path, required=True)
    parser.add_argument("--new-python", type=Path, required=True)
    parser.add_argument("--new-source", type=Path)
    parser.add_argument("--datasets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--stage", choices=("baseline", "screen", "repeat"), required=True)
    parser.add_argument(
        "--operation", choices=("viewer", "smooth", "standardize", "merge", "convert"), default="viewer"
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("systemd benchmark orchestration must run as root")
    account = pwd.getpwnam(args.user)
    if not args.output.exists():
        args.output.mkdir(parents=True)
        os.chown(args.output, account.pw_uid, account.pw_gid)
    runner = args.output / "benchmark.py"
    if not runner.exists():
        shutil.copyfile(Path(__file__).with_name("benchmark_data_platform_compute.py"), runner)
        runner.chmod(0o644)
    samples = ("single", "v21", "v3") if args.operation == "viewer" else ("v21",)

    def run(version, cpu, sample, workers, threads, repeat=1):
        name = f"{version}-{cpu}-{sample}-w{workers}-t{threads}-{args.operation}-r{repeat}"
        output = args.output / name
        if (output / "measurement.json").exists():
            return
        if output.exists():
            raise RuntimeError(f"Incomplete run {name}; preserve it and use a new output root")
        environment = {
            "CUDA_VISIBLE_DEVICES": "",
            "HF_HOME": str(args.output / "hf"),
            "DATA_PLATFORM_EXECUTION_WORKERS": str(4 if version == "old" else workers),
            **{
                key: str(4 if version == "old" else 1)
                for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
            },
        }
        if version == "new":
            environment.update(
                DATA_PLATFORM_EXECUTION_NATIVE_THREADS="1", DATA_PLATFORM_VIDEO_THREADS=str(threads)
            )
            if args.new_source:
                environment["PYTHONPATH"] = str(args.new_source.resolve())
        command = [
            "systemd-run",
            "--wait",
            "--collect",
            "--pipe",
            "--uid=" + args.user,
            "-p",
            f"CPUQuota={cpu * 100}%",
            "-p",
            f"MemoryMax={4 if cpu == 2 else cpu * 2}G",
            "-p",
            "CPUWeight=10",
            "-p",
            "IOWeight=10",
            *["--setenv=" + key + "=" + value for key, value in environment.items()],
            str(args.old_python if version == "old" else args.new_python),
            str(runner),
            "--datasets",
            str(args.datasets),
            "--output",
            str(output),
            "--sample",
            sample,
            "--workers",
            str(workers),
            "--operation",
            args.operation,
        ]
        print("START", name, flush=True)
        with (args.output / (name + ".log")).open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        print("DONE", name, flush=True)

    if args.stage == "baseline":
        for cpu in (2, 32, 64):
            for sample in samples:
                run("old", cpu, sample, 4, 0)
    elif args.stage == "screen":
        for cpu in (32, 64):
            for workers in (4, 8, 16, 32, 64):
                if workers > cpu:
                    continue
                for threads in (1, 2) if args.operation in ("viewer", "convert") else (1,):
                    for sample in samples:
                        run("new", cpu, sample, workers, threads)
    else:
        for cpu in (32, 64):
            for sample in samples:
                candidates = []
                for report in args.output.glob(f"new-{cpu}-{sample}-*-{args.operation}-r1/measurement.json"):
                    record = json.loads(report.read_text())
                    candidates.append(
                        (record["elapsed_seconds"], record["workers"], int(record["video_threads"]))
                    )
                if len(candidates) < 2:
                    raise RuntimeError("Complete screening before repetition")
                for repeat in (2, 3):
                    run("old", cpu, sample, 4, 0, repeat)
                    for _, workers, threads in sorted(candidates)[:2]:
                        run("new", cpu, sample, workers, threads, repeat)


if __name__ == "__main__":
    main()

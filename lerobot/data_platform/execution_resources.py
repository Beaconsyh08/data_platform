"""Resolve job budgets against host capacity and inherited cgroup v2 limits."""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

_PREFIX = "DATA_PLATFORM_COMPUTE_"


def _positive(name: str, default: float, *, integer: bool = False) -> float:
    value = os.environ.get(name, str(default))
    try:
        parsed = float(value)
    except ValueError:
        raise ValueError(f"{name} must be positive") from None
    if not math.isfinite(parsed) or parsed <= 0 or (integer and not parsed.is_integer()):
        raise ValueError(f"{name} must be a positive {'integer' if integer else 'number'}")
    return parsed


def cgroup_directory() -> Path | None:
    try:
        relative = next(
            line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")
        )
    except (OSError, StopIteration):
        return None
    path = Path("/sys/fs/cgroup") / relative.lstrip("/")
    return path if path.is_dir() else None


def _cpuset_count(value: str) -> int:
    cpus = set()
    for part in value.strip().split(","):
        if not part:
            continue
        bounds = [int(item) for item in part.split("-")]
        cpus.update(range(bounds[0], bounds[-1] + 1))
    return len(cpus)


def inherited_limits() -> tuple[float, int]:
    cpus = float(os.cpu_count() or 1)
    if hasattr(os, "sched_getaffinity"):
        cpus = min(cpus, len(os.sched_getaffinity(0)))
    memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    current = cgroup_directory()
    while current is not None and current != Path("/sys/fs"):
        for name in ("cpu.max", "cpuset.cpus.effective", "memory.max"):
            path = current / name
            try:
                value = path.read_text().strip()
            except FileNotFoundError:
                continue
            if name == "cpu.max":
                quota, period = value.split()
                if quota != "max":
                    cpus = min(cpus, int(quota) / int(period))
            elif name == "memory.max" and value != "max":
                memory = min(memory, int(value))
            elif name == "cpuset.cpus.effective" and value:
                cpus = min(cpus, _cpuset_count(value))
        if current == Path("/sys/fs/cgroup") or current.parent == current:
            break
        current = current.parent
    return cpus, memory


@dataclass(frozen=True)
class ExecutionResources:
    cpu_limit: float
    memory_limit: int
    max_workers: int
    native_threads: int
    video_threads: int

    @classmethod
    def resolve(cls) -> ExecutionResources:
        available_cpu, available_memory = inherited_limits()
        host_memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        cpu = (
            _positive(_PREFIX + "CPU_LIMIT", 1)
            if _PREFIX + "CPU_LIMIT" in os.environ
            else max(0.1, (os.cpu_count() or 1) * _positive(_PREFIX + "CPU_FRACTION", 0.5))
        )
        memory = (
            _positive(_PREFIX + "MEMORY_LIMIT_GIB", 1) * 1024**3
            if _PREFIX + "MEMORY_LIMIT_GIB" in os.environ
            else host_memory * _positive(_PREFIX + "MEMORY_FRACTION", 0.6)
        )
        cpu = min(cpu, available_cpu)
        cpu_threads = max(1, math.floor(cpu))
        workers = min(int(_positive(_PREFIX + "MAX_WORKERS", 4, integer=True)), cpu_threads)
        native = min(int(_positive(_PREFIX + "NATIVE_THREADS", workers, integer=True)), cpu_threads)
        video = min(int(_positive("DATA_PLATFORM_VIDEO_THREADS", native, integer=True)), cpu_threads)
        return cls(cpu, max(1, int(min(memory, available_memory))), workers, native, video)

    def workers(self, requested: int | None) -> int:
        # Zero is the existing automatic-worker sentinel for selected transforms.
        return min(self.max_workers, max(1, int(requested))) if requested else self.max_workers

    def for_slots(self, slots: int) -> ExecutionResources:
        if slots <= 1:
            return self
        cpu = self.cpu_limit / slots
        threads = max(1, math.floor(cpu))
        reserve_bytes = min(4 * 1024**3, max(1024**3, self.memory_limit // 16))
        return replace(
            self,
            cpu_limit=cpu,
            memory_limit=max(1, (self.memory_limit - reserve_bytes) // slots),
            max_workers=min(self.max_workers, threads),
            native_threads=min(self.native_threads, threads),
            video_threads=min(self.video_threads, threads),
        )

    def environment(self) -> dict[str, str]:
        return {
            "DATA_PLATFORM_EXECUTION_WORKERS": str(self.max_workers),
            "DATA_PLATFORM_EXECUTION_NATIVE_THREADS": str(self.native_threads),
            "DATA_PLATFORM_VIDEO_THREADS": str(self.video_threads),
            **{
                key: str(self.native_threads)
                for key in (
                    "OMP_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS",
                )
            },
        }

    def record(self) -> dict:
        return asdict(self)


def configure_native_threads() -> None:
    value = os.environ.get("DATA_PLATFORM_EXECUTION_NATIVE_THREADS")
    if value is None:
        return
    import cv2
    import pyarrow as pa

    count = int(value)
    cv2.setNumThreads(count)
    pa.set_cpu_count(count)
    pa.set_io_thread_count(count)


def video_threads() -> int | None:
    if "DATA_PLATFORM_VIDEO_THREADS" not in os.environ:
        return None
    return int(_positive("DATA_PLATFORM_VIDEO_THREADS", 1, integer=True))

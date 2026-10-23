"""Shared admission rules for independent remote jobs on one Agent."""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lerobot.data_platform.operators.registry import list_operators, operation_ids

PARALLEL_OPERATIONS = frozenset(operation_ids(remote=True, parallel=True))
DATASET_OUTPUT_OPERATIONS = frozenset(
    spec.operator_id for spec in list_operators() if spec.remote and spec.output_type == "dataset"
)


def agent_slots() -> int:
    return min(8, max(1, int(os.environ.get("DATA_PLATFORM_AGENT_MAX_RUNNING_JOBS", "1"))))


def default_final_output(operation: str, source: str, job_id: str) -> str | None:
    if not (operation.startswith("preprocess.") or operation in DATASET_OUTPUT_OPERATIONS):
        return None
    root = PurePosixPath(source)
    return str(root.parent / f"{root.name}_{operation.split('.')[-1]}_{job_id}")


def _path(value: str, *, canonical: bool) -> str:
    raw = str(value or "")
    if not raw.startswith("/"):
        raise ValueError("Job resource paths must be absolute")
    return str(Path(raw).resolve()) if canonical else posixpath.normpath(raw)


@dataclass(frozen=True)
class JobFootprint:
    operation: str
    episode_index: int | None
    sources: tuple[str, ...]
    outputs: tuple[str, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return self.sources + self.outputs

    @property
    def parallel(self) -> bool:
        return self.operation in PARALLEL_OPERATIONS


def footprint(
    operation: str,
    source: str,
    output_dir: str,
    options: dict,
    job_id: str,
    *,
    final_output: str | None = None,
    canonical: bool = False,
) -> JobFootprint:
    source_locations = options.get("_source_locations") or options.get("source_locations") or []
    inputs = [source, *(item["root"] for item in source_locations)]
    # Raw MCAP locations have no Viewer output directory. Conversion reserves its final output below.
    no_cache_output = {"caption.annotate", "import.mcap.inspect", "import.mcap.convert"}
    outputs = [] if operation in no_cache_output else [output_dir]
    outputs.extend(item["output_dir"] for item in source_locations if item.get("output_dir"))
    final = final_output or options.get("out_root") or default_final_output(operation, source, job_id)
    if final:
        outputs.append(final)
    return JobFootprint(
        operation,
        options.get("episode_index") if operation == "caption.annotate" else None,
        tuple(sorted({_path(value, canonical=canonical) for value in inputs})),
        tuple(sorted({_path(value, canonical=canonical) for value in outputs})),
    )


def _overlap(left: str, right: str) -> bool:
    return (
        left == right or left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")
    )


def conflicts(left: JobFootprint, right: JobFootprint) -> bool:
    if not left.parallel or not right.parallel:
        return True
    if (
        left.operation == right.operation == "caption.annotate"
        and left.sources == right.sources
        and left.episode_index != right.episode_index
    ):
        return False
    return any(_overlap(a, b) for a in left.paths for b in right.paths)

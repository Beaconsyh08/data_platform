from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lerobot.data_platform.precompute.compare.builder import build_compare_cache


@dataclass
class CompareResult:
    out_dir: Path
    repo_id_a: str
    repo_id_b: str


def run_compare_build(
    *,
    root_a: Path,
    meta_a,
    static_a: Path,
    repo_id_a: str,
    root_b: Path,
    meta_b,
    static_b: Path,
    repo_id_b: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> CompareResult:
    out_dir = build_compare_cache(
        root_a=root_a,
        meta_a=meta_a,
        static_a=static_a,
        repo_id_a=repo_id_a,
        root_b=root_b,
        meta_b=meta_b,
        static_b=static_b,
        repo_id_b=repo_id_b,
        progress_callback=progress_callback,
    )
    return CompareResult(out_dir=out_dir, repo_id_a=repo_id_a, repo_id_b=repo_id_b)


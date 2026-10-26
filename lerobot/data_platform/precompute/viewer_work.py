"""Run independent Viewer artifacts within one shared concurrency budget."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lerobot.data_platform.execution_pools import (
    ThreadPoolExecutor,
    check_stop,
    effective_workers,
    map_completed,
)


@dataclass(frozen=True)
class ViewerWork:
    episode: int
    output: Path
    run: Callable[[], Any]


def _run(item: ViewerWork) -> tuple[ViewerWork, Any]:
    check_stop()
    result = item.run()
    check_stop()
    return item, result


def run_viewer_work(items: list[ViewerWork], workers: int, progress=None, consume=None) -> None:
    """Only count an episode after all its artifacts finish; aggregate on the caller thread."""
    outputs = [item.output for item in items]
    if len(set(outputs)) != len(outputs):
        raise ValueError("Duplicate Viewer output work item")
    remaining = Counter(item.episode for item in items)
    total = len(remaining)
    completed = 0
    files_completed = 0

    def report(episode=None):
        if progress:
            progress(
                {
                    "status": "running",
                    "step": "prepare",
                    "current": completed,
                    "total": total,
                    "files_current": files_completed,
                    "files_total": len(items),
                    "episode": episode,
                    "message": f"Prepared {completed}/{total} episodes; {files_completed}/{len(items)} video/CSV files",
                }
            )

    report()
    count = min(effective_workers(workers), max(1, len(items)))
    with ThreadPoolExecutor(max_workers=count) as executor:
        for item, result in map_completed(executor, _run, items):
            if consume:
                consume(item, result)
            remaining[item.episode] -= 1
            files_completed += 1
            if remaining[item.episode] == 0:
                completed += 1
            report(item.episode)
    check_stop()

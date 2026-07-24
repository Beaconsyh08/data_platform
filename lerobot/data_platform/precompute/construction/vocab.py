from __future__ import annotations

import json
from pathlib import Path

from lerobot.data_platform.precompute.labeling.task_parser import parse_task


def _iter_task_strings(meta_or_root) -> list[str]:
    root = Path(getattr(meta_or_root, "root", meta_or_root))
    tasks_path = root / "meta" / "tasks.jsonl"
    if tasks_path.is_file():
        tasks = []
        with tasks_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                task = row.get("task")
                if task:
                    tasks.append(str(task))
        return tasks

    tasks = getattr(meta_or_root, "tasks", {}) or {}
    return [str(task) for _, task in sorted(tasks.items(), key=lambda item: int(item[0]))]


def build_vocab(meta_or_root) -> set[str]:
    vocab: set[str] = set()
    for task in _iter_task_strings(meta_or_root):
        parsed = parse_task(task)
        if parsed is None:
            continue
        target = parsed.get("target")
        reference = parsed.get("reference")
        if target:
            vocab.add(str(target).strip().lower())
        if reference:
            vocab.add(str(reference).strip().lower())
    return vocab

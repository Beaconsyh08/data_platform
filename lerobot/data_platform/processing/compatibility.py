"""Small domain adapters for legacy multi-action operations."""

import json
from pathlib import Path

from lerobot.data_platform.execution import atomic_json
from lerobot.data_platform.precompute.preprocess.common import PreprocessResult, emit


def apply_prompt_assignments(root: Path, static_dir: Path, pending: dict, progress_callback=None):
    from lerobot.data_platform.precompute.preprocess.quality_flags import apply_task_assignment_choice

    path = Path(static_dir) / "prompt_assignments_pending.json"
    document = json.loads(path.read_text()) if path.is_file() else {"assignments": []}
    current = {int(row["episode_index"]): row for row in document.get("assignments", [])}
    selected = {int(key): value for key, value in pending.items()}
    if any(
        key not in current or current[key]["selected_task"] != value["selected_task"]
        for key, value in selected.items()
    ):
        raise ValueError("Pending prompt assignments changed while queued")
    failures, applied = [], []
    for index, (episode, value) in enumerate(sorted(selected.items()), 1):
        emit(progress_callback, current=index - 1, total=len(selected), message=f"Applying episode {episode}")
        try:
            apply_task_assignment_choice(Path(root), Path(static_dir), episode, value["selected_task"])
            applied.append(episode)
            current.pop(episode)
        except Exception as exc:
            failures.append({"episode_index": episode, "error": str(exc)})
    atomic_json(path, {"version": 1, "assignments": list(current.values())})
    return PreprocessResult(
        "apply_prompt_assignments",
        [Path(root)],
        Path(root),
        f"local/{Path(root).name}",
        summary={
            "applied_count": len(applied),
            "failed_count": len(failures),
            "remaining_pending_count": len(current),
            "failures": failures[:20],
        },
    )

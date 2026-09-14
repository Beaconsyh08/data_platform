"""Generate stage descriptions used by the local data platform."""

import json
from contextlib import suppress
from pathlib import Path


def cached_subtask_names(task: str, static_dir: Path, episode_id: int) -> tuple[int, dict[int, str]]:
    from lerobot.data_platform.precompute.analysis import find_cached_episode_csv
    from lerobot.data_platform.precompute.data_profile import STAGE_PROFILE_EQUAL_TIME
    from lerobot.data_platform.precompute.viewer_manifest import load_viewer_manifest
    from lerobot.data_platform.task_catalog import TaskConfigSnapshot

    manifest = load_viewer_manifest(Path(static_dir)) or {}
    config = manifest.get("task_config")
    count = TaskConfigSnapshot.from_dict(config).resolve(task).stage_count
    csv_path = find_cached_episode_csv(Path(static_dir), episode_id)
    if csv_path:
        with suppress(OSError, ValueError, TypeError, KeyError):
            count = int(json.loads(csv_path.with_suffix(".stages.json").read_text())["stage_count"])
    return count - 1, {
        stage: generate_subtask_text(
            task,
            stage,
            task_config=config,
            stage_count=count,
            force_equal_time=manifest.get("stage_profile") == STAGE_PROFILE_EQUAL_TIME,
        )
        for stage in range(-1, count)
    }


def _parse_task_object(task: str) -> tuple[str, str]:
    task_lower = task.lower().strip()
    prefixes = (
        "pick up the ",
        "pick up ",
        "pick the ",
        "pick ",
        "place the ",
        "place ",
        "put the ",
        "put ",
        "put down the ",
        "put down ",
        "grasp the ",
        "grasp ",
        "grab the ",
        "grab ",
        "give the ",
        "give ",
        "hand over the ",
        "hand over ",
        "hand the ",
        "hand ",
        "move the ",
        "move ",
        "lift the ",
        "lift ",
    )
    for prefix in prefixes:
        if task_lower.startswith(prefix):
            task_lower = task_lower[len(prefix) :]
            break

    target = ""
    for suffix in (" to the person", " to person", " to me", " to the table", " to the box"):
        if task_lower.endswith(suffix):
            target = suffix
            task_lower = task_lower[: -len(suffix)]
            break
    return task_lower, target


_PICK_TEMPLATES = [
    "prepare to grasp {object}",
    "reach for {object}",
    "grasp {object}",
    "lift {object}",
    "pick complete",
]

_PLACE_TEMPLATES = [
    "prepare to place object",
    "move object to target area",
    "place object",
    "retreat gripper",
    "place complete",
]

_GIVE_TEMPLATES = [
    "prepare to grasp {object}",
    "reach for {object}",
    "grasp {object}",
    "lift {object}{target}",
    "release {object} to hand",
    "give complete",
]

_DEFAULT_TEMPLATES = [
    "prepare for {task}",
    "approach target for {task}",
    "execute {task}",
    "retreat from {task}",
    "{task} complete",
]


def generate_subtask_text(
    task: str,
    stage: int,
    subtask_override: list[str] | None = None,
    *,
    task_config: dict | None = None,
    stage_count: int | None = None,
    force_equal_time: bool = False,
) -> str:
    """Return the stage description for a task."""
    from lerobot.data_platform.task_catalog import TaskConfigSnapshot

    resolved = TaskConfigSnapshot.from_dict(task_config).resolve(task)
    object_name, target = _parse_task_object(task)
    object_name = resolved.attributes.get("object", object_name)

    if stage == -1:
        if force_equal_time or resolved.stage_strategy == "equal_time":
            return "Unassigned stage"
        return f"{object_name} not found"

    if subtask_override is not None and 0 <= stage < len(subtask_override):
        return subtask_override[stage]

    if force_equal_time or resolved.stage_strategy == "equal_time":
        count = stage_count or resolved.stage_count
        return f"Stage {max(0, min(count - 1, stage)) + 1}/{count}"

    task_lower = task.lower()
    if resolved.stage_strategy == "legacy_give":
        stage = max(0, min(5, stage))
        return _GIVE_TEMPLATES[stage].format(object=object_name, target=target).strip()
    if resolved.stage_strategy == "legacy_pick":
        stage = max(0, min(4, stage))
        return _PICK_TEMPLATES[stage].format(object=object_name)
    if resolved.stage_strategy == "legacy_place":
        stage = max(0, min(4, stage))
        return _PLACE_TEMPLATES[stage].format(object=object_name)

    stage = max(0, min(4, stage))
    return _DEFAULT_TEMPLATES[stage].format(task=task_lower)

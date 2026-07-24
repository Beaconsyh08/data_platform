"""Generate stage descriptions used by the local data platform."""


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


def generate_subtask_text(task: str, stage: int, subtask_override: list[str] | None = None) -> str:
    """Return the stage description for a task."""
    object_name, target = _parse_task_object(task)

    if stage == -1:
        return f"{object_name} not found"

    if subtask_override is not None and 0 <= stage < len(subtask_override):
        return subtask_override[stage]

    task_lower = task.lower()
    if "give" in task_lower or "hand" in task_lower:
        stage = max(0, min(5, stage))
        return _GIVE_TEMPLATES[stage].format(object=object_name, target=target).strip()
    if "pick" in task_lower or "grasp" in task_lower or "grab" in task_lower or "lift" in task_lower:
        stage = max(0, min(4, stage))
        return _PICK_TEMPLATES[stage].format(object=object_name)
    if "place" in task_lower or "put" in task_lower:
        stage = max(0, min(4, stage))
        return _PLACE_TEMPLATES[stage].format(object=object_name)

    stage = max(0, min(4, stage))
    return _DEFAULT_TEMPLATES[stage].format(task=task_lower)

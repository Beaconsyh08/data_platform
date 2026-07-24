from __future__ import annotations

from lerobot.data_platform.precompute.construction.types import Scenario
from lerobot.data_platform.precompute.labeling.task_parser import parse_task


SINGLE_PICK = "single_pick"
DIRECTIONAL_PICK = "directional_pick"
RELATIVE_PICK = "relative_pick"
GIVE = "give"
UNKNOWN = "unknown"

SCENARIOS = [SINGLE_PICK, DIRECTIONAL_PICK, RELATIVE_PICK, GIVE]


def classify_task(task_str: str) -> Scenario:
    parsed = parse_task(task_str or "")
    if parsed is None:
        return Scenario(UNKNOWN, None)
    if parsed.get("action") == "give":
        return Scenario(GIVE, parsed)
    if parsed.get("reference"):
        return Scenario(RELATIVE_PICK, parsed)
    if parsed.get("direction"):
        return Scenario(DIRECTIONAL_PICK, parsed)
    if parsed.get("target"):
        return Scenario(SINGLE_PICK, parsed)
    return Scenario(UNKNOWN, parsed)

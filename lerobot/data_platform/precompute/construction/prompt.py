from __future__ import annotations

from lerobot.data_platform.precompute.construction.scenario import (
    DIRECTIONAL_PICK,
    GIVE,
    RELATIVE_PICK,
    SINGLE_PICK,
)


def make_prompt(
    scenario: str,
    missing_obj: str,
    original_parsed: dict | None,
    detected_existing: list[str] | None = None,
) -> str:
    parsed = original_parsed or {}
    missing_obj = str(missing_obj).strip().lower()
    detected_existing = detected_existing or []

    if scenario == SINGLE_PICK:
        return f"Pick up the {missing_obj}"
    if scenario == DIRECTIONAL_PICK:
        direction = parsed.get("direction") or "left"
        return f"Pick up the {missing_obj} on the {direction}"
    if scenario == RELATIVE_PICK:
        direction = parsed.get("direction") or "left"
        reference = parsed.get("reference")
        if reference not in detected_existing and detected_existing:
            reference = detected_existing[0]
        if not reference:
            reference = parsed.get("target") or "object"
        return f"Pick up the {missing_obj} to the {direction} of the {reference}"
    if scenario == GIVE:
        return f"Give me the {missing_obj}"
    raise ValueError(f"Unsupported construction scenario: {scenario}")

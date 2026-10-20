"""Deterministic repair of unambiguous prerequisites in assistant drafts only."""

import copy

from lerobot.data_platform.operators import get_operator


def bind_source_snapshot(definition, blocked):
    """Bind the original source once; preserve business steps, parameters and references."""
    if any(issue.get("code") == "invalid_input" for issue in blocked):
        result = copy.deepcopy(definition)
        snapshots, repairs = set(), []
        for step in result["steps"]:
            if step.get("input") in snapshots:
                step["input"] = "source"
                repairs.append(
                    {
                        "step_id": step["step_id"],
                        "reason": "Snapshot binds source identity; read the original source dataset",
                        "action": "reference_fixed",
                    }
                )
            if step["operator_id"] == "curation.snapshot" and step.get("input", "source") == "source":
                snapshots.add(step["step_id"])
        if repairs:
            return result, repairs
    if not any(issue.get("code") == "missing_snapshot" for issue in blocked):
        return definition, []
    result = copy.deepcopy(definition)
    steps = result["steps"]
    snapshots = [
        step
        for step in steps
        if step["operator_id"] == "curation.snapshot" and step.get("input", "source") == "source"
    ]
    reusable = next(
        (step for step in snapshots if not step.get("artifacts") and not step.get("review_before")), None
    )
    if reusable:
        snapshot = reusable
        steps.remove(snapshot)
    else:
        if len(steps) >= 12:
            return definition, []
        used = {step["step_id"] for step in steps}
        key = next(key for key in ["snapshot", *[f"snapshot_{i}" for i in range(13)]] if key not in used)
        snapshot = {
            "step_id": key,
            "operator_id": "curation.snapshot",
            "version": get_operator("curation.snapshot").version,
            "parameters": {},
            "input": "source",
            "artifacts": [],
        }
    steps.insert(0, snapshot)
    return result, [
        {
            "step_id": snapshot["step_id"],
            "reason": "Bind source version before dependent steps",
            "action": "moved" if reusable else "added",
        }
    ]

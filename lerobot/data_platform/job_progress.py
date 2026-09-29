"""Shared attempt-scoped progress and human-readable log policy.

Counters describe the current stage. Producers should supply explicit overall
``progress`` for multi-stage work; legacy counters use a monotonic high-water
mark. Only the job owner, after publication, may expose 100 percent.
"""

import math


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def progress_snapshot(previous: dict | None, payload: dict, *, message: str = "") -> dict:
    """Merge telemetry without letting text-only events erase known progress."""
    previous = previous or {}
    result = dict(previous)
    stage = payload.get("phase") or payload.get("step")
    previous_stage = previous.get("phase") or previous.get("step")
    if stage and stage != previous_stage:
        for key in ("current", "total", "step", "phase"):
            result.pop(key, None)
    for key in ("current", "total", "phase", "step", "status"):
        if key in payload:
            result[key] = payload[key]
    candidate = _number(payload.get("progress"))
    if candidate is None and ("current" in payload or "total" in payload):
        current, total = _number(result.get("current")), _number(result.get("total"))
        if current is not None and total is not None and total > 0:
            candidate = 100 * current / total
    known = _number(previous.get("progress"))
    if candidate is not None or known is not None:
        result["progress"] = min(99, max(0, known or 0, candidate or 0))
    if message or payload.get("message"):
        result["message"] = message or payload["message"]
    return result


def visible_progress_log(previous: dict | None, payload: dict, message: str, level: str = "info") -> bool:
    """Keep stage changes, ten-percent milestones and diagnostics in the summary.

    Detailed events remain in the existing event/audit stream. Warnings and
    errors are never deduplicated or sampled.
    """
    previous = previous or {}
    if level.lower() in {"warning", "warn", "error", "critical"} or payload.get("status") == "error":
        return True
    if not message:
        return False
    stage = payload.get("phase") or payload.get("step")
    old_stage = previous.get("phase") or previous.get("step")
    if stage and stage != old_stage:
        return True
    if message == previous.get("message"):
        return False
    current, total = _number(payload.get("current")), _number(payload.get("total"))
    old_current, old_total = _number(previous.get("current")), _number(previous.get("total"))
    if current is not None and total is not None and total > 0:
        if old_current is None or old_total is None or old_total <= 0:
            return True
        return int(10 * max(0, current) / total) > int(10 * max(0, old_current) / old_total)
    return True

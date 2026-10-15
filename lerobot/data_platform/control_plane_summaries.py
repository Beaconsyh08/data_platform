"""Small console responses that leave persisted execution inputs and reports intact."""

from __future__ import annotations


def summarize_location(location: dict) -> dict:
    """Keep dataset selection and operation metadata without per-episode evidence."""
    return {
        **location,
        "metadata": {
            key: value
            for key, value in (location.get("metadata") or {}).items()
            if key not in {"episodes", "stats", "tasks"}
        },
    }


def summarize_job(job: dict) -> dict:
    """Keep controls, progress and output links without full execution artifacts."""
    result = {key: value for key, value in (job.get("result") or {}).items() if key != "precompute"}
    for field in ("dataset_location", "synced_location"):
        if isinstance(result.get(field), dict):
            result[field] = summarize_location(result[field])
    for field, omitted in (
        ("mcap_import", {"files", "rejected"}),
        ("preprocess", {"episode_lineage"}),
    ):
        if isinstance(result.get(field), dict):
            result[field] = {key: value for key, value in result[field].items() if key not in omitted}
    return {
        **job,
        "options": {
            key: value
            for key, value in (job.get("options") or {}).items()
            if key not in {"snapshot", "task_config", "_source_locations"}
        },
        "result": result,
    }

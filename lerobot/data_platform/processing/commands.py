"""Serialize already validated domain calls without capturing functions or HTTP requests."""

from __future__ import annotations

import inspect
from dataclasses import asdict, is_dataclass
from pathlib import Path

from lerobot.data_platform.operators import get_operator


def _json(value):
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _json(asdict(value))
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def build_command(operator_id, function, *args, **kwargs) -> dict:
    """Called at the validated route boundary; only allowlisted implementations execute."""
    get_operator(operator_id)
    bound = inspect.signature(function).bind_partial(*args, **kwargs).arguments
    # Worker metadata and callbacks are supplied by its execution context.
    parameters = {
        key: value
        for key, value in bound.items()
        if key not in {"meta", "dataset", "progress_callback", "log"}
    }
    for key in ("kwargs", "options"):
        if key in parameters and isinstance(parameters[key], dict):
            parameters.update(parameters.pop(key))
    if parameters.get("in_place") or get_operator(operator_id).effect == "source":
        operator_id = "mutation." + operator_id.split(".")[-1]
        get_operator(operator_id)
    return {
        "operation": operator_id,
        "options": {
            "local_compute": {
                "operator_id": operator_id,
                "parameters": _json(parameters),
            },
            "operator_snapshot": {"operator_id": operator_id, "version": get_operator(operator_id).version},
        },
    }


def standardize_command(root, out_root, data_version, workers, overwrite, delete_episodes, dry_run):
    return {
        "operation": "preprocess.standardize",
        "options": {
            "local_compute": {
                "operator_id": "preprocess.standardize",
                "composite": True,
                "parameters": _json(
                    {
                        "out_root": out_root,
                        "data_version": data_version,
                        "workers": workers,
                        "overwrite_output": overwrite,
                        "delete_episodes": delete_episodes,
                        "dry_run": dry_run,
                    }
                ),
            },
            "operator_snapshot": {"operator_id": "preprocess.standardize", "version": "1.0.0"},
        },
    }

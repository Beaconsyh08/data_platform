"""Bounded execution evidence and comparable historical recipes, without log or artifact reads."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from sqlalchemy import func, select

from lerobot.data_platform.ai_tools.security import DisclosureGuard
from lerobot.data_platform.control_plane import RemoteJob
from lerobot.data_platform.management_storage import JobAttempt, JobControl
from lerobot.data_platform.processing.models import PipelineVersion
from lerobot.data_platform.task_catalog import content_digest

METRICS = {
    "total_frames",
    "total_episodes",
    "selected_episodes",
    "episodes_scanned",
    "quality_issue_count",
    "flagged_episode_count",
    "video_error_streams",
    "uploaded_files",
}


@dataclass(frozen=True)
class ExecutionEvidence:
    conditions: dict
    steps: list[dict]


def failure_code(error):
    """Classify only explicit diagnostics, never infer OOM from an unexplained process loss."""
    text = str(error or "").lower()
    for code, phrases in (
        ("empty_selection", ("no episodes match split filters", "no episodes selected")),
        ("memory_exhausted", ("out of memory", "memoryerror")),
        ("permission_denied", ("permission denied",)),
        ("output_exists", ("output already exists", "output directory already exists")),
        ("provider_timeout", ("qwen api connection failed or timed out",)),
        ("process_lost", ("compute process exited without a result",)),
    ):
        if any(text.startswith(phrase) for phrase in phrases):
            return code
    return "unclassified"


def metric_columns(column):
    return [
        value.label(f"metric_{index}_{position}")
        for index, key in enumerate(sorted(METRICS))
        for position, value in enumerate(
            (
                column[key],
                column["summary"][key],
                column["preprocess"][key],
                column["preprocess"]["summary"][key],
            )
        )
    ]


def collect(session, run, steps):
    try:
        return _collect(session, run, steps)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _collect(session, run, steps):
    """Require the immutable plan, actual command and final attempt to agree.

    Missing evidence removes the richer projection; terminal followup support remains unchanged.
    Do not load archived reports on the foreground path or expose arbitrary result/error strings.
    """
    version = session.get(PipelineVersion, run.version_id)
    if not version or content_digest(version.spec) != version.digest:
        return None
    planned = {s["step_id"]: s for s in version.spec["steps"]}
    positions = {s["step_id"]: index for index, s in enumerate(version.spec["steps"])}
    evidence, formats, releases = [], set(), set()
    guard = DisclosureGuard.from_environment()
    for step in steps:
        plan = planned.get(step.step_id)
        if plan is None:
            return None
        recipe = {
            "operator": plan["operator_id"],
            "version": plan["version"],
            "input": positions.get(plan.get("input"), "source"),
            "parameters": plan["parameters"],
            "artifacts": [positions[name] for name in plan.get("artifacts", [])],
            "options": plan.get("options", {}),
        }
        # Refuse rather than truncate parameters: an omitted value may explain success or failure.
        if not guard.safe_draft(recipe) or len(json.dumps(recipe).encode()) > 2000:
            return None
        row = {"recipe": recipe, "status": step.status}
        if step.job_id:
            names = list(plan["parameters"])
            # Project bounded fields in SQL: no full reports, artifact network reads or task inventories.
            job = session.execute(
                select(
                    RemoteJob.job_id,
                    RemoteJob.requested_by,
                    RemoteJob.operation,
                    func.substr(RemoteJob.error, 1, 1024).label("error"),
                    RemoteJob.options["operator_snapshot"].label("snapshot"),
                    RemoteJob.options["parameters"].label("parameters"),
                    *[RemoteJob.options[key].label(f"parameter_{i}") for i, key in enumerate(names)],
                    *metric_columns(RemoteJob.result),
                ).where(RemoteJob.job_id == step.job_id)
            ).one_or_none()
            control = session.get(JobControl, step.job_id)
            if not job or not control or not control.attempt_id:
                return None
            attempt = session.execute(
                select(
                    JobAttempt.attempt_id,
                    JobAttempt.job_id,
                    JobAttempt.status,
                    func.substr(JobAttempt.error, 1, 1024).label("error"),
                    *metric_columns(JobAttempt.result),
                ).where(JobAttempt.attempt_id == control.attempt_id)
            ).one_or_none()
            if not attempt or attempt.job_id != job.job_id or attempt.status != step.status:
                return None
            snapshot = job.snapshot or {}
            actual = (
                job.parameters
                if isinstance(job.parameters, dict)
                else {key: job._mapping[f"parameter_{i}"] for i, key in enumerate(names)}
            )
            if (
                job.requested_by != run.owner
                or job.operation != plan["operator_id"]
                or snapshot.get("pipeline_run") != run.run_id
                or snapshot.get("step_id") != step.step_id
                or snapshot.get("operator_id") != plan["operator_id"]
                or snapshot.get("version") != plan["version"]
                or snapshot.get("input_identity") != step.input_identity
                or any(key not in actual or actual[key] != value for key, value in plan["parameters"].items())
            ):
                return None
            formats.add(snapshot.get("data_format"))
            releases.add(snapshot.get("release"))
            row.update(job_id=job.job_id, attempt_id=attempt.attempt_id)
            metrics = {}
            for index, key in enumerate(sorted(METRICS)):
                for position in range(4):
                    label = f"metric_{index}_{position}"
                    value, expected = job._mapping[label], attempt._mapping[label]
                    if (
                        type(value) in (int, float)
                        and math.isfinite(value)
                        and value >= 0
                        and value == expected
                    ):
                        metrics[key] = value
            row["metrics"] = metrics if step.status == "done" else {}
            if step.status == "error":
                row["failure"] = failure_code(attempt.error) if attempt.error == job.error else "unclassified"
        evidence.append(row)
    if not formats or None in formats or None in releases or not run.input_identity.get("digest"):
        return None
    return ExecutionEvidence(
        {
            "input_digest": content_digest(run.input_identity),
            "formats": sorted(formats),
            "releases": sorted(releases),
        },
        evidence,
    )


def comparison(sources):
    """Determine admissible claims from comparable Runs; the model cannot invent repair links."""
    records = [json.loads(source.text) for source in sources]
    if not records or any(not r.get("execution") for r in records):
        return None
    conditions = records[0]["execution"]["conditions"]
    if any(r["execution"]["conditions"] != conditions for r in records):
        return None
    recipes = [[s["recipe"] for s in r["execution"]["steps"]] for r in records]
    if not recipes[0] or any(
        [(s["operator"], s["version"]) for s in recipe] != [(s["operator"], s["version"]) for s in recipes[0]]
        for recipe in recipes
    ):
        return None
    success = [i for i, r in enumerate(records) if r["outcome"]["run_status"] == "done"]
    failed = [i for i, r in enumerate(records) if r["outcome"]["run_status"] == "error"]
    counts = {
        "successful_runs": len(success),
        "failed_runs": len(failed),
        "cancelled_runs": sum(r["outcome"]["run_status"] == "cancelled" for r in records),
        "total_runs": len(records),
    }
    if len(success) >= 2 and all(recipe == recipes[0] for recipe in recipes):
        return {"kind": "observed_recipe", "conditions": conditions, "counts": counts}
    repairs = []
    for before in failed:
        for after in success:
            errors = [
                s.get("failure") for s in records[before]["execution"]["steps"] if s["status"] == "error"
            ]
            # Exact original action time establishes sequence, not rounded legacy Run timestamps.
            if (
                not errors
                or "unclassified" in errors
                or records[after]["origin_created_at"] <= records[before]["completed_at"]
                or recipes[before] == recipes[after]
            ):
                continue
            repairs.append(
                {
                    "failed_run": sources[before].run_id,
                    "successful_run": sources[after].run_id,
                    "failures": errors,
                    "before": recipes[before],
                    "after": recipes[after],
                }
            )
    if repairs:
        return {
            "kind": "observed_recovery",
            "conditions": conditions,
            "counts": counts,
            "changes": repairs[:4],
        }
    return None


def cohort(snapshot, source_ids):
    """Include counterexamples in the same input/runtime cohort, not just favorable model citations."""
    selected = [source for source in snapshot.sources if source.source_id in source_ids]
    records = [json.loads(source.text) for source in selected]
    if not records or not any(r.get("execution") for r in records):
        return selected, None
    if not all(r.get("execution") for r in records):
        return [], None
    condition = records[0]["execution"]["conditions"]
    if any(r["execution"]["conditions"] != condition for r in records):
        return [], None

    def shape(record):
        return [
            (s["recipe"]["operator"], s["recipe"]["version"])
            for s in record.get("execution", {}).get("steps", [])
        ]

    def recipe(record):
        return [s["recipe"] for s in record["execution"]["steps"]]

    if any(shape(r) != shape(records[0]) for r in records):
        return [], None
    same_recipe = all(recipe(r) == recipe(records[0]) for r in records)
    selected = [
        source
        for source in snapshot.sources
        if (record := json.loads(source.text)).get("execution", {}).get("conditions") == condition
        and shape(record) == shape(records[0])
        and (not same_recipe or recipe(record) == recipe(records[0]))
    ]
    return selected, comparison(selected)

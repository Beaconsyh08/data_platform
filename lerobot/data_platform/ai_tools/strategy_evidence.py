"""Cross-account whitelist projections of authorized, acknowledged pipeline results."""

import time

from sqlalchemy import func, select

from lerobot.data_platform.ai_tools.experience_evidence import failure_code
from lerobot.data_platform.ai_tools.followups import _acknowledged
from lerobot.data_platform.ai_tools.strategy_contracts import OPERATORS
from lerobot.data_platform.ai_tools.strategy_models import StrategyEvidence
from lerobot.data_platform.control_plane import RemoteJob
from lerobot.data_platform.management_storage import JobAttempt, JobControl
from lerobot.data_platform.processing.models import PipelineRun, PipelineStepRun, PipelineVersion
from lerobot.data_platform.processing.service import location_identity
from lerobot.data_platform.task_catalog import content_digest


def projection(manager, session, administrator, run):
    if manager._actor(session, administrator, write=False).role != "admin":
        raise PermissionError("An active administrator is required")
    location, _ = manager._location(session, run.location_id, administrator, write=False)
    manager._location(session, run.location_id, run.owner, write=False)
    if location_identity(location) != run.input_identity:
        raise ValueError("Source identity changed")
    plan = session.get(PipelineVersion, run.version_id)
    if plan is None or content_digest(plan.spec) != plan.digest:
        raise ValueError("Immutable plan changed")
    steps = session.scalars(
        select(PipelineStepRun)
        .where(PipelineStepRun.run_id == run.run_id)
        .order_by(PipelineStepRun.position)
        .limit(13)
    ).all()
    if not 1 <= len(steps) <= 12 or not _acknowledged(session, run, steps):
        raise ValueError("Terminal acknowledgement is missing")
    planned = plan.spec["steps"]
    if len(planned) != len(steps) or any(s["operator_id"] not in OPERATORS for s in planned):
        raise ValueError("Plan is outside the learning allowlist")
    positions = {s["step_id"]: i for i, s in enumerate(planned)}
    rows, identities = [], []
    for step, definition in zip(steps, planned, strict=True):
        if step.step_id != definition["step_id"]:
            raise ValueError("Step identity changed")
        for key in (step.input_location_id, step.output_location_id):
            if key:
                manager._location(session, key, administrator, write=False)
                manager._location(session, key, run.owner, write=False)
        outcome, duration = "cancelled" if run.status == "cancelled" else "none", None
        if step.job_id:
            job = session.execute(
                select(RemoteJob.status, func.substr(RemoteJob.error, 1, 1024)).where(
                    RemoteJob.job_id == step.job_id
                )
            ).one()
            control = session.get(JobControl, step.job_id)
            attempt = session.get(JobAttempt, control.attempt_id) if control.attempt_id else None
            outcome = failure_code(job[1]) if job[0] == "error" else outcome
            if job[0] == "error" and str(job[1] or "").lower().startswith(
                ("oss upload failed", "result upload failed", "result publication failed")
            ):
                outcome = "publication_failed"
            if attempt and attempt.finished_at and attempt.started_at:
                duration = max(0, int(attempt.finished_at - attempt.started_at))
            identities.append([step.job_id, control.attempt_id, job[0]])
        rows.append(
            {
                "operator_id": definition["operator_id"],
                "version": definition["version"],
                "input": positions.get(definition.get("input"), "source"),
                "artifacts": [positions[k] for k in definition.get("artifacts", [])],
                "status": step.status,
                "failure": outcome,
                "planning_failure": outcome == "empty_selection",
                "seconds": duration,
                "output_registered": bool(step.output_location_id),
                "acknowledged": True,
            }
        )
    fmt = location["metadata"].get("codebase_version")
    if fmt not in {"v2.1", "v3.0"}:
        raise ValueError("Unknown format")
    attribution = plan.attribution or {}
    value = {
        "format": fmt,
        "status": run.status,
        "steps": rows,
        "strategy_version_id": attribution.get("version_id"),
        "user_modified": bool(attribution.get("user_modified", False)),
    }
    digest = content_digest([value, run.revision, run.input_identity, plan.digest, identities])
    return value, digest


def collect(manager, administrator, *, excluded_owner=None):
    """Bounded scan, upsert changed evidence; never load conversations or reports."""
    with manager.store.sessions.begin() as session:
        rows = session.scalars(
            select(PipelineRun)
            .where(PipelineRun.status.in_(["done", "error", "cancelled"]))
            .order_by(PipelineRun.updated_at.desc())
            .limit(200)
        ).all()
        count = 0
        for run in rows:
            if run.owner == excluded_owner:
                continue
            try:
                value, digest = projection(manager, session, administrator, run)
            except (PermissionError, KeyError, ValueError):
                previous = session.get(StrategyEvidence, run.run_id)
                if previous:
                    previous.valid = False
                continue
            previous = session.get(StrategyEvidence, run.run_id)
            if previous and previous.source_digest == digest:
                continue
            if previous:
                # Changing evidence cannot silently change the meaning of an old strategy reference.
                previous.valid = False
                continue
            session.add(
                StrategyEvidence(
                    run_id=run.run_id,
                    administrator=administrator,
                    source_digest=digest,
                    projection=value,
                    valid=True,
                    created_at=int(time.time()),
                )
            )
            count += 1
    return count


def validate_sources(manager, session, references):
    for ref in references:
        record = session.get(StrategyEvidence, ref["run_id"])
        run = session.get(PipelineRun, ref["run_id"])
        if not record or not record.valid or not run or record.source_digest != ref["digest"]:
            raise PermissionError("Strategy evidence is unavailable")
        try:
            _, digest = projection(manager, session, record.administrator, run)
        except (KeyError, ValueError) as exc:
            raise PermissionError("Strategy evidence changed") from exc
        if digest != ref["digest"]:
            raise PermissionError("Strategy evidence changed")

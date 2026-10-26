"""Durable, authorized completion observations; no provider calls or operation submission."""

from __future__ import annotations

import time
from urllib.parse import urlencode

from sqlalchemy import delete, select
from sqlalchemy.orm import load_only

from lerobot.data_platform.ai_tools.memory import settings
from lerobot.data_platform.ai_tools.models import (
    AssistantConversation,
    AssistantFollowup,
    AssistantPreferences,
)
from lerobot.data_platform.ai_tools.security import DisclosureGuard
from lerobot.data_platform.control_plane import RemoteJob
from lerobot.data_platform.job_management import JobConflictError, scheduler_lock
from lerobot.data_platform.management_storage import JobAttempt, JobControl
from lerobot.data_platform.operators import get_operator
from lerobot.data_platform.processing.models import PipelineRun, PipelineStepRun, PipelineVersion

TERMINAL = {"done", "error", "cancelled"}


def register(session, turn, run_id):
    """Called in the confirmation receipt transaction, so retries cannot duplicate the observation."""
    if not settings(session, turn.owner)["followups_enabled"] or session.get(
        AssistantFollowup, turn.request_id
    ):
        return
    conversation = session.get(AssistantConversation, turn.conversation_id)
    now = int(time.time())
    session.add(
        AssistantFollowup(
            action_id=turn.request_id,
            owner=turn.owner,
            conversation_id=turn.conversation_id,
            location_id=conversation.location_id,
            input_identity=conversation.input_identity,
            run_id=run_id,
            status="pending",
            summary={},
            next_check_at=now,
            updated_at=now,
        )
    )


def _authorized(service, session, row):
    service._target(session, row.owner, row.location_id, row.input_identity)
    run = session.get(PipelineRun, row.run_id)
    if run is None or run.owner != row.owner or run.location_id != row.location_id:
        raise KeyError("Run not found")
    steps = session.scalars(
        select(PipelineStepRun)
        .where(PipelineStepRun.run_id == row.run_id)
        .order_by(PipelineStepRun.position)
        .limit(13)
    ).all()
    if len(steps) > 12:
        raise ValueError("Run exceeds observation budget")
    for step in steps:
        for location in (step.input_location_id, step.output_location_id):
            if location:
                service.manager._location(session, location, row.owner, write=False)
    return run, steps


def _acknowledged(session, run, steps):
    if not steps:
        return False
    for step in steps:
        if run.status == "done" and step.status not in {"done", "skipped"}:
            return False
        if not step.job_id:
            if step.status == "done":
                return False
            continue
        job_status = session.scalar(select(RemoteJob.status).where(RemoteJob.job_id == step.job_id))
        control = session.get(JobControl, step.job_id)
        if (
            job_status not in TERMINAL
            or step.status != job_status
            or not control
            or not control.stop_confirmed
        ):
            return False
        if control.attempt_id:
            attempt = session.scalar(
                select(JobAttempt)
                .options(
                    load_only(
                        JobAttempt.attempt_id, JobAttempt.job_id, JobAttempt.status, JobAttempt.finished_at
                    )
                )
                .where(JobAttempt.attempt_id == control.attempt_id)
            )
            if (
                not attempt
                or attempt.job_id != step.job_id
                or attempt.status != job_status
                or not attempt.finished_at
            ):
                return False
        elif job_status != "cancelled":
            return False
    return True


def _summary(session, row, run, steps):
    version = session.get(PipelineVersion, run.version_id)
    operators = [s["operator_id"] for s in version.spec["steps"]]
    personal = session.get(AssistantPreferences, row.owner)
    language = (
        personal.settings.get("language", "auto") if personal and personal.settings.get("enabled") else "auto"
    )
    if language == "auto":
        conversation = session.get(AssistantConversation, row.conversation_id)
        text = " ".join(
            item["content"]
            for item in (conversation.history if conversation else [])
            if item["role"] == "user"
        )
        language = "zh" if any("\u4e00" <= c <= "\u9fff" for c in text) else "en"
    completed = sum(step.status == "done" for step in steps)
    review = any(get_operator(op).review_required for op in operators)
    if language == "zh":
        title = {"done": "任务已完成", "error": "任务失败", "cancelled": "任务已取消"}[run.status]
        message = f"{title}。已完成 {completed}/{len(steps)} 个步骤。"
        message += (
            "结果及覆盖范围请查看运行记录；检测候选仍需人工复核。" if review else "详情和产物请查看运行记录。"
        )
        if run.status != "done":
            message += "请先在 Jobs 检查失败、停止或恢复状态。"
        message += "后续任务仍须明确确认。"
    else:
        title = {"done": "Task completed", "error": "Task failed", "cancelled": "Task cancelled"}[run.status]
        message = f"{title}. {completed}/{len(steps)} steps completed. "
        message += (
            "Check results and coverage in the run; candidates still need human review. "
            if review
            else "Open the run for details and outputs. "
        )
        if run.status != "done":
            message += "Inspect failure, stop or recovery status in Jobs first. "
        message += "Further tasks require explicit confirmation."
    return {
        "message": message,
        "run_status": run.status,
        "run_revision": run.revision,
        "completed_steps": completed,
        "total_steps": len(steps),
        "operators": operators,
        "review_required": review,
    }


def tick(service, *, now=None):
    """Bounded SQL-only publication, serialized with existing control transactions across runners."""
    if not service.config.enabled:
        return 0
    now = int(time.time()) if now is None else now
    query = (
        select(AssistantFollowup.action_id)
        .where(AssistantFollowup.status == "pending", AssistantFollowup.next_check_at <= now)
        .order_by(AssistantFollowup.next_check_at, AssistantFollowup.action_id)
        .limit(20)
    )
    with service.store.sessions() as session:
        candidates = list(session.scalars(query))
    for identity in candidates:
        with service.store.sessions.begin() as session:
            scheduler_lock(session)
            row = session.get(AssistantFollowup, identity)
            if row is None or row.status != "pending" or row.next_check_at > now:
                continue
            row.next_check_at = now + 30
            if not settings(session, row.owner)["followups_enabled"]:
                row.status, row.updated_at = "muted", now
                continue
            try:
                run, steps = _authorized(service, session, row)
            except (PermissionError, KeyError, JobConflictError, ValueError):
                row.status, row.updated_at = "unavailable", now
                continue
            if run.status not in TERMINAL or not _acknowledged(session, run, steps):
                continue
            row.summary = _summary(session, row, run, steps)
            row.status, row.updated_at = "ready", now
            from lerobot.data_platform.ai_tools.experience import enqueue

            enqueue(session, row.owner, row.location_id)
            service.manager._audit(
                session,
                row.owner,
                "assistant.followup.ready",
                request_id=row.action_id,
                run_id=row.run_id,
                result_status=run.status,
            )
    # No scheduler write when idle. Retention is independent of conversation cleanup.
    with service.store.sessions() as session:
        expired = list(
            session.scalars(
                select(AssistantFollowup.action_id)
                .where(AssistantFollowup.status != "pending", AssistantFollowup.updated_at < now - 30 * 86400)
                .limit(50)
            )
        )
    if expired:
        with service.store.sessions.begin() as session:
            scheduler_lock(session)
            from lerobot.data_platform.ai_tools.experience import enqueue

            for owner, location in session.execute(
                select(AssistantFollowup.owner, AssistantFollowup.location_id)
                .where(
                    AssistantFollowup.action_id.in_(expired),
                    AssistantFollowup.status != "pending",
                    AssistantFollowup.updated_at < now - 30 * 86400,
                )
                .distinct()
            ):
                enqueue(session, owner, location)
            session.execute(
                delete(AssistantFollowup).where(
                    AssistantFollowup.action_id.in_(expired),
                    AssistantFollowup.status != "pending",
                    AssistantFollowup.updated_at < now - 30 * 86400,
                )
            )
    return len(candidates)


def read(service, owner, *, conversation_id=None):
    from lerobot.data_platform.ai_tools.operation_state import _artifact_links

    with service.store.sessions() as session:
        service.manager._actor(session, owner, write=False)
        if not settings(session, owner)["followups_enabled"]:
            return {"updates": [], "pending": False}
        query = select(AssistantFollowup).where(
            AssistantFollowup.owner == owner, AssistantFollowup.status.in_(["pending", "ready"])
        )
        if conversation_id:
            service._conversation(session, owner, conversation_id)
            query = query.where(AssistantFollowup.conversation_id == conversation_id)
        rows = session.scalars(
            query.order_by(AssistantFollowup.updated_at.desc(), AssistantFollowup.action_id).limit(50)
        ).all()
        updates, pending = [], False
        for row in rows:
            try:
                run, steps = _authorized(service, session, row)
            except (PermissionError, KeyError, JobConflictError, ValueError):
                continue
            if row.status == "pending":
                pending = True
                continue
            # A manual retry or changed final acknowledgement cannot display stale success as current.
            if (
                run.status != row.summary["run_status"]
                or run.revision != row.summary["run_revision"]
                or not _acknowledged(session, run, steps)
            ):
                continue
            links = [
                {
                    "title": "Open run",
                    "url": "/pipelines?" + urlencode({"location_id": row.location_id, "run_id": row.run_id}),
                }
            ]
            version = session.get(PipelineVersion, run.version_id)
            operators = {s["step_id"]: s["operator_id"] for s in version.spec["steps"]}
            for step in steps:
                if step.status == "done" and step.job_id and step.input_location_id:
                    location, _ = service.manager._location(
                        session, step.input_location_id, owner, write=False
                    )
                    links.extend(
                        _artifact_links(
                            service.manager,
                            session,
                            owner,
                            location,
                            operators[step.step_id],
                            step.job_id,
                            step.output_location_id,
                        )
                    )
            updates.append(
                {
                    "update_id": row.action_id,
                    "conversation_id": row.conversation_id,
                    "run_id": row.run_id,
                    "completed_at": row.updated_at,
                    **row.summary,
                    "links": links,
                }
            )
        return DisclosureGuard.from_environment().tree({"updates": updates[:20], "pending": pending})

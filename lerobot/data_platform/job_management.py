"""Transactional scheduling and explicit controls for persistent jobs."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import PurePosixPath

from sqlalchemy import func, select, update

from lerobot.data_platform.curation import OPERATIONS as CURATION_OPERATIONS
from lerobot.data_platform.management_storage import (
    JobAttempt,
    JobCommand,
    JobControl,
    SchedulerLock,
    enqueue_event,
)
from lerobot.data_platform.operation_log import build_operation_event, sanitize_for_log

JOB_PROTOCOL = 2
ACTIVE_STATES = {"running", "cancel_requested", "interrupted"}
SAFE_OPERATIONS = {
    *CURATION_OPERATIONS,
    "caption.annotate",
    "viewer.prepare",
    "preprocess.convert_action",
    "preprocess.convert_v3",
    "preprocess.drop_field",
    "preprocess.smooth_action",
    "preprocess.split",
    "preprocess.merge",
    "preprocess.standardize",
    "preprocess.value_edit",
}


class JobConflictError(ValueError):
    pass


def scheduler_lock(session):
    # An actual UPDATE acquires a write lock on SQLite as well as an InnoDB row lock.
    session.execute(
        update(SchedulerLock).where(SchedulerLock.lock_id == 1).values(revision=SchedulerLock.revision + 1)
    )


class JobManager:
    def __init__(self, store):
        self.store = store
        self.user_limit = max(1, int(os.environ.get("DATA_PLATFORM_USER_RUNNING_LIMIT", "1")))
        self.node_limit = max(1, int(os.environ.get("DATA_PLATFORM_NODE_RUNNING_LIMIT", "1")))
        self.queue_limit = max(1, int(os.environ.get("DATA_PLATFORM_USER_QUEUE_LIMIT", "20")))

    @staticmethod
    def control(session, job):
        control = session.get(JobControl, job.job_id)
        if control is None:
            control = JobControl(
                job_id=job.job_id, phase=job.status, stop_confirmed=job.status not in ACTIVE_STATES
            )
            session.add(control)
            session.flush()
        return control

    def audit(self, session, job, operation, actor=None, details=None):
        enqueue_event(
            session,
            build_operation_event(
                operation,
                status=job.status,
                actor=actor or {"user_id": job.requested_by},
                source="control-plane",
                details={"job_id": job.job_id, **(details or {})},
            ),
        )

    def on_create(self, session, job):
        self.control(session, job)
        self.audit(session, job, "job.submitted")

    def expire(self, session):
        from lerobot.data_platform.control_plane import RemoteJob, _utcnow

        rows = session.scalars(
            select(RemoteJob).where(
                RemoteJob.status.in_({"running", "cancel_requested"}), RemoteJob.lease_until < _utcnow()
            )
        ).all()
        for job in rows:
            job.status = "interrupted"
            control = self.control(session, job)
            control.revision += 1
            control.stop_confirmed = False
            self.audit(session, job, "job.interrupted")

    def reap_expired(self):
        from lerobot.data_platform.control_plane import RemoteJob, _utcnow

        with self.store.sessions() as session:
            expired = session.scalar(
                select(RemoteJob.job_id)
                .where(
                    RemoteJob.status.in_({"running", "cancel_requested"}), RemoteJob.lease_until < _utcnow()
                )
                .limit(1)
            )
        if expired:
            with self.store.sessions.begin() as session:
                scheduler_lock(session)
                self.expire(session)

    def claim(self, node_id, lease_seconds=60, worker_instance_id=None):
        from lerobot.data_platform.control_plane import ControlPlaneNode, DatasetLocation, RemoteJob, _utcnow
        from lerobot.data_platform.precompute.data_profile import (
            DATA_PROFILE_PROTOCOL,
            required_data_profile_protocol,
        )

        if os.environ.get("DATA_PLATFORM_PAUSE_CLAIMS", "0") == "1":
            return None
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            from lerobot.data_platform.maintenance import is_maintenance

            if is_maintenance(session):
                return None
            self.expire(session)
            node = session.get(ControlPlaneNode, node_id)
            if node is None:
                raise KeyError(node_id)
            from lerobot.data_platform.environment import EnvironmentIdentity

            identity = EnvironmentIdentity.from_env()
            if identity and (
                node.capabilities.get("environment") != identity.name
                or node.capabilities.get("instance_id") != identity.instance_id
                or bool(os.environ.get("DATA_PLATFORM_RELEASE"))
                and node.capabilities.get("release") != os.environ["DATA_PLATFORM_RELEASE"]
            ):
                return None
            active = session.scalars(select(RemoteJob).where(RemoteJob.status.in_(ACTIVE_STATES))).all()
            if sum(job.node_id == node_id for job in active) >= self.node_limit:
                return None
            queued = session.scalars(
                select(RemoteJob).where(RemoteJob.node_id == node_id, RemoteJob.status == "queued")
            ).all()
            candidates = [(job, self.control(session, job)) for job in queued]
            now = time.time()
            candidates.sort(
                key=lambda pair: (
                    -min(2, pair[1].priority + int((now - pair[1].queued_at) / 1800)),
                    pair[1].queued_at,
                    pair[0].job_id,
                )
            )
            for job, control in candidates:
                from lerobot.data_platform.control_plane import SCOPED_SOURCE_OPERATIONS, ControlPlaneUser

                submitter = session.get(ControlPlaneUser, job.requested_by) if job.requested_by else None
                if (
                    submitter is None
                    or not submitter.active
                    or submitter.role not in {"admin", "data_manager", "operator"}
                ):
                    continue
                source_ids = [job.location_id, *(job.options or {}).get("source_location_ids", [])]
                if any(
                    not self.store.dataset_access_allowed(
                        session, submitter, session.get(DatasetLocation, key)
                    )
                    for key in source_ids
                ):
                    continue
                if job.operation.startswith("mutation.") and submitter.role != "admin":
                    from lerobot.data_platform.episode_deletion_requests import has_approved_deletion

                    if not (
                        job.operation in SCOPED_SOURCE_OPERATIONS
                        and self.store.source_mutation_allowed(
                            session, submitter, session.get(DatasetLocation, job.location_id)
                        )
                    ) and not has_approved_deletion(session, job):
                        continue
                if sum(row.requested_by == job.requested_by for row in active) >= self.user_limit:
                    continue
                location = session.get(DatasetLocation, job.location_id)
                if location is None:
                    continue
                if (
                    job.operation.startswith("curation.")
                    and (node.capabilities or {}).get("curation_protocol") != 1
                ):
                    continue
                required = required_data_profile_protocol(location.details or {})
                if job.operation == "viewer.prepare" and job.options.get("force_recompute_stage"):
                    required = DATA_PROFILE_PROTOCOL
                if required > (node.capabilities or {}).get("data_profile_protocol", 0):
                    continue
                # Until shared-filesystem locking is configured, all paths on a node are conservatively exclusive.
                if any(row.node_id == node_id and self._conflicts(session, row, job) for row in active):
                    continue
                protocol = min(JOB_PROTOCOL, int((node.capabilities or {}).get("job_protocol", 1)))
                if protocol >= 2 and not worker_instance_id:
                    raise JobConflictError("Agent worker instance is required")
                token = secrets.token_urlsafe(32)
                attempt = JobAttempt(
                    attempt_id=str(uuid.uuid4()),
                    job_id=job.job_id,
                    attempt_no=1
                    + session.scalar(
                        select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id == job.job_id)
                    ),
                    worker_instance_id=worker_instance_id or node_id,
                    credential_digest=hashlib.sha256(token.encode()).hexdigest(),
                    protocol=protocol,
                    status="running",
                )
                session.add(attempt)
                control.attempt_id, control.phase = attempt.attempt_id, "executing"
                control.stop_confirmed, control.stop_mode = False, None
                control.revision += 1
                if (
                    job.operation.startswith("preprocess.")
                    or job.operation in {"curation.materialize", "curation.construction"}
                ) and not control.final_output:
                    source = PurePosixPath(location.root)
                    control.final_output = job.options.get("out_root") or str(
                        source.parent / f"{source.name}_{job.operation.split('.')[-1]}_{job.job_id}"
                    )
                job.status, job.started_at, job.updated_at = "running", _utcnow(), _utcnow()
                job.finished_at, job.error, job.result = None, None, {}
                job.lease_owner = attempt.worker_instance_id
                job.lease_until = _utcnow() + timedelta(seconds=max(10, int(lease_seconds)))
                session.flush()
                self.audit(session, job, "job.started", details={"attempt_id": attempt.attempt_id})
                payload = self.store._job_dict(job)
                payload.update(self.control_dict(control))
                payload["location"] = self.store._location_dict(location)
                if protocol >= 2:
                    payload["execution"] = {
                        "protocol": protocol,
                        "attempt_id": attempt.attempt_id,
                        "credential": token,
                        "worker_instance_id": attempt.worker_instance_id,
                        "final_output": control.final_output,
                        "input_fingerprint": control.input_fingerprint,
                    }
                return payload
        return None

    def _conflicts(self, session, left, right):
        from lerobot.data_platform.control_plane import DatasetLocation

        def paths(job):
            location = session.get(DatasetLocation, job.location_id)
            values = [location.root, location.output_dir] if location else []
            values += [item["root"] for item in (job.options or {}).get("source_locations", [])]
            if (job.options or {}).get("out_root"):
                values.append(job.options["out_root"])
            return [PurePosixPath(path) for path in values]

        return any(a == b or a in b.parents or b in a.parents for a in paths(left) for b in paths(right))

    def validate(self, session, job, node_id, attempt_id=None, credential=None, *, allow_terminal=False):
        if job is None or job.node_id != node_id:
            raise KeyError("job not found for this node")
        control = self.control(session, job)
        attempt = session.get(JobAttempt, control.attempt_id) if control.attempt_id else None
        if attempt and attempt.protocol >= 2:
            digest = hashlib.sha256(str(credential or "").encode()).hexdigest()
            if attempt.attempt_id != attempt_id or not hmac.compare_digest(digest, attempt.credential_digest):
                raise JobConflictError("Execution attempt is no longer valid")
        if not allow_terminal and job.status not in ACTIVE_STATES:
            raise JobConflictError("Job is not active")
        return control, attempt

    def heartbeat(self, job_id, node_id, *, lease_seconds=60, attempt_id=None, credential=None):
        from lerobot.data_platform.control_plane import ControlPlaneNode, RemoteJob, _utcnow

        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            job = session.get(RemoteJob, job_id)
            control, _ = self.validate(session, job, node_id, attempt_id, credential)
            job.lease_until = _utcnow() + timedelta(seconds=max(10, lease_seconds))
            node = session.get(ControlPlaneNode, node_id)
            node.last_seen_at = _utcnow()
            # Interrupted work must reconcile its result or stop; it cannot silently resume publishing.
            return {
                "renewed": True,
                "stop_mode": control.stop_mode,
                "status": job.status,
                "phase": control.phase,
            }

    def checkpoint(self, job_id, node_id, attempt_id, credential, *, phase, fingerprint=None):
        from lerobot.data_platform.control_plane import RemoteJob

        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            job = session.get(RemoteJob, job_id)
            control, _ = self.validate(session, job, node_id, attempt_id, credential)
            if control.stop_mode or job.status == "cancel_requested":
                raise JobConflictError("Stop requested")
            if phase not in {"executing", "finalizing"}:
                raise ValueError("Unsupported execution phase")
            if fingerprint:
                if control.input_fingerprint and control.input_fingerprint != fingerprint:
                    raise JobConflictError("Input changed since the original execution; create a new job")
                control.input_fingerprint = fingerprint
            if control.phase == "finalizing" and phase != "finalizing":
                raise JobConflictError("Cannot leave finalization")
            job.status = "running"
            control.phase = phase
            control.revision += 1
            return self.control_dict(control)

    def command(self, job_id, actor, action, body, *, owner_only=False):
        from lerobot.data_platform.control_plane import ControlPlaneNode, RemoteJob, _utcnow

        key = str(body.get("idempotency_key") or "")
        if not key or len(key) > 64 or type(body.get("revision")) is not int:
            raise ValueError("revision and idempotency_key (1-64 characters) are required")
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            job = session.get(RemoteJob, job_id)
            if job is None:
                raise KeyError(job_id)
            if owner_only and job.requested_by != actor["user_id"]:
                raise PermissionError("Only the submitter may control a job from Pipeline runs")
            if actor["role"] != "admin" and (
                actor["role"] not in {"operator", "data_manager"} or job.requested_by != actor["user_id"]
            ):
                raise PermissionError("Only the owner or an administrator may control this job")
            if action in {"terminate", "priority"} and actor["role"] != "admin":
                raise PermissionError("Administrator role required")
            command_key = hashlib.sha256(f"{actor['user_id']}:{key}".encode()).hexdigest()
            request = {"action": action, **body}
            previous = session.get(JobCommand, command_key)
            if previous:
                if previous.job_id != job_id or previous.request != request:
                    raise JobConflictError("Idempotency key was already used for another request")
                return previous.response
            control = self.control(session, job)
            if body["revision"] != control.revision:
                raise JobConflictError("Job changed; refresh before trying again")
            attempt = session.get(JobAttempt, control.attempt_id) if control.attempt_id else None
            safe = job.operation in SAFE_OPERATIONS and attempt is not None and attempt.protocol >= 2
            if action == "priority":
                if job.status != "queued":
                    raise JobConflictError("Only queued jobs can change priority")
                priority = body.get("priority")
                if type(priority) is not int or priority not in {0, 1, 2}:
                    raise ValueError("priority must be 0, 1 or 2")
                control.priority = priority
            elif action == "retry":
                if job.status not in {"error", "cancelled", "interrupted"} or not control.stop_confirmed:
                    raise JobConflictError("Retry requires a terminal job and confirmed process exit")
                if job.operation not in SAFE_OPERATIONS or (attempt and attempt.protocol < 2):
                    raise JobConflictError("This operation or legacy execution does not support retry")
                count = session.scalar(
                    select(func.count())
                    .select_from(RemoteJob)
                    .where(RemoteJob.requested_by == job.requested_by, RemoteJob.status == "queued")
                )
                if count >= self.queue_limit:
                    raise JobConflictError("User queue limit reached")
                job.status, control.phase, control.queued_at = "queued", "queued", time.time()
                control.stop_mode = None
            elif action in {"cancel", "terminate"}:
                if job.status == "cancelled":
                    return self.store._job_dict(job) | self.control_dict(control)
                if job.status == "queued" and action == "cancel":
                    job.status, control.phase, control.stop_confirmed = "cancelled", "cancelled", True
                    job.finished_at = _utcnow()
                else:
                    if job.status not in {"running", "cancel_requested"} or control.phase == "finalizing":
                        raise JobConflictError("Job cannot be stopped in this state or during finalization")
                    if not safe:
                        raise JobConflictError(
                            "Operation does not support safe stopping; upgrade the executor if needed"
                        )
                    if action == "terminate":
                        if body.get("confirm") is not True or not str(body.get("reason") or "").strip():
                            raise JobConflictError("Forced termination requires confirmation and a reason")
                        node = session.get(ControlPlaneNode, job.node_id)
                        if (
                            node is None
                            or node.last_seen_at is None
                            or node.last_seen_at < _utcnow() - timedelta(minutes=2)
                        ):
                            raise JobConflictError("Agent is offline; process state must be reconciled")
                    control.stop_mode = (
                        "force" if action == "terminate" or control.stop_mode == "force" else "cooperative"
                    )
                    job.status = "cancel_requested"
            else:
                raise ValueError("Unknown job control action")
            control.revision += 1
            job.updated_at = _utcnow()
            session.flush()
            result = self.store._job_dict(job) | self.control_dict(control)
            session.add(
                JobCommand(
                    command_id=command_key,
                    job_id=job_id,
                    actor_id=actor["user_id"],
                    request=request,
                    response=result,
                )
            )
            self.audit(
                session,
                job,
                f"job.{action}",
                actor,
                {"reason": body.get("reason"), "priority": control.priority},
            )
            return result

    def finish(self, job_id, node_id, *, status, result=None, error=None, attempt_id=None, credential=None):
        from lerobot.data_platform.control_plane import RemoteJob, _utcnow

        if status not in {"done", "error", "cancelled"}:
            raise ValueError("Invalid terminal status")
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            job = session.get(RemoteJob, job_id)
            control, attempt = self.validate(
                session, job, node_id, attempt_id, credential, allow_terminal=True
            )
            if job.status in {"done", "error", "cancelled"}:
                if job.status != status:
                    raise JobConflictError("Job is already terminal with a different status")
                return self.store._job_dict(job)
            if status == "done" and attempt and attempt.protocol >= 2 and control.phase != "finalizing":
                raise JobConflictError("Finalization handshake required")
            if status == "done" and control.stop_mode:
                raise JobConflictError("Stopped execution cannot publish results")
            if job.status not in ACTIVE_STATES:
                raise JobConflictError("Job is not active")
            job.status, job.result, job.error = (
                status,
                dict(result or {}),
                sanitize_for_log(error),
            )
            job.finished_at = job.updated_at = _utcnow()
            job.lease_owner = job.lease_until = None
            control.stop_confirmed, control.phase = True, status
            control.revision += 1
            if attempt:
                attempt.status, attempt.finished_at = status, time.time()
                attempt.result, attempt.error = job.result, job.error
            self.audit(session, job, "job.completed", details={"attempt_id": control.attempt_id})
            return self.store._job_dict(job)

    @staticmethod
    def control_dict(control):
        return {
            "revision": control.revision,
            "priority": control.priority,
            "phase": control.phase,
            "attempt_id": control.attempt_id,
            "stop_mode": control.stop_mode,
            "stop_confirmed": control.stop_confirmed,
            "queued_at": control.queued_at,
        }

    def queue_ahead(self, session, payload):
        """Return only a count of preceding work on this task's execution node."""
        from lerobot.data_platform.control_plane import RemoteJob

        if payload["status"] != "queued":
            return 0
        rows = session.execute(
            select(RemoteJob, JobControl)
            .join(JobControl, JobControl.job_id == RemoteJob.job_id)
            .where(RemoteJob.node_id == payload["node_id"], RemoteJob.status.in_({"queued"} | ACTIVE_STATES))
        ).all()
        now = time.time()

        def rank(job, control):
            return (
                -min(2, control.priority + int((now - control.queued_at) / 1800)),
                control.queued_at,
                job.job_id,
            )

        current = next((rank(job, control) for job, control in rows if job.job_id == payload["job_id"]), None)
        if current is None:
            return 0
        return sum(
            job.job_id != payload["job_id"] and (job.status in ACTIVE_STATES or rank(job, control) < current)
            for job, control in rows
        )

    def decorate(self, payload, actor=None):
        from lerobot.data_platform.control_plane import ControlPlaneUser

        with self.store.sessions() as session:
            submitter = (
                session.get(ControlPlaneUser, payload["requested_by"])
                if payload.get("requested_by")
                else None
            )
            payload["requested_by_username"] = submitter.username if submitter else None
            payload["queue_ahead"] = self.queue_ahead(session, payload)
            control = session.get(JobControl, payload["job_id"])
            if control:
                payload.update(self.control_dict(control))
            else:
                payload.update({"revision": 0, "priority": 1, "phase": payload["status"]})
            attempt = session.get(JobAttempt, control.attempt_id) if control and control.attempt_id else None
            capable = payload["operation"] in SAFE_OPERATIONS and attempt and attempt.protocol >= 2
            owner = actor and (
                actor["role"] == "admin"
                or (
                    actor["role"] in {"operator", "data_manager"}
                    and actor["user_id"] == payload["requested_by"]
                )
            )
            actions = []
            if owner:
                if payload["status"] == "queued":
                    actions.append("cancel")
                    if actor["role"] == "admin":
                        actions.append("priority")
                if (
                    capable
                    and payload["status"] in {"running", "cancel_requested"}
                    and control.phase != "finalizing"
                ):
                    actions.append("cancel")
                    if actor["role"] == "admin":
                        from lerobot.data_platform.control_plane import ControlPlaneNode, _utcnow

                        node = session.get(ControlPlaneNode, payload["node_id"])
                        if (
                            node
                            and node.last_seen_at
                            and node.last_seen_at >= _utcnow() - timedelta(minutes=2)
                        ):
                            actions.append("terminate")
                if (
                    payload["operation"] in SAFE_OPERATIONS
                    and control
                    and control.stop_confirmed
                    and payload["status"] in {"error", "cancelled", "interrupted"}
                    and (not attempt or capable)
                ):
                    actions.append("retry")
            payload["progress"] = (payload.get("result") or {}).get("progress", {})
            payload["available_actions"] = actions
            reasons = {}
            for action in ("retry", "cancel"):
                if action in actions:
                    continue
                if not owner:
                    reason = "Only the submitter or an administrator can control this task"
                elif action == "retry" and payload["status"] not in {"error", "cancelled", "interrupted"}:
                    reason = "Retry is available after a failed or cancelled execution"
                elif action == "retry" and (not control or not control.stop_confirmed):
                    reason = "Execution exit has not been confirmed"
                elif action == "cancel" and payload["status"] not in {
                    "queued",
                    "running",
                    "cancel_requested",
                }:
                    reason = "This task is no longer queued or running"
                elif control and control.phase == "finalizing":
                    reason = "Output is being committed; stopping is temporarily unavailable"
                elif payload["operation"] not in SAFE_OPERATIONS:
                    reason = "This task type has not yet been adapted for safe retry or running cancellation"
                else:
                    reason = "This execution protocol does not support the operation"
                reasons[action] = reason
            payload["action_reasons"] = reasons
            payload["control_reason"] = (
                "" if actions else "No supported action for this role, execution protocol or state"
            )
            payload["queue_reason"] = (
                "Waiting for node capacity, user quota or data locks" if payload["status"] == "queued" else ""
            )
            return payload

    def attempts(self, job_id):
        with self.store.sessions() as session:
            return [
                {
                    key: sanitize_for_log(getattr(row, key))
                    for key in (
                        "attempt_id",
                        "job_id",
                        "attempt_no",
                        "worker_instance_id",
                        "status",
                        "started_at",
                        "finished_at",
                        "result",
                        "error",
                    )
                }
                for row in session.scalars(
                    select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt_no)
                ).all()
            ]

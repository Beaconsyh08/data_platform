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

from lerobot.data_platform.job_concurrency import PARALLEL_OPERATIONS, footprint
from lerobot.data_platform.job_concurrency import conflicts as resource_conflicts
from lerobot.data_platform.management_storage import (
    JobAttempt,
    JobCommand,
    JobControl,
    SchedulerLock,
    enqueue_event,
)
from lerobot.data_platform.operation_log import build_operation_event, sanitize_for_log
from lerobot.data_platform.operators.registry import operation_ids

JOB_PROTOCOL = 2
ACTIVE_STATES = {"running", "cancel_requested", "interrupted"}
MCAP_RECOVERY_LIMIT = 3
PROCESS_LOSS_ERROR = "Compute process exited without a result"
SAFE_OPERATIONS = operation_ids(remote=True, cancellable=True)


class JobConflictError(ValueError):
    pass


def is_recovered_attempt(attempt: JobAttempt | None) -> bool:
    recovery = (attempt.result or {}).get("auto_recovery") if attempt else None
    return bool(
        attempt
        and attempt.protocol >= 2
        and attempt.status == "error"
        and attempt.finished_at is not None
        and isinstance(recovery, dict)
        and recovery.get("reason") == "compute_process_lost"
    )


def scheduler_lock(session):
    if session.get_bind().dialect.name == "mysql":
        # InnoDB can serialize scheduling without generating a row-change binlog event.
        session.execute(
            select(SchedulerLock.lock_id).where(SchedulerLock.lock_id == 1).with_for_update()
        ).scalar_one()
    else:
        # SQLite needs an actual write to acquire its database write lock.
        session.execute(
            update(SchedulerLock)
            .where(SchedulerLock.lock_id == 1)
            .values(revision=SchedulerLock.revision + 1)
        )


class JobManager:
    def __init__(self, store):
        self.store = store
        self.user_limit = max(1, int(os.environ.get("DATA_PLATFORM_USER_RUNNING_LIMIT", "1")))
        self.node_limit = max(1, int(os.environ.get("DATA_PLATFORM_NODE_RUNNING_LIMIT", "1")))
        self.caption_limit = max(1, int(os.environ.get("DATA_PLATFORM_CAPTION_RUNNING_LIMIT", "1")))
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

    @staticmethod
    def user_running_count(session, user_id):
        """Count ordinary slots across all nodes, including executions awaiting confirmed exit."""
        from lerobot.data_platform.control_plane import RemoteJob

        return session.scalar(
            select(func.count())
            .select_from(RemoteJob)
            .outerjoin(JobControl, JobControl.job_id == RemoteJob.job_id)
            .where(
                RemoteJob.requested_by == user_id,
                RemoteJob.status.in_(ACTIVE_STATES),
                func.coalesce(JobControl.extra_quota, False).is_(False),
            )
        )

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

    def claim(self, node_id, lease_seconds=60, worker_instance_id=None, *, release=None):
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
            if node.capabilities.get("rolling_deploy_protocol") and (
                release != node.capabilities.get("release")
                or release != os.environ.get("DATA_PLATFORM_RELEASE")
            ):
                return None
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
            node_active = [job for job in active if job.node_id == node_id]
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
                if (job.options or {}).get("local_compute") and not (node.capabilities or {}).get(
                    "local_requests"
                ):
                    continue
                snapshot = (job.options or {}).get("operator_snapshot")
                if snapshot:
                    caps = node.capabilities or {}
                    key = snapshot.get("operator_id", job.operation)
                    installed = caps.get("operator_version_history", {}).get(
                        key, [caps.get("operator_versions", {}).get(key)]
                    )
                    if snapshot.get("version") not in installed:
                        continue
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
                capability = (node.capabilities or {}).get("job_concurrent_limit")
                node_limit = min(self.node_limit, max(1, int(capability or 1)))
                user_limit = self.user_limit
                if capability is None and job.operation == "caption.annotate":
                    if any(row.operation != "caption.annotate" for row in node_active):
                        continue
                    legacy_caption_limit = min(
                        self.caption_limit,
                        max(1, int((node.capabilities or {}).get("caption_concurrent_limit", 1))),
                    )
                    node_limit = max(node_limit, legacy_caption_limit)
                    user_limit = max(user_limit, legacy_caption_limit)
                if (
                    len(node_active) >= node_limit
                    or not control.extra_quota
                    and self.user_running_count(session, job.requested_by) >= user_limit
                ):
                    continue
                location = session.get(DatasetLocation, job.location_id)
                if location is None:
                    continue
                if job.operation.startswith("import.mcap.") and (
                    node.capabilities.get("job_protocol", 1) < 2
                    or node.capabilities.get("mcap_import_protocol") != 1
                    or job.operation not in node.capabilities.get("operations", [])
                    or not node.capabilities.get("mcap_import", {}).get("ready")
                ):
                    continue
                if (
                    job.operation.startswith("curation.")
                    and (node.capabilities or {}).get("curation_protocol") != 1
                ):
                    continue
                if job.operation == "curation.quality":
                    parameters = (job.options or {}).get("parameters", {})
                    if any(
                        key in parameters for key in ("checks", "qc_config", "video_mode", "video_workers")
                    ):
                        caps = node.capabilities or {}
                        if caps.get("quality_protocol") != 1 or not set(
                            parameters.get("checks", ["legacy"])
                        ).issubset(caps.get("quality_checks", [])):
                            continue
                required = required_data_profile_protocol(location.details or {})
                if job.operation == "viewer.prepare" and job.options.get("force_recompute_stage"):
                    required = DATA_PROFILE_PROTOCOL
                if required > (node.capabilities or {}).get("data_profile_protocol", 0):
                    continue
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
                    or job.operation
                    in {"curation.materialize", "curation.construction", "import.mcap.convert"}
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
                        "release": node.capabilities.get("release"),
                        "final_output": control.final_output,
                        "input_fingerprint": control.input_fingerprint,
                    }
                return payload
        return None

    def _conflicts(self, session, left, right):
        from lerobot.data_platform.control_plane import DatasetLocation

        def describe(job):
            location = session.get(DatasetLocation, job.location_id)
            if location is None:
                raise ValueError("Missing registered job location")
            options = dict(job.options or {})
            if options.get("source_location_ids") and not options.get("_source_locations"):
                options["_source_locations"] = [
                    {
                        "root": source.root,
                        "output_dir": source.output_dir,
                    }
                    for key in options["source_location_ids"]
                    if (source := session.get(DatasetLocation, key)) is not None
                ]
                if len(options["_source_locations"]) != len(options["source_location_ids"]):
                    raise ValueError("Missing registered source location")
            return footprint(
                job.operation,
                location.root,
                location.output_dir,
                options,
                job.job_id,
                final_output=self.control(session, job).final_output,
            )

        try:
            return resource_conflicts(describe(left), describe(right))
        except (KeyError, TypeError, ValueError):
            return True

    def validate(self, session, job, node_id, attempt_id=None, credential=None, *, allow_terminal=False):
        if job is None or job.node_id != node_id:
            raise KeyError("job not found for this node")
        control = self.control(session, job)
        attempt = session.get(JobAttempt, control.attempt_id) if control.attempt_id else None
        if allow_terminal and job.operation == "import.mcap.convert" and attempt_id != control.attempt_id:
            previous = session.get(JobAttempt, attempt_id) if attempt_id else None
            if previous and previous.job_id == job.job_id and is_recovered_attempt(previous):
                # Only completion acknowledgement may replay a recovered attempt; all writes stay fenced.
                attempt = previous
        if attempt and attempt.protocol >= 2:
            digest = hashlib.sha256(str(credential or "").encode()).hexdigest()
            if attempt.attempt_id != attempt_id or not hmac.compare_digest(digest, attempt.credential_digest):
                raise JobConflictError("Execution attempt is no longer valid")
        if not allow_terminal and job.status not in ACTIVE_STATES:
            raise JobConflictError("Job is not active")
        return control, attempt

    def _recovery_number(self, session, job, control, attempt, status, error):
        if (
            job.operation != "import.mcap.convert"
            or status != "error"
            or not isinstance(error, str)
            or error != PROCESS_LOSS_ERROR
            or job.status not in {"running", "interrupted"}
            or control.stop_mode
            or control.phase == "finalizing"
            or attempt is None
            or attempt.protocol < 2
        ):
            return None
        previous = session.scalars(
            select(JobAttempt)
            .where(JobAttempt.job_id == job.job_id, JobAttempt.attempt_no < attempt.attempt_no)
            .order_by(JobAttempt.attempt_no.desc())
            .limit(MCAP_RECOVERY_LIMIT)
        )
        number = 1
        for record in previous:
            if not is_recovered_attempt(record):
                break
            number += 1
        return number if number <= MCAP_RECOVERY_LIMIT else None

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
            if action in {"terminate", "priority", "extra-quota"} and actor["role"] != "admin":
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
            if action == "extra-quota":
                if job.status != "queued":
                    raise JobConflictError("Only queued jobs can change extra quota")
                if type(body.get("enabled")) is not bool:
                    raise ValueError("enabled must be a boolean")
                control.extra_quota = body["enabled"]
            elif action == "priority":
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
                job.result, job.error = {}, None
                job.started_at = job.finished_at = None
                control.stop_mode = None
                control.extra_quota = False
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
                {
                    "reason": body.get("reason"),
                    "priority": control.priority,
                    "extra_quota": control.extra_quota,
                },
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
            if is_recovered_attempt(attempt):
                if status != attempt.status:
                    raise JobConflictError("Conflicting recovered attempt status")
                return self.store._job_dict(job)
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
            recovery_number = self._recovery_number(session, job, control, attempt, status, error)
            progress = (job.result or {}).get("progress", {})
            job.status, job.result, job.error = (
                status,
                {
                    **dict(result or {}),
                    "progress": {**progress, **({"progress": 100} if status == "done" else {})},
                },
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
            if recovery_number is not None:
                recovery = {
                    "reason": "compute_process_lost",
                    "number": recovery_number,
                    "limit": MCAP_RECOVERY_LIMIT,
                }
                attempt.result = {**attempt.result, "auto_recovery": recovery}
                job.status, control.phase = "queued", "queued"
                job.result = {
                    "auto_recovery": recovery,
                    "progress": {
                        "phase": "queued",
                        "message": "Automatically queued to resume MCAP conversion from verified checkpoints",
                    },
                }
                job.error = job.started_at = job.finished_at = None
                self.audit(
                    session,
                    job,
                    "job.auto_requeued",
                    details={"attempt_id": attempt.attempt_id, **recovery},
                )
            return self.store._job_dict(job)

    @staticmethod
    def control_dict(control):
        return {
            "revision": control.revision,
            "priority": control.priority,
            "extra_quota": control.extra_quota,
            "phase": control.phase,
            "attempt_id": control.attempt_id,
            "stop_mode": control.stop_mode,
            "stop_confirmed": control.stop_confirmed,
            "queued_at": control.queued_at,
        }

    def queue_counts(self, session, node_ids):
        """Read queue ranks without loading options/results or repeating a query per task."""
        from lerobot.data_platform.control_plane import RemoteJob

        if not node_ids:
            return {}
        rows = session.execute(
            select(
                RemoteJob.job_id,
                RemoteJob.node_id,
                RemoteJob.status,
                JobControl.priority,
                JobControl.queued_at,
            )
            .join(JobControl, JobControl.job_id == RemoteJob.job_id)
            .where(RemoteJob.node_id.in_(node_ids), RemoteJob.status.in_({"queued"} | ACTIVE_STATES))
        ).all()
        now = time.time()
        counts = {}
        for node_id in node_ids:
            active_count = sum(row.node_id == node_id and row.status in ACTIVE_STATES for row in rows)
            queued = sorted(
                (row for row in rows if row.node_id == node_id and row.status == "queued"),
                key=lambda row: (
                    -min(2, row.priority + int((now - row.queued_at) / 1800)),
                    row.queued_at,
                    row.job_id,
                ),
            )
            counts.update({row.job_id: active_count + index for index, row in enumerate(queued)})
        return counts

    def queue_ahead(self, session, payload):
        if payload["status"] != "queued":
            return 0
        return self.queue_counts(session, {payload["node_id"]}).get(payload["job_id"], 0)

    def decorate(self, payload, actor=None):
        return self.decorate_many([payload], actor)[0]

    def decorate_many(self, payloads, actor=None):
        """Share one request snapshot for task controls, capacity and queue positions."""
        from lerobot.data_platform.control_plane import (
            ControlPlaneNode,
            ControlPlaneUser,
            DatasetLocation,
            RemoteJob,
        )

        if not payloads:
            return []
        with self.store.sessions() as session:
            users = {
                user.user_id: user
                for user in session.scalars(
                    select(ControlPlaneUser).where(
                        ControlPlaneUser.user_id.in_(
                            {p["requested_by"] for p in payloads if p.get("requested_by")}
                        )
                    )
                )
            }
            nodes = {
                node.node_id: node
                for node in session.scalars(
                    select(ControlPlaneNode).where(
                        ControlPlaneNode.node_id.in_({p["node_id"] for p in payloads})
                    )
                )
            }
            active_jobs = session.scalars(select(RemoteJob).where(RemoteJob.status.in_(ACTIVE_STATES))).all()
            controls = {
                control.job_id: control
                for control in session.scalars(
                    select(JobControl).where(
                        JobControl.job_id.in_(
                            {p["job_id"] for p in payloads} | {job.job_id for job in active_jobs}
                        )
                    )
                )
            }
            attempts = {
                attempt.attempt_id: attempt
                for attempt in session.scalars(
                    select(JobAttempt).where(
                        JobAttempt.attempt_id.in_(
                            {control.attempt_id for control in controls.values() if control.attempt_id}
                        )
                    )
                )
            }
            queued_nodes = {p["node_id"] for p in payloads if p["status"] == "queued"}
            queue_counts = self.queue_counts(session, queued_nodes)
            current_jobs = (
                {
                    job.job_id: job
                    for job in session.scalars(
                        select(RemoteJob).where(
                            RemoteJob.job_id.in_({p["job_id"] for p in payloads if p["status"] == "queued"})
                        )
                    )
                }
                if active_jobs and queued_nodes
                else {}
            )
            # Keep ORM rows alive so conflict checks reuse the same session identity map.
            location_ids = {
                key
                for job in [*active_jobs, *current_jobs.values()]
                for key in [job.location_id, *(job.options or {}).get("source_location_ids", [])]
            }
            _locations = (
                session.scalars(
                    select(DatasetLocation).where(DatasetLocation.location_id.in_(location_ids))
                ).all()
                if current_jobs
                else []
            )
            user_counts = {}
            for job in active_jobs:
                control = controls.get(job.job_id)
                if not (control and control.extra_quota):
                    user_counts[job.requested_by] = user_counts.get(job.requested_by, 0) + 1
            for payload in payloads:
                submitter = users.get(payload.get("requested_by"))
                payload["requested_by_username"] = submitter.username if submitter else None
                payload["queue_ahead"] = queue_counts.get(payload["job_id"], 0)
                node = nodes.get(payload["node_id"])
                active = [job for job in active_jobs if job.node_id == payload["node_id"]]
                capability = (node.capabilities or {}).get("job_concurrent_limit") if node else None
                limit = min(self.node_limit, max(1, int(capability or 1)))
                user_limit = self.user_limit
                if capability is None and payload["operation"] == "caption.annotate" and node:
                    legacy_limit = min(
                        self.caption_limit,
                        max(1, int((node.capabilities or {}).get("caption_concurrent_limit", 1))),
                    )
                    limit = max(limit, legacy_limit)
                    user_limit = max(user_limit, legacy_limit)
                payload["node_slots"] = {"used": len(active), "limit": limit}
                control = controls.get(payload["job_id"])
                payload["queue_wait_reason"] = None
                if payload["status"] == "queued":
                    current = current_jobs.get(payload["job_id"])
                    conflicting = [
                        row for row in active if current and self._conflicts(session, row, current)
                    ]
                    if conflicting:
                        payload["queue_wait_reason"] = (
                            "exclusive_job"
                            if current.operation not in PARALLEL_OPERATIONS
                            or any(row.operation not in PARALLEL_OPERATIONS for row in conflicting)
                            else "data_path_conflict"
                        )
                    elif len(active) >= limit:
                        payload["queue_wait_reason"] = "node_capacity"
                    elif (
                        not (control and control.extra_quota)
                        and user_counts.get(payload["requested_by"], 0) >= user_limit
                    ):
                        payload["queue_wait_reason"] = "user_capacity"
                    elif not node or not capability:
                        payload["queue_wait_reason"] = "agent_capacity"
                if control:
                    payload.update(self.control_dict(control))
                else:
                    payload.update(
                        {"revision": 0, "priority": 1, "phase": payload["status"], "extra_quota": False}
                    )
                attempt = attempts.get(control.attempt_id) if control else None
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
                            actions.extend(["priority", "extra-quota"])
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
                        reason = (
                            "This task type has not yet been adapted for safe retry or running cancellation"
                        )
                    else:
                        reason = "This execution protocol does not support the operation"
                    reasons[action] = reason
                payload["action_reasons"] = reasons
                payload["control_reason"] = (
                    "" if actions else "No supported action for this role, execution protocol or state"
                )
                payload["queue_reason"] = ""
                if payload["status"] == "queued":
                    payload["queue_reason"] = {
                        "user_capacity": "Waiting for user quota",
                        "node_capacity": "Waiting for a free node slot",
                        "data_path_conflict": "Waiting for conflicting dataset paths",
                        "exclusive_job": "Waiting for an exclusive task",
                        "agent_capacity": "Waiting for Agent capacity",
                    }.get(payload["queue_wait_reason"], "Waiting for Agent claim and admission checks")
            return payloads

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

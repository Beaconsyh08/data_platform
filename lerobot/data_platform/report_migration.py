"""Opt-in, bounded and reversible report replacement; network IO stays outside transactions."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from dataclasses import dataclass

from sqlalchemy import cast, func, literal, or_, select
from sqlalchemy.dialects.mysql import DOUBLE, JSON

from lerobot.data_platform.artifact_store import ArtifactRef, atomic_json, staging_directory
from lerobot.data_platform.control_plane import RemoteJob
from lerobot.data_platform.job_management import scheduler_lock
from lerobot.data_platform.management_storage import JobAttempt, JobControl
from lerobot.data_platform.processing.models import PipelineRun, PipelineStepRun
from lerobot.data_platform.report_archive import (
    REPORT_PATHS,
    archived_fields,
    field_value,
    hydrate_result,
    reference,
    set_field,
)

THRESHOLD = 256 * 1024
MAX_RECORD_BYTES = 64 * 1024**2
TERMINAL = {"done", "error", "cancelled"}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def checksum(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _float_repairs(expected, actual, path="$"):
    """Find only numeric parser drift; any other persisted difference is an error."""
    if checksum(expected) == checksum(actual):
        return []
    if isinstance(expected, float) and math.isfinite(expected) and type(actual) in (int, float):
        return [(path, expected)]
    if isinstance(expected, dict) and isinstance(actual, dict) and expected.keys() == actual.keys():
        return [
            repair
            for key in expected
            for repair in _float_repairs(expected[key], actual[key], path + "." + json.dumps(key))
        ]
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        return [
            repair
            for index, (left, right) in enumerate(zip(expected, actual, strict=True))
            for repair in _float_repairs(left, right, f"{path}[{index}]")
        ]
    raise ValueError("Persisted report differs from the verified replacement")


def _replace_result(session, row, desired):
    if session.bind.dialect.name != "mysql":
        row.result = desired
    else:
        # Preserve the binary JSON outside the archive whitelist. Re-parsing the whole
        # document on MySQL can change floating-point values in unrelated metadata.
        expression = type(row).result
        for path in (*REPORT_PATHS, ("_archive_fields",)):
            value = field_value(desired, path)
            if checksum(value) == checksum(field_value(row.result, path)):
                continue
            if value is not None:
                expression = func.json_set(
                    expression, "$." + ".".join(path), cast(literal(encoded(value).decode()), JSON)
                )
            elif path == ("_archive_fields",):
                expression = func.json_remove(expression, "$._archive_fields")
        row.result = expression
    session.flush()
    session.refresh(row, ["result"])
    if session.bind.dialect.name == "mysql":
        repairs = _float_repairs(desired, row.result)
        # MySQL's JSON text parser can lose a float ULP. SQL's DOUBLE parser does
        # preserve it; patch only affected leaves, with bounded statement size.
        for start in range(0, len(repairs), 100):
            arguments = []
            for path, value in repairs[start : start + 100]:
                arguments.extend((path, cast(literal(repr(value)), DOUBLE)))
            row.result = func.json_set(type(row).result, *arguments)
            session.flush()
            session.refresh(row, ["result"])
    if checksum(row.result) != checksum(desired):
        raise ValueError("Persisted report differs from the verified replacement")


@dataclass(frozen=True)
class MigrationResult:
    examined: int = 0
    migrated: int = 0
    bytes_externalized: int = 0
    next_cursor: str | None = None


class ReportMigrator:
    def __init__(self, control, artifacts):
        self.control = control
        self.artifacts = artifacts

    def _rows(self, session, job_id):
        job = session.get(RemoteJob, job_id, with_for_update=True)
        if job is None:
            raise ValueError("Job no longer exists")
        attempts = session.scalars(
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.attempt_id)
            .with_for_update()
        ).all()
        return job, attempts

    def _eligible(self, session, job, attempts):
        control = session.get(JobControl, job.job_id)
        if (
            job.status != "done"
            or job.finished_at is None
            or job.lease_owner
            or job.lease_until
            or not control
            or control.phase != "done"
            or not control.stop_confirmed
            or not attempts
            or control.attempt_id not in {a.attempt_id for a in attempts}
            or any(
                a.status != "done" or a.finished_at is None or (a.result or {}).get("auto_recovery")
                for a in attempts
            )
        ):
            return False
        # Conservatively protect all work on this location, including pending review/retry pipelines.
        if session.scalar(
            select(RemoteJob.job_id)
            .where(RemoteJob.location_id == job.location_id, RemoteJob.status.not_in(TERMINAL))
            .limit(1)
        ):
            return False
        return not session.scalar(
            select(PipelineRun.run_id)
            .outerjoin(PipelineStepRun, PipelineStepRun.run_id == PipelineRun.run_id)
            .where(
                or_(PipelineRun.location_id == job.location_id, PipelineStepRun.job_id == job.job_id),
                PipelineRun.status.not_in(TERMINAL),
            )
            .limit(1)
        )

    @staticmethod
    def _values(job, attempts):
        return {
            "job": copy.deepcopy(job.result or {}),
            **{a.attempt_id: copy.deepcopy(a.result or {}) for a in attempts},
        }

    @staticmethod
    def _revision(session, job):
        state = session.get(JobControl, job.job_id)
        return (job.updated_at, state.revision if state else None, checksum(job.options))

    def migrate(self, job_id, *, byte_limit=MAX_RECORD_BYTES):
        if not self.artifacts.config.migration_enabled:
            raise ValueError("History migration is disabled; deploy and accept the reader first")
        self.artifacts.check_space()
        with self.control.sessions() as session:
            size = (
                session.scalar(select(func.length(RemoteJob.result)).where(RemoteJob.job_id == job_id)) or 0
            )
            size += (
                session.scalar(
                    select(func.sum(func.length(JobAttempt.result))).where(JobAttempt.job_id == job_id)
                )
                or 0
            )
            if size > MAX_RECORD_BYTES:
                return 0
            job, attempts = self._rows(session, job_id)
            if not self._eligible(session, job, attempts):
                return 0
            before = self._values(job, attempts)
            revision = self._revision(session, job)
        if any(archived_fields(value) for value in before.values()):
            return 0
        after = copy.deepcopy(before)
        values = {}
        count = 0
        for row_id, result in before.items():
            for path in REPORT_PATHS:
                value = field_value(result, path)
                if value is None or (isinstance(value, dict) and "_oss_artifact" in value):
                    continue
                payload = encoded(value)
                if len(payload) <= THRESHOLD:
                    continue
                count += len(payload)
                if count > min(byte_limit, MAX_RECORD_BYTES):
                    return 0
                digest = hashlib.sha256(payload).hexdigest()
                values.setdefault(digest, {"payload": payload, "targets": []})["targets"].append(
                    (row_id, path)
                )
        if not values:
            return 0
        # Uploads are idempotent; a crash here leaves the inline reports intact.
        for item in values.values():
            self.artifacts.check_space(len(item["payload"]))
            with staging_directory(self.artifacts, "migration") as directory:
                source = directory / "report.json"
                source.write_bytes(item["payload"])
                ref = self.artifacts.enqueue(source, "reports")
            self.artifacts.flush()
            if not self.artifacts.verified(ref):
                raise RuntimeError("Report upload is pending; retry migration after flushing the spool")
            for row_id, path in item["targets"]:
                set_field(after[row_id], path, reference(ref))
        for result in after.values():
            fields = [
                ".".join(path)
                for path in REPORT_PATHS
                if isinstance(field_value(result, path), dict)
                and "_oss_artifact" in field_value(result, path)
            ]
            if fields:
                result["_archive_fields"] = fields
        manifest = self.artifacts.root / "migrations" / f"{job_id}.json"
        journal = {
            "schema_version": 1,
            "job_id": job_id,
            "minimum_reader": 1,
            "created_at": time.time(),
            "before": {key: checksum(value) for key, value in before.items()},
            "after": {key: checksum(value) for key, value in after.items()},
            "references": [
                reference(ArtifactRef.parse(field_value(value, path)["_oss_artifact"]))
                for value in after.values()
                for path in REPORT_PATHS
                if isinstance(field_value(value, path), dict) and "_oss_artifact" in field_value(value, path)
            ],
        }
        # A compact journal is durable before committing the replacement. No report payload duplication.
        with self.control.sessions.begin() as session:
            scheduler_lock(session)
            job, attempts = self._rows(session, job_id)
            if (
                not self._eligible(session, job, attempts)
                or self._values(job, attempts) != before
                or self._revision(session, job) != revision
            ):
                return 0
            _replace_result(session, job, after["job"])
            for attempt in attempts:
                _replace_result(session, attempt, after[attempt.attempt_id])
            atomic_json(manifest, journal)
            self.control.job_manager.audit(session, job, "job.reports_archived", details={"bytes": count})
        return count

    def restore(self, job_id):
        manifest = self.artifacts.root / "migrations" / f"{job_id}.json"
        journal = json.loads(manifest.read_text())
        if journal.get("schema_version") != 1 or journal.get("job_id") != job_id:
            raise ValueError("Invalid migration journal")
        with self.control.sessions() as session:
            job, attempts = self._rows(session, job_id)
            before = self._values(job, attempts)
        hashes = {key: checksum(value) for key, value in before.items()}
        if hashes == journal["before"]:
            return False  # Not applied or already restored.
        if hashes != journal["after"]:
            raise ValueError("Record changed since migration; manual reconciliation required")
        after = {key: hydrate_result(value, self.artifacts) for key, value in before.items()}
        if {key: checksum(value) for key, value in after.items()} != journal["before"]:
            raise ValueError("Restored report does not match the original")
        with self.control.sessions.begin() as session:
            scheduler_lock(session)
            job, attempts = self._rows(session, job_id)
            if self._values(job, attempts) != before or job.status != "done" or job.lease_owner:
                raise ValueError("Record changed during restore")
            _replace_result(session, job, after["job"])
            for attempt in attempts:
                _replace_result(session, attempt, after[attempt.attempt_id])
            self.control.job_manager.audit(session, job, "job.reports_restored")
        return True

    def batch(self, *, cursor="", records=20, byte_limit=64 * 1024**2):
        if not 1 <= records <= 200 or not 0 < byte_limit <= MAX_RECORD_BYTES:
            raise ValueError("Invalid migration batch limits")
        with self.control.sessions() as session:
            ids = list(
                session.scalars(
                    select(RemoteJob.job_id)
                    .where(RemoteJob.status == "done", RemoteJob.job_id > cursor)
                    .order_by(RemoteJob.job_id)
                    .limit(records)
                )
            )
        total, migrated = 0, 0
        last = cursor
        for job_id in ids:
            size = self.migrate(job_id, byte_limit=byte_limit - total)
            total += size
            migrated += bool(size)
            last = job_id
            if total >= byte_limit:
                break
        return MigrationResult(len(ids), migrated, total, last if ids else None)

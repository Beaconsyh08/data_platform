"""Versioned storage for task controls and independently delivered audit events."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from lerobot.data_platform.operation_log import sanitize_for_log


class ManagementBase(DeclarativeBase):
    pass


class SchemaMigration(ManagementBase):
    __tablename__ = "dp_management_migrations"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)


class SchedulerLock(ManagementBase):
    __tablename__ = "dp_scheduler_lock"
    lock_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class JobControl(ManagementBase):
    __tablename__ = "dp_job_controls"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    queued_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    phase: Mapped[str] = mapped_column(String(32), default="queued")
    stop_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stop_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_output: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobAttempt(ManagementBase):
    __tablename__ = "dp_job_attempts"
    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    worker_instance_id: Mapped[str] = mapped_column(String(128))
    credential_digest: Mapped[str] = mapped_column(String(64))
    protocol: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[float] = mapped_column(Float, default=time.time)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobCommand(ManagementBase):
    __tablename__ = "dp_job_commands"
    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    request: Mapped[dict] = mapped_column(JSON)
    response: Mapped[dict] = mapped_column(JSON)


class EventReceipt(ManagementBase):
    __tablename__ = "dp_job_event_receipts"
    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer)


class AuditOutbox(ManagementBase):
    __tablename__ = "dp_audit_outbox"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    payload: Mapped[dict] = mapped_column(JSON)


def migrate_management(engine) -> None:
    """Migration 1 creates management tables; serialize startup migrations across processes."""
    import hashlib

    from lerobot.data_platform import maintenance  # noqa: F401 -- register the maintenance table

    lock_key = "dp-management-" + hashlib.sha256(str(engine.url.database).encode()).hexdigest()[:32]
    with engine.connect() as conn:
        mysql = engine.dialect.name == "mysql"
        if mysql:
            if conn.exec_driver_sql("SELECT GET_LOCK(%s, 30)", (lock_key,)).scalar() != 1:
                raise RuntimeError("Timed out waiting for management schema migration")
        elif engine.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            ManagementBase.metadata.create_all(conn)
            if conn.scalar(select(SchemaMigration.version).where(SchemaMigration.version == 1)) is None:
                conn.execute(SchedulerLock.__table__.insert().values(lock_id=1, revision=0))
                conn.execute(SchemaMigration.__table__.insert().values(version=1))
            if conn.scalar(select(SchemaMigration.version).where(SchemaMigration.version == 2)) is None:
                conn.execute(SchemaMigration.__table__.insert().values(version=2))
            conn.commit()
        finally:
            if mysql:
                conn.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (lock_key,))


def enqueue_event(session, event: dict) -> None:
    limit = int(os.environ.get("DATA_PLATFORM_OUTBOX_MAX_EVENTS", "100000"))
    if session.scalar(select(func.count()).select_from(AuditOutbox)) >= limit:
        raise RuntimeError("Audit outbox is full; restore log delivery before submitting more work")
    session.add(AuditOutbox(event_id=event["event_id"], payload=sanitize_for_log(event)))


class LogBase(DeclarativeBase):
    pass


class UsageEvent(LogBase):
    __tablename__ = "dp_usage_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[str] = mapped_column(String(48), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    dataset_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON)


def _normalized_timestamp(value):
    if not value:
        return ""
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


class UsageLogStore:
    def __init__(self, url: str):
        options = (
            {"connect_args": {"timeout": 3}}
            if url.startswith("sqlite")
            else {
                "pool_size": 2,
                "max_overflow": 0,
                "pool_timeout": 2,
                "connect_args": {"connect_timeout": 3, "read_timeout": 3, "write_timeout": 3},
            }
        )
        self.engine = create_engine(url, pool_pre_ping=True, **options)
        from lerobot.data_platform.environment import verify_database_environment

        verify_database_environment(self.engine, "logs")
        self.sessions = sessionmaker(self.engine)
        self.initialized = False

    def ensure_schema(self, *, initialize=False):
        if not self.initialized:
            from sqlalchemy import inspect

            from lerobot.data_platform.environment import EnvironmentIdentity

            if EnvironmentIdentity.from_env() is not None and not initialize:
                if not all(inspect(self.engine).has_table(name) for name in LogBase.metadata.tables):
                    raise RuntimeError(
                        "Initialize the log schema explicitly before starting this environment"
                    )
            else:
                LogBase.metadata.create_all(self.engine)
            self.initialized = True

    def append(self, payload: dict):
        self.ensure_schema()
        payload = sanitize_for_log(payload)
        from sqlalchemy.exc import IntegrityError

        try:
            with self.sessions.begin() as session:
                if session.get(UsageEvent, payload["event_id"]):
                    return
                details = payload.get("details") or {}
                session.add(
                    UsageEvent(
                        event_id=payload["event_id"],
                        timestamp=_normalized_timestamp(payload.get("timestamp") or payload.get("time")),
                        user_id=(payload.get("actor") or {}).get("user_id"),
                        operation=str(payload.get("operation") or payload.get("op") or "unknown")[:128],
                        status=str(payload.get("status") or "unknown")[:32],
                        job_id=details.get("job_id"),
                        dataset_key=payload.get("dataset_key"),
                        payload=payload,
                    )
                )
        except IntegrityError:
            # Another delivery worker can have inserted the same immutable event.
            with self.sessions() as session:
                if session.get(UsageEvent, payload["event_id"]) is None:
                    raise

    def query(self, filters: dict, *, limit=50, offset=0):
        self.ensure_schema()
        statement = select(UsageEvent)
        for key in ("user_id", "operation", "status", "job_id", "dataset_key"):
            if filters.get(key):
                statement = statement.where(getattr(UsageEvent, key) == str(filters[key]))
        if filters.get("since"):
            statement = statement.where(UsageEvent.timestamp >= _normalized_timestamp(filters["since"]))
        if filters.get("until"):
            statement = statement.where(UsageEvent.timestamp <= _normalized_timestamp(filters["until"]))
        with self.sessions() as session:
            rows = session.scalars(
                statement.order_by(UsageEvent.timestamp.desc(), UsageEvent.event_id)
                .offset(offset)
                .limit(limit + 1)
            ).all()
            return {"events": [row.payload for row in rows[:limit]], "has_more": len(rows) > limit}

    def summary(self, filters: dict):
        self.ensure_schema()
        predicates = [UsageEvent.status != "started"]
        for key in ("user_id", "operation", "status", "job_id", "dataset_key"):
            if filters.get(key):
                predicates.append(getattr(UsageEvent, key) == str(filters[key]))
        if filters.get("since"):
            predicates.append(UsageEvent.timestamp >= _normalized_timestamp(filters["since"]))
        if filters.get("until"):
            predicates.append(UsageEvent.timestamp <= _normalized_timestamp(filters["until"]))
        grouped = (
            select(UsageEvent.operation, UsageEvent.status, func.count())
            .where(*predicates)
            .group_by(UsageEvent.operation, UsageEvent.status)
        )
        metrics = select(
            func.count(),
            func.count(func.distinct(UsageEvent.user_id)),
            func.avg(UsageEvent.payload["details"]["duration_ms"].as_float()),
        ).where(*predicates)
        with self.sessions() as session:
            total, users, duration = session.execute(metrics).one()
            return {
                "event_count": total,
                "active_users": users,
                "average_request_ms": duration,
                "counts": [
                    {"operation": op, "status": status, "count": count}
                    for op, status, count in session.execute(grouped)
                ],
            }


class EventSpool:
    """A bounded, durable queue outside dataset roots; one atomically replaced file per event."""

    def __init__(self, root: Path, max_bytes: int = 256 * 1024 * 1024):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes

    def write(self, event: dict, *, reserve_result=False, release_reservation=None):
        import fcntl

        event = sanitize_for_log(event)
        data = json.dumps(event, ensure_ascii=False).encode()
        if len(data) > 65536:
            event["details"] = {"truncated": True, "job_id": (event.get("details") or {}).get("job_id")}
            data = json.dumps(event, ensure_ascii=False).encode()
        with (self.root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            used = 0
            for path in self.root.iterdir():
                if path.suffix not in {".json", ".reserve"}:
                    continue
                with contextlib.suppress(FileNotFoundError):
                    used += path.stat().st_size
            reservation = (
                self.root / f"{uuid.uuid5(uuid.NAMESPACE_URL, str(release_reservation))}.reserve"
                if release_reservation
                else None
            )
            released = reservation.stat().st_size if reservation and reservation.exists() else 0
            target = self.root / f"{uuid.uuid5(uuid.NAMESPACE_URL, str(event['event_id']))}.json"
            existing = target.stat().st_size if target.exists() else 0
            if used - released - existing + len(data) + (65536 if reserve_result else 0) > self.max_bytes:
                raise RuntimeError("Audit spool is full; restore log delivery")
            if reserve_result:
                reserved = target.with_suffix(".reserve")
                with reserved.open("wb") as handle:
                    handle.write(b"0" * 65536)
                    handle.flush()
                    os.fsync(handle.fileno())
            temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                if reservation:
                    reservation.unlink(missing_ok=True)
                fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                temporary.unlink(missing_ok=True)


class LogDelivery:
    def __init__(self, control, logs: UsageLogStore, spool: EventSpool, lifecycle=None):
        self.control, self.logs, self.spool, self.lifecycle = control, logs, spool, lifecycle
        self.last_success = None
        self.error = None
        self.stop = threading.Event()

    def flush(self):
        try:
            with self.control.sessions() as session:
                rows = session.scalars(select(AuditOutbox).order_by(AuditOutbox.created_at).limit(200)).all()
                for row in rows:
                    self.logs.append(row.payload)
                    with self.control.sessions.begin() as transaction:
                        transaction.execute(
                            AuditOutbox.__table__.delete().where(AuditOutbox.event_id == row.event_id)
                        )
            if self.lifecycle:
                for event in self.lifecycle.pending_events(200):
                    self.logs.append(event)
                    self.lifecycle.ack_event(event["event_id"])
            for path in sorted(self.spool.root.glob("*.json"))[:200]:
                try:
                    payload = json.loads(path.read_text())
                except FileNotFoundError:
                    continue
                self.logs.append(payload)
                path.unlink(missing_ok=True)
            self.last_success = datetime.now(timezone.utc).isoformat()
            self.error = None
        except Exception:
            # Database exceptions often contain connection details or SQL parameters.
            self.error = "Log delivery unavailable; events remain queued"

    def status(self):
        with self.control.sessions() as session:
            count = session.scalar(select(func.count()).select_from(AuditOutbox))
        files = list(self.spool.root.glob("*.json"))
        reserves = list(self.spool.root.glob("*.reserve"))
        size = sum(p.stat().st_size for p in files + reserves if p.exists())
        return {
            "pending_control_events": count,
            "pending_spool_events": len(files),
            "pending_request_results": len(reserves),
            "spool_bytes": size,
            "spool_limit_bytes": self.spool.max_bytes,
            "warning": size >= self.spool.max_bytes * 0.8,
            "last_success": self.last_success,
            "error": self.error,
        }

    def start(self):
        def run():
            while not self.stop.is_set():
                self.flush()
                self.stop.wait(2)

        threading.Thread(target=run, name="data-platform-log-delivery", daemon=True).start()

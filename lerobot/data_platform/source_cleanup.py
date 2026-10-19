"""Authoritative missing-source reconciliation without deleting dataset contents or history."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import JSON, Boolean, Float, String, Text, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from lerobot.data_platform.job_management import scheduler_lock
from lerobot.data_platform.management_storage import JobControl, ManagementBase, enqueue_event
from lerobot.data_platform.operation_log import build_operation_event, sanitize_for_log


class SourceCleanup(ManagementBase):
    __tablename__ = "dp_source_cleanups"
    location_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(36), index=True)
    token: Mapped[str] = mapped_column(String(36), unique=True)
    root: Mapped[str] = mapped_column(Text)
    revision: Mapped[str] = mapped_column(String(64))
    witness: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), index=True)
    first_seen: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    node_done: Mapped[bool] = mapped_column(Boolean, default=False)
    job_cursor: Mapped[str] = mapped_column(String(36), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)


@dataclass(frozen=True)
class CleanupTask:
    location_id: str
    token: str
    root: str
    output_dir: str
    dataset_key: str
    node_done: bool
    shared_cache: bool
    protected_roots: tuple[str, ...]
    witness: dict

    def to_dict(self):
        return asdict(self)


def references_location(job, location_id: str) -> bool:
    options = job.options or {}
    return (
        job.location_id == location_id
        or location_id in options.get("source_location_ids", [])
        or any(item.get("location_id") == location_id for item in options.get("_source_locations", []))
    )


class SourceCleanupManager:
    """Persist proof and cleanup progress; filesystem work happens outside DB transactions."""

    def __init__(self, store, cache_root: Path):
        self.store, self.cache_root = store, Path(cache_root)

    @staticmethod
    def _audit(session, location, status, **details):
        enqueue_event(
            session,
            build_operation_event(
                "dataset.source_cleanup",
                status=status,
                source="agent",
                actor={"node_id": location.node_id},
                details={"location_id": location.location_id, "root": location.root, **details},
            ),
        )

    def _aliases(self, session, location):
        from lerobot.data_platform.control_plane import ControlPlaneNode, DatasetLocation

        prefix = str(self.cache_root / location.location_id).rstrip("/") + "/"
        return session.scalars(
            select(DatasetLocation)
            .join(ControlPlaneNode)
            .where(
                ControlPlaneNode.name.like("local-%"),
                DatasetLocation.root == prefix + "remote_source",
                DatasetLocation.location_id != location.location_id,
            )
        ).all()

    def _busy(self, session, locations):
        from lerobot.data_platform.control_plane import RemoteJob

        rows = session.execute(
            select(RemoteJob.location_id, RemoteJob.options)
            .outerjoin(JobControl, JobControl.job_id == RemoteJob.job_id)
            .where(
                or_(
                    RemoteJob.status.in_(["running", "cancel_requested", "interrupted"]),
                    JobControl.stop_confirmed.is_(False),
                )
            )
            .limit(1001)
        ).all()
        return len(rows) > 1000 or any(
            references_location(job, item.location_id) for job in rows for item in locations
        )

    def poll(self, node, cursor=""):
        from lerobot.data_platform.control_plane import DatasetLocation, _iso
        from lerobot.data_platform.maintenance import is_maintenance

        if not isinstance(cursor, str) or len(cursor) > 36:
            raise ValueError("Invalid source-check cursor")
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            if is_maintenance(session):
                return {"checks": [], "next_cursor": cursor, "task": None}
            rows = session.execute(
                select(
                    DatasetLocation.location_id,
                    DatasetLocation.root,
                    DatasetLocation.updated_at,
                    SourceCleanup.status,
                )
                .outerjoin(SourceCleanup, SourceCleanup.location_id == DatasetLocation.location_id)
                .where(
                    DatasetLocation.node_id == node["node_id"],
                    DatasetLocation.state == "available",
                    DatasetLocation.location_id > cursor,
                    ~DatasetLocation.root.startswith(str(self.cache_root) + "/", autoescape=True)
                    if node["name"].startswith("local-")
                    else True,
                )
                .order_by(DatasetLocation.location_id)
                .limit(50)
            ).all()
            checks = [
                {
                    "location_id": key,
                    "root": root,
                    "revision": _iso(revision),
                    "observed": status == "observed",
                }
                for key, root, revision, status in rows
            ]
            task = None
            pending = session.scalars(
                select(SourceCleanup)
                .where(
                    SourceCleanup.node_id == node["node_id"],
                    SourceCleanup.status.in_(["waiting", "node", "server"]),
                )
                .order_by(SourceCleanup.updated_at)
                .limit(10)
            ).all()
            for record in pending:
                location = session.get(DatasetLocation, record.location_id)
                if not location or location.state != "deleting":
                    continue
                aliases = self._aliases(session, location)
                if self._busy(session, [location, *aliases]):
                    record.updated_at = time.time()
                    continue
                record.status = "server" if record.node_done else "node"
                # A cache can never be removed while another registered source uses it.
                other = session.execute(
                    select(DatasetLocation.root, DatasetLocation.output_dir)
                    .where(
                        DatasetLocation.node_id == node["node_id"],
                        DatasetLocation.state == "available",
                        DatasetLocation.location_id != location.location_id,
                    )
                    .limit(1001)
                ).all()
                out = PurePosixPath(location.output_dir)
                shared = len(other) > 1000 or any(
                    PurePosixPath(root).is_relative_to(out)
                    or out.is_relative_to(PurePosixPath(root))
                    or out == PurePosixPath(cache)
                    for root, cache in other
                    if root and cache
                )
                protected = tuple(root for root, _ in other[:1000])
                task = CleanupTask(
                    location.location_id,
                    record.token,
                    record.root,
                    location.output_dir,
                    location.dataset_key,
                    record.node_done,
                    shared,
                    protected,
                    record.witness,
                ).to_dict()
                break
            return {"checks": checks, "next_cursor": rows[-1][0] if len(rows) == 50 else "", "task": task}

    def report(self, node, body):
        from lerobot.data_platform.control_plane import DatasetLocation, RemoteJob, _iso, _utcnow
        from lerobot.data_platform.maintenance import is_maintenance

        if body.get("status") not in {"missing", "present"}:
            raise ValueError("Invalid presence result")
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            if is_maintenance(session):
                return {"status": "maintenance"}
            location = session.get(DatasetLocation, str(body.get("location_id") or ""))
            if not location or location.node_id != node["node_id"] or location.root != body.get("root"):
                raise KeyError("Source location not found for this Agent")
            record = session.get(SourceCleanup, location.location_id)
            if body["status"] == "present":
                if record and record.status not in {"done", "restored"}:
                    if body.get("token") and body["token"] != record.token:
                        raise ValueError("Stale cleanup token")
                    record.status, record.updated_at = "restored", time.time()
                    record.lease_until, record.lease_owner = 0, None
                    for item in [location, *self._aliases(session, location)]:
                        item.state = "available"
                    metadata = dict(location.details or {})
                    for key in ("viewer_ready", "viewer_url", "cache_root"):
                        metadata.pop(key, None)
                    location.details = metadata
                    self._audit(session, location, "restored")
                return {"status": "present"}
            if location.state != "available":
                return {"status": record.status if record else location.state}
            if body.get("revision") != _iso(location.updated_at):
                raise ValueError("Source registration changed; check it again")
            witness = body.get("witness")
            if not isinstance(witness, dict) or set(witness) != {"anchor", "device", "inode"}:
                raise ValueError("A readable parent witness is required")
            root, anchor = PurePosixPath(location.root), PurePosixPath(str(witness["anchor"]))
            if (
                not root.is_absolute()
                or not anchor.is_absolute()
                or ".." in anchor.parts
                or anchor == PurePosixPath("/")
                or anchor == root
                or not root.is_relative_to(anchor)
            ):
                raise ValueError("Invalid parent witness")
            if not any(root.is_relative_to(PurePosixPath(path)) for path in node["allowed_roots"]):
                raise PermissionError("Source is outside the Agent's read roots")
            if any(type(witness[name]) is not int or witness[name] < 0 for name in ("device", "inode")):
                raise ValueError("Invalid filesystem witness")
            now = time.time()
            if record is None:
                record = SourceCleanup(
                    location_id=location.location_id,
                    node_id=node["node_id"],
                    token=str(uuid.uuid4()),
                    root=location.root,
                    revision=_iso(location.updated_at),
                    witness=witness,
                    status="observed",
                    first_seen=now,
                    updated_at=now,
                    node_done=False,
                    job_cursor="",
                    lease_until=0,
                )
                session.add(record)
                return {"status": "observed"}
            if (
                record.status != "observed"
                or record.revision != body["revision"]
                or record.witness != witness
            ):
                record.token, record.root, record.revision = (
                    str(uuid.uuid4()),
                    location.root,
                    body["revision"],
                )
                record.witness, record.status, record.first_seen = witness, "observed", now
                record.node_done, record.job_cursor = False, ""
                record.lease_until, record.lease_owner = 0, None
                return {"status": "observed"}
            if now - record.first_seen < 5:
                return {"status": "observed"}
            record.status, record.updated_at = "waiting", now
            affected = [location, *self._aliases(session, location)]
            for item in affected:
                item.state = "deleting"
                item.updated_at = _utcnow()
            queued = session.scalars(select(RemoteJob).where(RemoteJob.status == "queued")).all()
            for job in queued:
                if any(references_location(job, item.location_id) for item in affected):
                    control = self.store.job_manager.control(session, job)
                    job.status, job.error, job.finished_at = (
                        "cancelled",
                        "Source dataset was deleted",
                        _utcnow(),
                    )
                    control.phase, control.stop_confirmed = "cancelled", True
                    control.revision += 1
                    self.store.job_manager.audit(
                        session,
                        job,
                        "job.cancelled",
                        actor={"node_id": node["node_id"]},
                        details={"reason": "source_deleted"},
                    )
            self._audit(session, location, "confirmed", token=record.token)
            return {"status": "waiting"}

    def task(self, node_id, token):
        from lerobot.data_platform.control_plane import DatasetLocation

        with self.store.sessions() as session:
            record = session.scalar(
                select(SourceCleanup).where(SourceCleanup.token == token, SourceCleanup.node_id == node_id)
            )
            if not record or record.status not in {"node", "server", "done"}:
                raise KeyError("Cleanup task not found")
            location = session.get(DatasetLocation, record.location_id)
            return record, self.store._location_dict(location)

    def jobs(self, node_id, token, cursor=""):
        from lerobot.data_platform.control_plane import DatasetLocation, RemoteJob

        if not isinstance(cursor, str) or len(cursor) > 36:
            raise ValueError("Invalid cleanup Job cursor")
        record, _ = self.task(node_id, token)
        with self.store.sessions() as session:
            location = session.get(DatasetLocation, record.location_id)
            location_ids = [
                record.location_id,
                *(item.location_id for item in self._aliases(session, location)),
            ]
            rows = session.scalars(
                select(RemoteJob.job_id)
                .where(RemoteJob.location_id.in_(location_ids), RemoteJob.job_id > cursor)
                .order_by(RemoteJob.job_id)
                .limit(50)
            ).all()
            return {"job_ids": rows, "next_cursor": rows[-1] if len(rows) == 50 else ""}

    def error(self, node_id, token, error):
        with self.store.sessions.begin() as session:
            record = session.scalar(
                select(SourceCleanup).where(SourceCleanup.node_id == node_id, SourceCleanup.token == token)
            )
            if not record:
                raise KeyError("Cleanup task not found")
            if record.status not in {"node", "server", "waiting"}:
                return
            record.lease_until, record.lease_owner = 0, None
            record.error = str(sanitize_for_log(str(error)))[:1000]
            record.updated_at = time.time()

    def begin_ack(self, node_id, token):
        from lerobot.data_platform.control_plane import DatasetLocation
        from lerobot.data_platform.maintenance import is_maintenance

        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            if is_maintenance(session):
                return {"status": "maintenance"}
            record = session.scalar(
                select(SourceCleanup).where(SourceCleanup.token == token, SourceCleanup.node_id == node_id)
            )
            if not record:
                raise KeyError("Cleanup task not found")
            if record.status == "done":
                return None
            location = session.get(DatasetLocation, record.location_id)
            if location.state != "deleting" or record.status not in {"node", "server"}:
                raise ValueError("Cleanup no longer applies")
            if self._busy(session, [location, *self._aliases(session, location)]):
                return {"status": "waiting"}
            if record.lease_until > time.time():
                return {"status": "busy"}
            owner = str(uuid.uuid4())
            record.lease_owner, record.lease_until = owner, time.time() + 300
            record.node_done, record.status = True, "server"
            protected = session.scalars(
                select(DatasetLocation.root)
                .where(
                    DatasetLocation.state == "available",
                    DatasetLocation.root.startswith(
                        str(self.cache_root / location.location_id) + "/", autoescape=True
                    ),
                )
                .limit(1)
            ).all()
            if protected:
                raise ValueError("Remote cache contains an independent registered dataset")
            return {
                "owner": owner,
                "location": self.store._location_dict(location),
                "cursor": record.job_cursor,
            }

    def finish_ack(self, node_id, token, owner, cursor):
        from lerobot.data_platform.control_plane import DatasetLocation

        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            record = session.scalar(
                select(SourceCleanup).where(SourceCleanup.node_id == node_id, SourceCleanup.token == token)
            )
            if not record or record.lease_owner != owner or record.status != "server":
                raise ValueError("Cleanup lease changed")
            record.lease_until, record.lease_owner, record.error = 0, None, None
            record.job_cursor, record.updated_at = cursor, time.time()
            if cursor:
                return {"status": "server"}
            location = session.get(DatasetLocation, record.location_id)
            for item in [location, *self._aliases(session, location)]:
                item.state = "deleted"
                item.details = {"source_deleted": True, "source_cleanup_token": token}
            record.status = "done"
            self._audit(session, location, "done", token=token)
            return {"status": "done"}

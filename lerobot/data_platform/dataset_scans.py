"""Durable, node-scoped dataset discovery requests independent of compute Jobs."""

import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from sqlalchemy import select

from lerobot.data_platform.management_storage import DatasetScan, enqueue_event
from lerobot.data_platform.operation_log import build_operation_event, sanitize_for_log


@dataclass(frozen=True)
class DatasetScanRequest:
    request_id: str
    node_id: str
    requested_by: str
    path: str
    status: str
    dataset_count: int
    error: str | None
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: DatasetScan) -> "DatasetScanRequest":
        return cls(**{field: getattr(row, field) for field in cls.__dataclass_fields__})


def scan_path(value: str, allowed_roots: list[str]) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("A scan path is required")
    path = PurePosixPath(value.strip())
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Scan path must be absolute and cannot contain '..'")
    if not any(path.is_relative_to(PurePosixPath(root)) for root in allowed_roots):
        raise PermissionError("Scan path must be inside this Agent's allowed roots")
    return str(path)


class DatasetScanManager:
    def __init__(self, store):
        self.store = store

    def _authorize(self, session, actor_id):
        from lerobot.data_platform.control_plane import ControlPlaneUser

        user = session.get(ControlPlaneUser, actor_id)
        if not user or not user.active or user.role not in {"admin", "data_manager", "operator"}:
            raise PermissionError("An admin, data manager or operator account is required")
        if user.role == "operator" and not self.store.all_mutation_locations(session, actor_id):
            raise PermissionError("Dataset discovery requires unrestricted dataset access")
        return user

    def can_request(self, actor_id) -> bool:
        with self.store.sessions() as session:
            try:
                self._authorize(session, actor_id)
            except PermissionError:
                return False
            return True

    @staticmethod
    def _audit(session, record, status):
        enqueue_event(
            session,
            build_operation_event(
                "dataset.scan",
                status=status,
                source="control-plane",
                actor={"user_id": record.requested_by},
                details=asdict(record),
            ),
        )

    def create(self, node_id, actor_id, path) -> DatasetScanRequest:
        from lerobot.data_platform.control_plane import ControlPlaneNode

        with self.store.sessions.begin() as session:
            self._authorize(session, actor_id)
            node = session.scalar(
                select(ControlPlaneNode).where(ControlPlaneNode.node_id == node_id).with_for_update()
            )
            if node is None:
                raise KeyError("Agent not found")
            path = scan_path(path, node.allowed_roots)
            if node.capabilities.get("dataset_scan_protocol") != 1:
                raise RuntimeError("Upgrade this Agent to support immediate dataset scans")
            if self.store._node_dict(node)["status"] != "online":
                raise RuntimeError("Agent is offline; immediate scanning is unavailable")
            pending = session.scalar(
                select(DatasetScan).where(
                    DatasetScan.node_id == node_id, DatasetScan.status.in_(["queued", "running"])
                )
            )
            if pending:
                if pending.requested_by == actor_id and pending.path == path:
                    return DatasetScanRequest.from_row(pending)
                raise RuntimeError("This Agent already has a dataset scan in progress")
            row = DatasetScan(
                request_id=str(uuid.uuid4()),
                node_id=node_id,
                active_node_id=node_id,
                requested_by=actor_id,
                path=path,
                status="queued",
                dataset_count=0,
                created_at=time.time(),
                updated_at=time.time(),
            )
            session.add(row)
            session.flush()
            record = DatasetScanRequest.from_row(row)
            self._audit(session, record, "submitted")
            return record

    def get(self, request_id, actor_id) -> DatasetScanRequest:
        with self.store.sessions() as session:
            user = self._authorize(session, actor_id)
            row = session.get(DatasetScan, request_id)
            if row is None or (user.role != "admin" and row.requested_by != actor_id):
                raise KeyError("Scan request not found")
            return DatasetScanRequest.from_row(row)

    def pending(self, node_id) -> DatasetScanRequest | None:
        with self.store.sessions.begin() as session:
            row = session.scalar(
                select(DatasetScan)
                .where(DatasetScan.node_id == node_id, DatasetScan.status.in_(["queued", "running"]))
                .with_for_update()
            )
            if row is None:
                return None
            if row.status == "queued":
                try:
                    self._authorize(session, row.requested_by)
                except PermissionError as exc:
                    row.status, row.error = "error", str(exc)
                    row.active_node_id = None
                    row.updated_at = time.time()
                    self._audit(session, DatasetScanRequest.from_row(row), "error")
                    return None
            return DatasetScanRequest.from_row(row)

    def update(self, node_id, request_id, *, status, dataset_count=0, error=None) -> DatasetScanRequest:
        if not isinstance(status, str) or status not in {"running", "done", "error"}:
            raise ValueError("Invalid scan status")
        if not isinstance(dataset_count, int) or isinstance(dataset_count, bool) or dataset_count < 0:
            raise ValueError("Invalid dataset count")
        with self.store.sessions.begin() as session:
            row = session.scalar(
                select(DatasetScan).where(DatasetScan.request_id == request_id).with_for_update()
            )
            if row is None or row.node_id != node_id:
                raise KeyError("Scan request not found for this Agent")
            if row.status in {"done", "error"}:
                return DatasetScanRequest.from_row(row)
            if row.status == "queued" and status == "running":
                self._authorize(session, row.requested_by)
            row.status, row.updated_at = status, time.time()
            if status in {"done", "error"}:
                row.active_node_id = None
            row.dataset_count = dataset_count
            row.error = str(sanitize_for_log(str(error)))[:1000] if error else None
            record = DatasetScanRequest.from_row(row)
            if status in {"done", "error"}:
                self._audit(session, record, status)
            return record

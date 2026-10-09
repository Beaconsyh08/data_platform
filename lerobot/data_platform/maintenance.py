"""Durable maintenance gate shared by Web requests and transactional scheduling."""

from dataclasses import dataclass

from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from lerobot.data_platform.management_storage import ManagementBase, enqueue_event
from lerobot.data_platform.operation_log import build_operation_event


class MaintenanceState(ManagementBase):
    __tablename__ = "dp_maintenance"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    release: Mapped[str] = mapped_column(String(128), default="")


class MaintenanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveJobCounts:
    running: int = 0
    cancel_requested: int = 0
    interrupted: int = 0

    @property
    def total(self) -> int:
        return self.running + self.cancel_requested + self.interrupted

    @property
    def summary(self) -> str:
        return (
            f"{self.running} running or finalizing, {self.cancel_requested} cancelling, "
            f"{self.interrupted} interrupted"
        )


class JobDrainError(MaintenanceError):
    def __init__(self, code: str, counts: ActiveJobCounts):
        self.code = code
        self.counts = counts
        message = (
            "Reconcile interrupted jobs before upgrading"
            if code == "interrupted_jobs"
            else "Timed out draining jobs"
        )
        super().__init__(f"{message}: {counts.summary}; maintenance remains enabled")


def is_maintenance(session) -> bool:
    row = session.get(MaintenanceState, 1)
    return bool(row and row.enabled)


def set_maintenance(store, enabled: bool, *, release: str = "") -> None:
    from lerobot.data_platform.job_management import scheduler_lock

    with store.sessions.begin() as session:
        scheduler_lock(session)
        row = session.get(MaintenanceState, 1)
        if row is None:
            row = MaintenanceState(id=1)
            session.add(row)
        row.enabled = enabled
        row.release = release
        enqueue_event(
            session,
            build_operation_event(
                "deployment.maintenance",
                status="done",
                source="deployment",
                details={"enabled": enabled, "release": release},
            ),
        )


def active_job_counts(store) -> ActiveJobCounts:
    from sqlalchemy import func

    from lerobot.data_platform.control_plane import RemoteJob
    from lerobot.data_platform.job_management import ACTIVE_STATES

    with store.sessions() as session:
        counts = dict(
            session.execute(
                select(RemoteJob.status, func.count())
                .where(RemoteJob.status.in_(ACTIVE_STATES))
                .group_by(RemoteJob.status)
            ).all()
        )
    return ActiveJobCounts(**counts)


def active_jobs(store) -> int:
    return active_job_counts(store).total


def wait_until_idle(store, *, timeout: float = 300):
    import time

    deadline = time.monotonic() + timeout
    while (counts := active_job_counts(store)).total:
        if counts.interrupted:
            raise JobDrainError("interrupted_jobs", counts)
        if time.monotonic() >= deadline:
            raise JobDrainError("drain_timeout", counts)
        time.sleep(1)

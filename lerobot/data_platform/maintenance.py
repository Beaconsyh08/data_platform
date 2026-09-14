"""Durable maintenance gate shared by Web requests and transactional scheduling."""

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


def active_jobs(store) -> int:
    from sqlalchemy import func

    from lerobot.data_platform.control_plane import RemoteJob
    from lerobot.data_platform.job_management import ACTIVE_STATES

    with store.sessions() as session:
        return session.scalar(
            select(func.count()).select_from(RemoteJob).where(RemoteJob.status.in_(ACTIVE_STATES))
        )


def wait_until_idle(store, *, timeout: float = 300):
    import time

    from lerobot.data_platform.control_plane import RemoteJob

    deadline = time.monotonic() + timeout
    while active_jobs(store):
        with store.sessions() as session:
            if session.scalar(select(RemoteJob.job_id).where(RemoteJob.status == "interrupted").limit(1)):
                raise MaintenanceError(
                    "Reconcile interrupted jobs before upgrading; maintenance remains enabled"
                )
        if time.monotonic() >= deadline:
            raise MaintenanceError(
                "Timed out draining jobs; maintenance remains enabled. Rerun after jobs finish"
            )
        time.sleep(1)

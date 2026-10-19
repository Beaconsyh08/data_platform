"""Restartable Server A coordinator; computations remain existing Agent jobs."""

import logging
import os
import signal
import threading
from pathlib import Path

from sqlalchemy import select

from lerobot.data_platform.processing.models import PipelineRun


def configured_manager():
    """Open shared durable state without constructing Web registries, caches or background readers."""
    from lerobot.data_platform.control_plane import ControlPlaneStore
    from lerobot.data_platform.environment import check_server_environment
    from lerobot.data_platform.lifecycle import LifecycleStore
    from lerobot.data_platform.processing.service import PipelineManager

    check_server_environment()
    database = os.environ.get("DATA_PLATFORM_DATABASE_URL")
    output = os.environ.get("DATA_PLATFORM_OUTPUT_DIR")
    if not output and os.environ.get("DATA_PLATFORM_ROOT"):
        output = str(Path(os.environ["DATA_PLATFORM_ROOT"]) / "vis/_console")
    if not database or not output:
        raise RuntimeError("Pipeline coordination requires the control database and console output directory")
    store = ControlPlaneStore(database, initialize_schema=False)
    return PipelineManager(store, LifecycleStore(Path(output) / "static/lifecycle"))


def main():
    manager = configured_manager()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    page = 0
    release_root = Path(__file__).resolve().parents[3]
    current = os.environ.get("DATA_PLATFORM_COORDINATOR_CURRENT")
    while not stop.is_set():
        if current and Path(current).resolve() != release_root:
            return
        try:
            with manager.store.sessions() as session:
                rows = session.scalars(
                    select(PipelineRun.run_id)
                    .where(
                        PipelineRun.status.in_(
                            ["queued", "running", "waiting_review", "cancel_requested", "retry_requested"]
                        )
                    )
                    .order_by(PipelineRun.run_id)
                    .offset(page)
                    .limit(200)
                ).all()
            page = page + len(rows) if len(rows) == 200 else 0
            for run_id in rows:
                if stop.is_set():
                    break
                try:
                    manager.tick(run_id)
                except Exception:
                    logging.exception("Pipeline %s will be reconciled again", run_id)
        except Exception:
            logging.exception("Pipeline reconciliation failed; durable state will be retried")
        stop.wait(2)


if __name__ == "__main__":
    main()

"""Independent experience worker. Slow providers never occupy an interactive Runner slot."""

from __future__ import annotations

import logging
import signal
import threading
import time
import uuid

from sqlalchemy import and_, select

from lerobot.data_platform.ai_tools import experience
from lerobot.data_platform.ai_tools.hindsight import Backend, Config, verify
from lerobot.data_platform.ai_tools.models import AssistantExperience, AssistantFollowup
from lerobot.data_platform.job_management import scheduler_lock

BUDGET_SECONDS = 240
LEASE_SECONDS = 300


def discover(service):
    """Bounded startup backfill of existing authorized completion records, not arbitrary old jobs."""
    with service.store.sessions.begin() as session:
        scheduler_lock(session)
        rows = session.execute(
            select(AssistantFollowup.owner, AssistantFollowup.location_id)
            .outerjoin(
                AssistantExperience,
                and_(
                    AssistantExperience.owner == AssistantFollowup.owner,
                    AssistantExperience.location_id == AssistantFollowup.location_id,
                ),
            )
            .where(AssistantFollowup.status == "ready", AssistantExperience.scope_id.is_(None))
            .distinct()
            .limit(20)
        ).all()
        for owner, location in rows:
            experience.enqueue(session, owner, location)


def tick(service, *, backend_factory=Backend, verifier=verify, config=None):
    if not service.config.enabled or not experience.enabled():
        return False
    discover(service)
    now, token = time.time(), str(uuid.uuid4())
    with service.store.sessions.begin() as session:
        scheduler_lock(session)
        row = session.scalar(
            select(AssistantExperience)
            .where(AssistantExperience.next_check_at <= now, AssistantExperience.lease_until <= now)
            .order_by(AssistantExperience.next_check_at, AssistantExperience.scope_id)
            .limit(1)
        )
        if row is None:
            return False
        row.lease_owner, row.lease_until = token, now + LEASE_SECONDS
        identity, owner, location, epoch = row.scope_id, row.owner, row.location_id, row.epoch
        old_bank = row.bank_id
    deadline = time.monotonic() + BUDGET_SECONDS
    backend, observations, current = None, [], None
    failed, cleaned = False, False

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError("Experience budget exhausted")
        with service.store.sessions() as session:
            state = session.get(AssistantExperience, identity)
            if state.lease_owner != token or state.lease_until <= time.time() or state.epoch != epoch:
                raise RuntimeError("Experience lease changed")
            service._target(session, owner, location)

    try:
        configured = config or Config.from_environment()
        if old_bank:
            backend_factory(configured, old_bank, deadline=deadline).delete()
            with service.store.sessions.begin() as session:
                scheduler_lock(session)
                row = session.get(AssistantExperience, identity)
                if row.lease_owner != token:
                    raise RuntimeError("Experience lease changed")
                row.bank_id = ""
        check()
        with service.store.sessions() as session:
            current = experience.snapshot(service, session, owner, location)
        if len(current.sources) >= 2:
            bank = "dp-exp-" + identity[:24] + "-" + token
            with service.store.sessions.begin() as session:
                scheduler_lock(session)
                row = session.get(AssistantExperience, identity)
                if row.lease_owner != token or row.epoch != epoch:
                    raise RuntimeError("Experience lease changed")
                row.bank_id = bank
            backend = backend_factory(configured, bank, deadline=deadline, check=check)
            candidates = backend.observations(current)
            check()
            observations = verifier(candidates, model=service.config.model, deadline=deadline)
            check()
    except Exception:
        failed = True
        # Provider and database exception bodies may contain private text or credentials.
        logging.warning("Experience refresh failed; retry is delayed and interactive requests are unaffected")
    finally:
        if backend is not None:
            try:
                backend.delete()
                cleaned = True
            except Exception:
                failed = True
    with service.store.sessions.begin() as session:
        scheduler_lock(session)
        row = session.get(AssistantExperience, identity)
        if row.lease_owner != token:
            return True
        if cleaned:
            row.bank_id = ""
        row.lease_owner, row.lease_until, row.updated_at = "", 0, int(time.time())
        if row.epoch != epoch:
            row.next_check_at = time.time() + 5
            return True
        try:
            fresh = experience.snapshot(service, session, owner, location)
            unchanged = current is not None and fresh.digest == current.digest
        except (PermissionError, KeyError, ValueError):
            unchanged = False
        if failed or not unchanged:
            row.status, row.observations, row.source_digest = "error", [], ""
            row.failures += 1
            row.next_check_at = time.time() + min(3600, 30 * 2 ** min(row.failures, 7))
        else:
            row.status, row.observations, row.source_digest = "ready", observations, current.digest
            row.notes_revision, row.failures = current.notes_revision, 0
            row.next_check_at = time.time() + 3600
            service.manager._audit(
                session,
                owner,
                "assistant.experience.ready",
                source_count=len(current.sources),
                observation_count=len(observations),
                source_digest=current.digest,
            )
    return True


def main():
    from lerobot.data_platform.ai_tools.service import AssistantService
    from lerobot.data_platform.processing.coordinator import configured_manager

    service = AssistantService(configured_manager())
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            tick(service)
        except Exception:
            logging.warning("Experience storage unavailable; leased work will be retried")
        stop.wait(5)


if __name__ == "__main__":
    main()

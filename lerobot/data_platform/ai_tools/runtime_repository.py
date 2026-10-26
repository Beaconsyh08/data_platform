"""Transactional assistant admission, leases and provider receipts in the control DB."""

import time
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import delete, select, text

from lerobot.data_platform.ai_tools.models import (
    AssistantCheckpoint,
    AssistantCheckpointWrite,
    AssistantExecution,
    AssistantModelCall,
    AssistantTurn,
)
from lerobot.data_platform.job_management import scheduler_lock
from lerobot.data_platform.task_catalog import content_digest

GRAPH_VERSION = "bounded-v1"
STATE_VERSION = 1
LEASE_SECONDS = 30
GENERATION_SECONDS = 75


class LeaseLostError(RuntimeError):
    pass


class OutcomeUnknownError(RuntimeError):
    pass


class ReplayChangedError(RuntimeError):
    pass


class BudgetExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    request_id: str
    worker: str
    epoch: int


def database_time(session):
    if session.get_bind().dialect.name == "mysql":
        return float(session.scalar(text("SELECT UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6))")))
    return time.time()


def expire_execution(session, turn):
    execution = session.get(AssistantExecution, turn.request_id)
    if execution is None:
        return False
    now = database_time(session)
    until = execution.deadline if execution.deadline is not None else execution.queue_until
    if turn.status == "running" and now >= until:
        turn.status = execution.status = "error"
        turn.result = {
            "error": "The assistant request timed out. Start a new request; no task was submitted."
        }
        execution.lease_until = 0
    return True


def delete_executions(session, request_ids):
    """Apply existing conversation retention to its private runtime records too."""
    for model in (AssistantCheckpointWrite, AssistantCheckpoint, AssistantModelCall, AssistantExecution):
        session.execute(delete(model).where(model.request_id.in_(request_ids)))


class RuntimeRepository:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def transaction(self, lease=None):
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            if lease is not None:
                self.require(session, lease)
            yield session
            # Atomic fence: no claim can pass the lock until this transaction commits.
            if lease is not None:
                self.require(session, lease)

    def require(self, session, lease):
        row = session.get(AssistantExecution, lease.request_id)
        turn = session.get(AssistantTurn, lease.request_id)
        if (
            row is None
            or turn is None
            or turn.status != "running"
            or row.status != "running"
            or row.lease_owner != lease.worker
            or row.lease_epoch != lease.epoch
            or row.lease_until <= database_time(session)
            or row.backend != "langgraph"
            or row.graph_version != GRAPH_VERSION
            or row.schema_version != STATE_VERSION
        ):
            raise LeaseLostError("Assistant execution ownership changed")
        return row

    def claim(self, worker):
        # Idle polling must not take the global scheduler lock or write SQLite WAL.
        with self.store.sessions() as session:
            now = database_time(session)
            candidate = session.scalar(
                select(AssistantExecution.request_id)
                .where(
                    AssistantExecution.status.in_(["queued", "running"]),
                    AssistantExecution.backend == "langgraph",
                    AssistantExecution.graph_version == GRAPH_VERSION,
                    AssistantExecution.schema_version == STATE_VERSION,
                    AssistantExecution.lease_until <= now,
                )
                .limit(1)
            )
        if candidate is None:
            return None
        with self.transaction() as session:
            now = database_time(session)
            rows = session.scalars(
                select(AssistantExecution)
                .where(
                    AssistantExecution.status.in_(["queued", "running"]),
                    AssistantExecution.backend == "langgraph",
                    AssistantExecution.graph_version == GRAPH_VERSION,
                    AssistantExecution.schema_version == STATE_VERSION,
                    AssistantExecution.lease_until <= now,
                )
                .order_by(AssistantExecution.created_at, AssistantExecution.request_id)
                .limit(20)
            ).all()
            for row in rows:
                turn = session.get(AssistantTurn, row.request_id)
                if turn is None or turn.status != "running":
                    row.status = "error"
                    continue
                expire_execution(session, turn)
                if turn.status != "running":
                    continue
                row.status, row.lease_owner = "running", worker
                row.lease_epoch += 1
                row.lease_until = now + LEASE_SECONDS
                if row.deadline is None:
                    row.deadline = now + GENERATION_SECONDS
                # Old web workers still recognize this as an active bounded Turn.
                turn.lease_until = int(row.deadline) + LEASE_SECONDS
                return Lease(row.request_id, worker, row.lease_epoch)
        return None

    def renew(self, lease):
        with self.transaction(lease) as session:
            row = self.require(session, lease)
            row.lease_until = database_time(session) + LEASE_SECONDS

    def snapshot(self, lease):
        with self.transaction(lease) as session:
            row = self.require(session, lease)
            return row.owner, row.model, dict(row.payload)

    def check_budget(self, lease):
        with self.transaction(lease) as session:
            row = self.require(session, lease)
            remaining = row.deadline - database_time(session)
            if remaining <= 0:
                raise BudgetExceededError("Assistant generation deadline exceeded")
            return remaining

    def reserve_call(self, lease, index, payload):
        digest = content_digest(payload)
        with self.transaction(lease) as session:
            row = self.require(session, lease)
            if row.deadline <= database_time(session):
                raise BudgetExceededError("Assistant generation deadline exceeded")
            receipt = session.get(AssistantModelCall, (lease.request_id, index))
            if receipt:
                if receipt.request_digest != digest:
                    raise ReplayChangedError("Authorized context changed during recovery")
                if receipt.response is None:
                    raise OutcomeUnknownError("Model response was not durably recorded")
                return receipt.response
            pending = session.scalar(
                select(AssistantModelCall.call_index)
                .where(
                    AssistantModelCall.request_id == lease.request_id,
                    AssistantModelCall.response.is_(None),
                )
                .limit(1)
            )
            if pending is not None:
                raise OutcomeUnknownError("An earlier model response is unknown")
            if row.calls_used >= 8 or index != row.calls_used:
                raise BudgetExceededError("Assistant model call budget exceeded")
            row.calls_used += 1
            session.add(
                AssistantModelCall(request_id=lease.request_id, call_index=index, request_digest=digest)
            )
        return None

    def record_response(self, lease, index, response):
        with self.transaction(lease) as session:
            row = session.get(AssistantModelCall, (lease.request_id, index))
            if row is None or row.response is not None:
                raise ReplayChangedError("Missing or already recorded model response")
            row.response = response

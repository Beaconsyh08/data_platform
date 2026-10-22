"""MySQL execution ownership and model-call receipts for the phase-A experiment.

All transitions lock the execution row. The checkpointer uses the same lock and
transaction boundary, so a replaced worker cannot publish checkpoints or receipts.
These tables are deliberately absent from platform startup migrations.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pymysql import Connection
from pymysql.cursors import DictCursor


class LeaseLostError(RuntimeError):
    """The caller no longer owns this execution."""


class OutcomeUnknownError(RuntimeError):
    """A model call was reserved but has no durable response; never resend it."""


class BudgetExceededError(RuntimeError):
    """The persistent generation budget has been exhausted."""


@dataclass(frozen=True)
class Lease:
    execution_id: str
    worker: str
    epoch: int


def clock(cursor: DictCursor) -> float:
    # Read *after* acquiring the row lock: NOW() in a blocked SELECT can be stale.
    cursor.execute("SELECT UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) AS now")
    return float(cursor.fetchone()["now"])


def lock_execution(cursor: DictCursor, execution_id: str) -> dict[str, Any]:
    cursor.execute("SELECT * FROM dp_ai_executions WHERE execution_id=%s FOR UPDATE", (execution_id,))
    row = cursor.fetchone()
    if row is None:
        raise LeaseLostError("Execution not found")
    return row


def require_lease(cursor: DictCursor, lease: Lease) -> dict[str, Any]:
    row = lock_execution(cursor, lease.execution_id)
    if (
        row["status"] != "running"
        or row["lease_owner"] != lease.worker
        or row["lease_epoch"] != lease.epoch
        or row["lease_until"] <= clock(cursor)
    ):
        raise LeaseLostError("Execution lease expired or was replaced")
    return row


class ExecutionRepository:
    """A connection factory must return a fresh, exclusively owned connection."""

    def __init__(self, connect: Callable[[], Connection]):
        self.connect = connect

    @contextmanager
    def transaction(self) -> Iterator[DictCursor]:
        with self.connect() as connection:
            connection.begin()
            try:
                with connection.cursor(DictCursor) as cursor:
                    yield cursor
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def setup(self) -> None:
        """Explicit experimental provisioning, never invoked on server startup."""
        with self.transaction() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS dp_ai_executions (
                    execution_id VARCHAR(36) PRIMARY KEY,
                    graph_version VARCHAR(32) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'queued',
                    lease_owner VARCHAR(64),
                    lease_epoch BIGINT NOT NULL DEFAULT 0,
                    lease_until DOUBLE NOT NULL DEFAULT 0,
                    deadline DOUBLE,
                    calls_used INTEGER NOT NULL DEFAULT 0,
                    result JSON
                ) ENGINE=InnoDB
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS dp_ai_model_calls (
                    execution_id VARCHAR(36) NOT NULL,
                    call_id VARCHAR(64) NOT NULL,
                    request_digest CHAR(64) NOT NULL,
                    response JSON,
                    PRIMARY KEY (execution_id, call_id)
                ) ENGINE=InnoDB
            """)

    def enqueue(self, execution_id: str, graph_version: str = "s3-v1") -> None:
        with self.transaction() as cursor:
            cursor.execute(
                "INSERT INTO dp_ai_executions (execution_id, graph_version) VALUES (%s, %s)",
                (execution_id, graph_version),
            )

    def claim(
        self, execution_id: str, worker: str, *, seconds: float = 30, graph_version: str = "s3-v1"
    ) -> Lease | None:
        if not 0 < seconds <= 120 or not worker or len(worker) > 64:
            raise ValueError("Invalid lease")
        with self.transaction() as cursor:
            row = lock_execution(cursor, execution_id)
            now = clock(cursor)
            if row["graph_version"] != graph_version:
                raise ValueError("Unsupported graph version")
            if row["status"] not in {"queued", "running"} or row["lease_until"] > now:
                return None
            epoch = row["lease_epoch"] + 1
            cursor.execute(
                "UPDATE dp_ai_executions SET status='running', lease_owner=%s, lease_epoch=%s, "
                "lease_until=%s, deadline=COALESCE(deadline,%s) WHERE execution_id=%s",
                (worker, epoch, now + seconds, now + 75, execution_id),
            )
            return Lease(execution_id, worker, epoch)

    def renew(self, lease: Lease, *, seconds: float = 30) -> None:
        if not 0 < seconds <= 120:
            raise ValueError("Invalid lease duration")
        with self.transaction() as cursor:
            require_lease(cursor, lease)
            cursor.execute(
                "UPDATE dp_ai_executions SET lease_until=%s WHERE execution_id=%s",
                (clock(cursor) + seconds, lease.execution_id),
            )

    def release(self, lease: Lease) -> None:
        """Gracefully relinquish ownership without resetting generation budgets."""
        with self.transaction() as cursor:
            require_lease(cursor, lease)
            cursor.execute(
                "UPDATE dp_ai_executions SET status='queued', lease_until=0 WHERE execution_id=%s",
                (lease.execution_id,),
            )

    def reserve_call(self, lease: Lease, call_id: str, request_digest: str) -> dict | None:
        """None permits one send; a cached response permits replay without a send.

        A pending receipt fails closed even if the process died before sending.
        The experiment uses a conservative 75-second wall deadline, including
        downtime. Waiting for a human or job must use a separate execution later.
        """
        if not call_id or len(call_id) > 64 or len(request_digest) != 64:
            raise ValueError("Invalid call identity")
        with self.transaction() as cursor:
            row = require_lease(cursor, lease)
            cursor.execute(
                "SELECT request_digest, response FROM dp_ai_model_calls WHERE execution_id=%s AND call_id=%s",
                (lease.execution_id, call_id),
            )
            receipt = cursor.fetchone()
            if receipt:
                if receipt["request_digest"] != request_digest:
                    raise ValueError("Model call identity reused with different content")
                if receipt["response"] is None:
                    raise OutcomeUnknownError("Model response was not durably recorded")
                return json.loads(receipt["response"])
            # Any earlier unknown outcome also blocks a fresh call ID.
            cursor.execute(
                "SELECT call_id FROM dp_ai_model_calls WHERE execution_id=%s AND response IS NULL LIMIT 1",
                (lease.execution_id,),
            )
            if cursor.fetchone():
                raise OutcomeUnknownError("An earlier model response is unknown")
            if row["calls_used"] >= 8 or clock(cursor) >= row["deadline"]:
                raise BudgetExceededError("Generation budget exhausted")
            cursor.execute(
                "INSERT INTO dp_ai_model_calls (execution_id, call_id, request_digest) VALUES (%s,%s,%s)",
                (lease.execution_id, call_id, request_digest),
            )
            cursor.execute(
                "UPDATE dp_ai_executions SET calls_used=calls_used+1 WHERE execution_id=%s",
                (lease.execution_id,),
            )
        return None

    def record_response(self, lease: Lease, call_id: str, response: dict) -> None:
        payload = json.dumps(response, ensure_ascii=False)
        if len(payload.encode()) > 262144:
            raise ValueError("Model response exceeds limit")
        with self.transaction() as cursor:
            require_lease(cursor, lease)
            cursor.execute(
                "UPDATE dp_ai_model_calls SET response=%s "
                "WHERE execution_id=%s AND call_id=%s AND response IS NULL",
                (payload, lease.execution_id, call_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Missing or already recorded model call")

    def finish(self, lease: Lease, result: dict, *, failed: bool = False) -> None:
        payload = json.dumps(result, ensure_ascii=False)
        if len(payload.encode()) > 262144:
            raise ValueError("Result exceeds limit")
        with self.transaction() as cursor:
            require_lease(cursor, lease)
            cursor.execute(
                "UPDATE dp_ai_executions SET status=%s, result=%s, lease_until=0 WHERE execution_id=%s",
                ("error" if failed else "completed", payload, lease.execution_id),
            )

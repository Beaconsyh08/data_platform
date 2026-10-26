"""Pinned community saver plus atomic execution fencing for the MySQL experiment.

The private cursor extension is tested against langgraph-checkpoint-mysql 3.0.0.
Upgrading that dependency requires rerunning the MySQL acceptance tests.
"""

import re
from contextlib import contextmanager

from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pymysql.cursors import DictCursor

from experiments.assistant_langgraph.repository import Lease, require_lease

_TABLES = re.compile(r"\b(checkpoint_migrations|checkpoint_blobs|checkpoint_writes|checkpoints)\b")


class NamespacedCursor(DictCursor):
    """Rewrite library-owned SQL identifiers, never bound values or user payloads."""

    def execute(self, query, args=None):
        return super().execute(_TABLES.sub(r"dp_ai_\1", query), args)

    def executemany(self, query, args):
        # PyMySQL's bulk optimization interpolates values before calling execute.
        # Keep values bound separately so identifier rewriting never touches data.
        count = sum(self.execute(query, values) for values in args)
        self.rowcount = count
        return count


class NamespacedMySQLSaver(PyMySQLSaver):
    """Only provisioning code may use this unfenced base class."""

    @staticmethod
    def _get_cursor_from_connection(connection):
        return connection.cursor(NamespacedCursor)


class FencedMySQLSaver(NamespacedMySQLSaver):
    def __init__(self, connection, lease: Lease):
        super().__init__(connection, serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        self.lease = lease

    def _scope(self, config):
        configurable = (config or {}).get("configurable", {})
        if (
            configurable.get("thread_id") != self.lease.execution_id
            or configurable.get("checkpoint_ns", "") != ""
        ):
            raise ValueError("Only this execution's root graph is supported")

    @contextmanager
    def _cursor(self, *, pipeline=False):
        # Same connection + row lock + transaction for ownership and ALL writes,
        # including pending writes. There is no gap between checking and saving.
        with self.lock:
            self.conn.begin()
            try:
                with self._get_cursor_from_connection(self.conn) as cursor:
                    require_lease(cursor, self.lease)
                    yield cursor
                    # A transaction that outlasted its lease must roll back too.
                    require_lease(cursor, self.lease)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def get_tuple(self, config):
        self._scope(config)
        return super().get_tuple(config)

    def put(self, config, checkpoint, metadata, new_versions):
        self._scope(config)
        return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        self._scope(config)
        return super().put_writes(config, writes, task_id, task_path)

    def list(self, config, *, filter=None, before=None, limit=None):
        self._scope(config)
        if before is not None:
            self._scope(before)
        yield from super().list(config, filter=filter, before=before, limit=limit)

    def setup(self):
        raise NotImplementedError("Provision explicitly before starting workers")

    def delete_thread(self, thread_id):
        raise NotImplementedError("Retention is not a runner capability")

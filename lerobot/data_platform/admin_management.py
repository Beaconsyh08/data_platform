"""Bounded read-only database views and durable request audit collection."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from sqlalchemy import create_engine, select

from lerobot.data_platform.management_storage import (
    EventSpool,
    JobAttempt,
    JobControl,
    LogDelivery,
    UsageEvent,
    UsageLogStore,
)
from lerobot.data_platform.operation_log import build_operation_event, sanitize_for_log


class DatabaseBrowser:
    """Fixed projections only; callers never provide identifiers or SQL."""

    def __init__(self, store, logs=None, lifecycle=None):
        from lerobot.data_platform.control_plane import (
            ControlPlaneNode,
            ControlPlaneUser,
            DatasetLocation,
            RemoteJob,
        )

        self.lifecycle = lifecycle
        self.tables = {
            "control.users": (
                store.engine,
                ControlPlaneUser.__table__,
                ("user_id", "username", "display_name", "role", "active", "created_at", "updated_at"),
            ),
            "control.nodes": (
                store.engine,
                ControlPlaneNode.__table__,
                ("node_id", "name", "hostname", "status", "capabilities", "last_seen_at"),
            ),
            "control.locations": (
                store.engine,
                DatasetLocation.__table__,
                ("location_id", "node_id", "dataset_key", "state", "metadata_json"),
            ),
            "control.jobs": (
                store.engine,
                RemoteJob.__table__,
                (
                    "job_id",
                    "node_id",
                    "location_id",
                    "requested_by",
                    "operation",
                    "status",
                    "created_at",
                    "updated_at",
                    "error",
                ),
            ),
            "control.attempts": (
                store.engine,
                JobAttempt.__table__,
                (
                    "attempt_id",
                    "job_id",
                    "attempt_no",
                    "worker_instance_id",
                    "status",
                    "started_at",
                    "finished_at",
                    "error",
                ),
            ),
            "control.queue": (
                store.engine,
                JobControl.__table__,
                (
                    "job_id",
                    "priority",
                    "revision",
                    "phase",
                    "queued_at",
                    "attempt_id",
                    "stop_mode",
                    "stop_confirmed",
                ),
            ),
        }
        if logs:
            self.tables["logs.usage"] = (
                logs.engine,
                UsageEvent.__table__,
                (
                    "event_id",
                    "timestamp",
                    "user_id",
                    "operation",
                    "status",
                    "job_id",
                    "dataset_key",
                    "payload",
                ),
            )
        self.engines = {}
        for engine, _, _ in self.tables.values():
            if engine not in self.engines:
                if engine.url.get_backend_name() == "sqlite":
                    # In-memory test databases must share their original connection.
                    self.engines[engine] = (
                        engine
                        if str(engine.url).endswith(":memory:")
                        else create_engine(
                            engine.url,
                            connect_args={"timeout": 2},
                            pool_size=1,
                            max_overflow=0,
                            pool_timeout=2,
                        )
                    )
                else:
                    self.engines[engine] = create_engine(
                        engine.url,
                        pool_size=1,
                        max_overflow=0,
                        pool_timeout=2,
                        connect_args={"connect_timeout": 3, "read_timeout": 3},
                    )
        self._requests = {}
        self._lock = threading.Lock()

    def rate_limit(self, user_id):
        with self._lock:
            now = time.monotonic()
            for key in list(self._requests):
                if not self._requests[key] or self._requests[key][-1] < now - 60:
                    del self._requests[key]
            history = self._requests.setdefault(user_id, deque())
            while history and history[0] < now - 60:
                history.popleft()
            if len(history) >= 30:
                return False
            history.append(now)
            return True

    def describe(self):
        result = [
            {"key": key, "columns": [{"name": name, "type": str(table.c[name].type)} for name in names]}
            for key, (_, table, names) in self.tables.items()
        ]
        if self.lifecycle:
            result.append(
                {
                    "key": "lifecycle.records",
                    "columns": [
                        {"name": name, "type": "TEXT"}
                        for name in ("kind", "record_id", "payload_json", "created_at", "updated_at")
                    ],
                }
            )
        return result

    def rows(self, key, *, limit=50, offset=0, field=None, value=None):
        if key == "lifecycle.records" and self.lifecycle:
            return self.lifecycle.browse_records(limit=limit, offset=offset, field=field, value=value)
        if key not in self.tables:
            raise KeyError(key)
        source, table, names = self.tables[key]
        if field and (field not in names or field in {"payload", "capabilities", "metadata_json"}):
            raise ValueError("Unsupported filter column")
        statement = select(*(table.c[name] for name in names))
        if field:
            statement = statement.where(table.c[field] == value)
        statement = statement.order_by(*table.primary_key.columns).offset(offset).limit(limit + 1)
        engine = self.engines[source]
        with engine.connect() as conn:
            raw = None
            if engine.dialect.name == "mysql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                statement = statement.prefix_with("/*+ MAX_EXECUTION_TIME(2000) */", dialect="mysql")
            elif engine.dialect.name == "sqlite":
                raw = conn.connection.driver_connection
                deadline = time.monotonic() + 2
                raw.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                conn.exec_driver_sql("PRAGMA query_only=ON")
            try:
                records = [sanitize_for_log(dict(row)) for row in conn.execute(statement).mappings()]
            finally:
                if raw:
                    raw.set_progress_handler(None, 0)
                    conn.exec_driver_sql("PRAGMA query_only=OFF")
        return {"rows": records[:limit], "has_more": len(records) > limit}


def install_usage_audit(app):
    """Install before authentication so denied requests are recorded too."""
    from flask import g, jsonify, request

    @app.before_request
    def usage_begin(*, denied=False):
        spool = app.extensions.get("data_platform_usage_spool")
        path = request.path
        if spool is None or path.startswith(("/api/agents/", "/static/")) or path == "/healthz":
            return None
        is_read = request.method in {"GET", "HEAD", "OPTIONS"}
        if (
            not denied
            and is_read
            and (request.method != "GET" or path.startswith("/api/") and "download" not in path)
        ):
            return None
        if Path(path).suffix.lower() in {
            ".mp4",
            ".m3u8",
            ".ts",
            ".css",
            ".js",
            ".png",
            ".jpg",
            ".ico",
            ".woff2",
        }:
            return None
        actor = {"kind": "anonymous"}
        store = app.extensions.get("data_platform_control")
        if store:
            from lerobot.data_platform.environment import session_cookie_name

            user = store.verify_session(request.cookies.get(session_cookie_name()))
            if user:
                actor = {key: user[key] for key in ("user_id", "username", "role")}
                if user.get("original_actor"):
                    actor["original_actor"] = user["original_actor"]
        body = request.get_json(silent=True) if not is_read else None
        event = build_operation_event(
            request.endpoint or f"{request.method} {path}",
            status="started",
            phase="request",
            actor=actor,
            dataset_keys=[str(body["dataset_key"])]
            if isinstance(body, dict) and body.get("dataset_key")
            else (
                [f"{request.view_args['dataset_namespace']}/{request.view_args['dataset_name']}"]
                if request.view_args
                and "dataset_namespace" in request.view_args
                and "dataset_name" in request.view_args
                else []
            ),
            details={
                "method": request.method,
                "path": path,
                "parameters": body if isinstance(body, dict) else {},
                "request_id": uuid.uuid4().hex,
            },
        )
        try:
            spool.write(event, reserve_result=True)
        except (OSError, RuntimeError):
            return jsonify({"error": "Audit storage unavailable or full; retry after recovery"}), 503
        g.usage_audit = (event, time.monotonic())
        return None

    @app.after_request
    def usage_end(response):
        audit = getattr(g, "usage_audit", None)
        if not audit and response.status_code in {401, 403}:
            # Successful polling is excluded, but authorization failures remain auditable.
            failure = usage_begin(denied=True)
            if failure is not None:
                response.headers["X-Data-Platform-Audit"] = "storage-unavailable"
            audit = getattr(g, "usage_audit", None)
        if not audit:
            return response
        original, started = audit
        payload = response.get_json(silent=True) if response.is_json else None
        payload = payload if isinstance(payload, dict) else {}
        job = payload.get("job") or {}
        job = job if isinstance(job, dict) else {}
        job_id = job.get("job_id") or job.get("id")
        user = getattr(g, "control_plane_user", None) or payload.get("user") or original["actor"]
        actor = {
            key: user[key] for key in ("user_id", "username", "role", "kind", "original_actor") if key in user
        }
        event = build_operation_event(
            original["operation"],
            phase="result",
            actor=actor,
            status="failed"
            if response.status_code >= 400
            else "accepted"
            if job_id or response.is_streamed
            else "success",
            parent_event_id=original["event_id"],
            dataset_keys=original.get("dataset_keys"),
            details={
                **original["details"],
                "job_id": job_id,
                "status_code": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "error": payload.get("error"),
            },
        )
        try:
            app.extensions["data_platform_usage_spool"].write(event, release_reservation=original["event_id"])
        except (OSError, RuntimeError):
            # The request intent remains durable, and the failure is visible to the operator.
            app.logger.error("Audit result could not be persisted; request intent remains in the audit spool")
            response.headers["X-Data-Platform-Audit"] = "result-pending"
        return response


def configure_management(app, store, root, lifecycle=None):
    app.extensions["data_platform_control"] = store
    spool = EventSpool(
        Path(root) / "management" / "audit-spool",
        max_bytes=int(os.environ.get("DATA_PLATFORM_AUDIT_SPOOL_BYTES", str(256 * 1024 * 1024))),
    )
    app.extensions["data_platform_usage_spool"] = spool
    url = os.environ.get("DATA_PLATFORM_LOG_DATABASE_URL")
    logs = UsageLogStore(url) if url else None
    app.extensions["data_platform_logs"] = logs
    browser = DatabaseBrowser(store, logs, lifecycle)
    app.extensions["data_platform_database_browser"] = browser
    delivery = LogDelivery(store, logs, spool, lifecycle) if logs else None
    app.extensions["data_platform_log_delivery"] = delivery
    if delivery and not app.testing and not os.environ.get("DATA_PLATFORM_INTERNAL_EXECUTION"):
        delivery.start()

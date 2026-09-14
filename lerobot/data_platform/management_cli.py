"""Explicit schema setup and idempotent imports for platform management storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import select

from lerobot.data_platform.control_plane import ControlPlaneStore, RemoteJob, RemoteJobEvent
from lerobot.data_platform.management_storage import UsageLogStore
from lerobot.data_platform.operation_log import build_operation_event


def import_jsonl(logs, paths):
    count = 0
    for path in paths:
        with Path(path).open() as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Expected an event object")
                    event.setdefault("event_id", hashlib.sha256(line.strip().encode()).hexdigest())
                    logs.append(event)
                    count += 1
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError(f"Invalid audit event at line {line_number}") from exc
    return count


def backfill_job_events(control, logs):
    after, count = 0, 0
    while True:
        with control.sessions() as session:
            rows = session.scalars(
                select(RemoteJobEvent)
                .where(RemoteJobEvent.event_id > after)
                .order_by(RemoteJobEvent.event_id)
                .limit(200)
            ).all()
            if not rows:
                return count
            for row in rows:
                job = session.get(RemoteJob, row.job_id)
                event = build_operation_event(
                    "job.event",
                    status="recorded",
                    source="legacy-job-events",
                    actor={"user_id": job.requested_by} if job else {"kind": "unknown"},
                    event_id=hashlib.sha256(f"{row.job_id}:{row.event_id}".encode()).hexdigest(),
                    details={
                        "job_id": row.job_id,
                        "message": row.message,
                        "level": row.level,
                        "payload": row.payload,
                    },
                )
                event["timestamp"] = row.created_at.isoformat() + "+00:00"
                logs.append(event)
                after = row.event_id
                count += 1


def main():
    from lerobot.data_platform.environment import check_server_environment

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=("dev", "prod"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Create versioned management tables and the separate log schema")
    importer = commands.add_parser(
        "import-jsonl", help="Import existing audit JSONL; duplicate event IDs are ignored"
    )
    importer.add_argument("paths", nargs="+", type=Path)
    commands.add_parser("backfill-job-events", help="Copy old job events without deleting the originals")
    args = parser.parse_args()
    if args.env:
        from lerobot.data_platform.deployment import Deployment, load_environment

        load_environment(Deployment(args.env))
    elif os.environ.get("DATA_PLATFORM_ENV"):
        parser.error("Named environments require explicit --env dev|prod")
    check_server_environment()
    log_url = os.environ.get("DATA_PLATFORM_LOG_DATABASE_URL")
    if not log_url:
        parser.error("DATA_PLATFORM_LOG_DATABASE_URL must be configured")
    logs = UsageLogStore(log_url)
    if args.command == "import-jsonl":
        print(f"Processed {import_jsonl(logs, args.paths)} audit records (idempotent)")
        return
    control_url = os.environ.get("DATA_PLATFORM_DATABASE_URL")
    if not control_url:
        parser.error("DATA_PLATFORM_DATABASE_URL must be configured")
    control = ControlPlaneStore(control_url, initialize_schema=args.command == "migrate")
    if args.command == "migrate":
        logs.ensure_schema(initialize=True)
        print("Management and log schema setup completed")
    else:
        print(f"Processed {backfill_job_events(control, logs)} historical job events; originals retained")


if __name__ == "__main__":
    main()

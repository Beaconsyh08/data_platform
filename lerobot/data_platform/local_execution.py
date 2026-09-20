"""Persist validated local job requests and execute them outside the web process."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import socket
import time
from pathlib import Path

from flask import current_app, g, has_app_context, request

from lerobot.data_platform.execution import atomic_json


def launch_background(*, target, name, daemon=True, thread_factory=None):
    """Keep single-user execution compatible; central deployments enqueue validated requests."""
    import threading

    factory = thread_factory or threading.Thread
    if not has_app_context():
        return factory(target=target, name=name, daemon=daemon).start()
    app = current_app._get_current_object()
    ctx = app.extensions.get("data_platform_route_context")
    if app.extensions.get("data_platform_local_execution"):
        return target()
    if ctx is None or ctx.control_plane_store is None:
        return factory(target=target, name=name, daemon=daemon).start()
    job = next((row for row in ctx.jobs_registry.values() if name.endswith(row["id"])), None)
    if job is None:
        raise RuntimeError("Background work must have a registered job before submission")
    try:
        continuation = None
        if job.get("job_type") == "task_mapping_cache":
            captured = dict(
                zip(
                    target.__code__.co_freevars,
                    (cell.cell_contents for cell in target.__closure__ or ()),
                    strict=True,
                )
            )
            continuation = {"task_config": captured["mapping"].snapshot}
        enqueue_local(app, ctx, job, continuation=continuation)
        if continuation:
            captured["release"]()
    except Exception:
        with ctx.jobs_lock:
            ctx.jobs_registry.pop(job["id"], None)
        raise
    return None


def enqueue_local(app, ctx, job, *, continuation=None, command=None, idempotency_key=None):
    from lerobot.data_platform.environment import EnvironmentIdentity, verify_directory

    store = ctx.control_plane_store
    user = getattr(g, "control_plane_user", None)
    if not user or user["role"] not in {"admin", "operator"}:
        raise PermissionError("Authenticated operator required for local job submission")
    root = Path(app.static_folder).parent / "management"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = EnvironmentIdentity.from_env()
    if identity:
        verify_directory(root.resolve(), "agent", initialize=True)
    with (root / ".local-config-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        name = "local-" + hashlib.sha256(f"{socket.gethostname()}:{root.resolve()}".encode()).hexdigest()[:16]
        state_file = root / "local-agent.json"
        if state_file.exists():
            state = json.loads(state_file.read_text())
        else:
            token = os.urandom(32).hex()
            node_token, node = store.enroll_node(
                name=name,
                hostname=socket.gethostname(),
                allowed_roots=[],
                writable_roots=[],
                capabilities={
                    "job_protocol": 2,
                    "data_profile_protocol": 100,
                    "local_requests": True,
                    "environment": identity.name if identity else "legacy",
                    "instance_id": identity.instance_id if identity else "",
                },
                enrollment_token=token,
                expected_token=token,
            )
            state = {
                "node_id": node["node_id"],
                "node_token": node_token,
                "name": name,
                "server_url": os.environ.get("DATA_PLATFORM_LOCAL_SERVER_URL", "http://127.0.0.1:9091"),
                "environment": identity.name if identity else "",
                "instance_id": identity.instance_id if identity else "",
            }
            atomic_json(state_file, state)
        entries = []
        for key, entry in ctx.datasets_index.items():
            entries.append(
                {
                    "repo_id": ctx.repo_id_from_key(key),
                    "root": str(entry["root"]),
                    "output_dir": str(entry["output_dir"]),
                }
            )
        entry = next((entry for entry in entries if entry["repo_id"] == job.get("dataset_key")), None)
        body = copy.deepcopy(request.get_json(silent=True) or {})
        if not isinstance(body, dict):
            raise ValueError("Background requests must be JSON objects")
        secrets = {}

        def extract(value, path=""):
            from lerobot.data_platform.operation_log import _is_sensitive_key

            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    current = path + "/" + key
                    if _is_sensitive_key(key) and item:
                        secrets[current] = item
                        result[key] = {"__secret_ref__": current}
                    else:
                        result[key] = extract(item, current)
                return result
            if isinstance(value, list):
                return [extract(item, path + "/" + str(index)) for index, item in enumerate(value)]
            return value

        body = extract(body)
        if secrets:
            atomic_json(root / "request-secrets" / f"{job['id']}.json", secrets)
        source = entry["root"] if entry else str(Path(body.get("root") or root).expanduser().resolve())
        output_dir = entry["output_dir"] if entry else str(root / "local-cache")
        previous = (
            json.loads((root / "local-executor.json").read_text())
            if (root / "local-executor.json").exists()
            else {}
        )
        allowed = sorted(
            set(previous.get("allowed_roots", []) + [source] + [entry["root"] for entry in entries])
        )
        writable = set(previous.get("writable_roots", [])) | {str(root)}
        writable.update(str(Path(entry["output_dir"]).parent) for entry in entries)
        writable.update(str(Path(entry["root"]).parent) for entry in entries)
        if job.get("output_root"):
            writable.add(str(Path(job["output_root"]).expanduser().resolve().parent))
        atomic_json(
            root / "local-executor.json",
            {
                "state_file": str(state_file),
                "allowed_roots": allowed,
                "writable_roots": sorted(writable),
                "allow_source_mutations": app.config.get("DATA_PLATFORM_LEGACY_MUTATIONS", False),
            },
        )
        store.configure_local_execution_roots(state["node_id"], allowed, sorted(writable))
        location = store.sync_locations(
            state["node_id"],
            [
                {
                    "dataset_key": job.get("dataset_key") or "local/console",
                    "root": source,
                    "output_dir": output_dir,
                    "metadata": {"local_execution": True},
                }
            ],
        )[0]
        options = {
            "request": {"path": request.path, "method": request.method, "body": body},
            "application": {
                "output_dir": str(Path(app.static_folder).parent),
                "console_mode": app.config.get("DATA_PLATFORM_CONSOLE_MODE", "full"),
            },
            "registry": entries,
            "local_job": {
                key: value for key, value in job.items() if not key.startswith("_") or key == "_task_input"
            },
            "continuation": continuation,
        }
        if command is not None:
            from lerobot.data_platform.curation_execution import worker_capabilities

            capabilities = {**agent_capabilities_for_local(), **worker_capabilities()}
            store.heartbeat(state["node_id"], capabilities=capabilities)
            options = command["options"]
        persisted = store.create_job(
            location_id=location["location_id"],
            requested_by=user["user_id"],
            operation=command["operation"] if command else f"local.request.{request.endpoint}",
            options=options,
            job_id=job["id"],
            idempotency_key=idempotency_key,
        )
        job["persistent_job_id"] = persisted["job_id"]
        job["message"] = "Queued for the local executor"
        return persisted


def local_job_payload(store, persistent):
    snapshot = dict((persistent.get("options") or {}).get("local_job") or {})
    snapshot.update((persistent.get("result") or {}).get("local_job") or {})
    snapshot.update(
        {
            "id": persistent["job_id"],
            "status": persistent["status"],
            "error": persistent.get("error"),
            "persistent_job_id": persistent["job_id"],
            "control_job_id": persistent["job_id"],
        }
    )
    from datetime import datetime

    for key in ("created_at", "started_at", "finished_at", "updated_at"):
        if persistent.get(key):
            snapshot[key] = datetime.fromisoformat(persistent[key]).timestamp()
    if persistent["status"] == "done":
        snapshot["progress"] = 100
    return snapshot


def execute_local_request(config):
    """Replay only a server-validated request, with current account and domain permissions."""
    from lerobot.data_platform.wsgi import create_app

    job = config["job"]
    options = job["options"]
    os.environ["DATA_PLATFORM_OUTPUT_DIR"] = options["application"]["output_dir"]
    os.environ["DATA_PLATFORM_CONSOLE_MODE"] = options["application"]["console_mode"]
    # This environment-only context is inaccessible to HTTP callers.
    os.environ["DATA_PLATFORM_INTERNAL_EXECUTION"] = str(Path(config["work"]) / "config.json")
    app = create_app()
    app.extensions["data_platform_local_execution"] = True
    ctx = app.extensions["data_platform_route_context"]
    for entry in options["registry"]:
        key = ctx.repo_key(entry["repo_id"])
        if key not in ctx.datasets_index:
            dataset = ctx.meta_only_dataset_cls(entry["repo_id"], root=Path(entry["root"]))
            ctx.register_dataset(dataset, Path(entry["output_dir"]))
    saved_request = copy.deepcopy(options["request"])
    secret_path = (
        Path(options["application"]["output_dir"])
        / "management"
        / "request-secrets"
        / f"{job['job_id']}.json"
    )
    secrets = json.loads(secret_path.read_text()) if secret_path.exists() else {}

    def restore(value):
        if isinstance(value, dict):
            if set(value) == {"__secret_ref__"}:
                return secrets[value["__secret_ref__"]]
            return {key: restore(item) for key, item in value.items()}
        if isinstance(value, list):
            return [restore(item) for item in value]
        return value

    saved_request["body"] = restore(saved_request["body"])
    if options.get("continuation"):
        from lerobot.data_platform.cli import run_precompute

        snapshot = options["continuation"]["task_config"]
        source = next(
            row for row in options["registry"] if row["repo_id"] == options["local_job"]["dataset_key"]
        )
        lifecycle = ctx.lifecycle_store()
        current = lifecycle.tasks.snapshot(source["repo_id"]).to_dict()
        if current["digest"] != snapshot["digest"]:
            raise ValueError("Task mapping changed while queued; rebuild the latest mapping")
        lease_id = "task-mapping-cache:" + source["repo_id"]
        owner = job["execution"]["attempt_id"]
        if not lifecycle.repository.claim_job(lease_id, "task_mapping_cache", owner=owner, lease_seconds=300):
            raise RuntimeError("Task cache is busy")

        def progress(payload):
            if not lifecycle.repository.heartbeat_job(lease_id, owner=owner, lease_seconds=300):
                raise RuntimeError("Task cache lease lost")

        try:
            run_precompute(
                root=Path(source["root"]),
                repo_id=source["repo_id"],
                output_dir=Path(source["output_dir"]),
                prepare_videos=False,
                prepare_csv=True,
                visualize_only=True,
                task_config=snapshot,
                show_progress=False,
                progress_callback=progress,
            )
        finally:
            lifecycle.repository.release_job(lease_id, owner=owner)
        return {"local_job": {**options["local_job"], "status": "done"}, "local_registrations": [source]}
    response = app.test_client().open(
        saved_request["path"], method=saved_request["method"], json=saved_request["body"]
    )
    payload = response.get_json(silent=True) or {}
    if response.status_code >= 400:
        raise RuntimeError(payload.get("error") or "Local request execution failed")
    local_job = payload.get("job") or {}
    local_job = dict(ctx.jobs_registry.get(local_job.get("id"), local_job))
    if local_job.get("status") == "error":
        raise RuntimeError(local_job.get("error") or "Local operation failed")
    if local_job.get("status") != "done":
        raise RuntimeError("Local handler did not confirm completion")
    registrations = [
        {
            "repo_id": ctx.repo_id_from_key(key),
            "root": str(entry["root"]),
            "output_dir": str(entry["output_dir"]),
        }
        for key, entry in ctx.datasets_index.items()
    ]
    return {"local_job": local_job, "local_registrations": registrations}


def report_local_progress(payload):
    from lerobot.data_platform.execution import SpoolingClient

    config = json.loads(Path(os.environ["DATA_PLATFORM_INTERNAL_EXECUTION"]).read_text())
    client = SpoolingClient(config.get("server_url", "local"), Path(config["work"]))
    client.sequence = time.time_ns()
    client.event(None, config["job"]["job_id"], payload.get("message") or "Local task progress", payload)


def internal_execution_user(store):
    """Verify the saved actor again; never trust a user ID from a request header."""
    from lerobot.data_platform.control_plane import ControlPlaneUser

    value = os.environ.get("DATA_PLATFORM_INTERNAL_EXECUTION")
    if not value:
        return None
    config = json.loads(Path(value).read_text())
    saved = config["job"]["options"]["request"]
    if request.path != saved["path"] or request.method != saved["method"]:
        return None
    with store.sessions() as session:
        actor = session.get(ControlPlaneUser, config["job"]["requested_by"])
        if actor is None or not actor.active or actor.role not in {"admin", "operator"}:
            raise PermissionError("Submitting account is no longer permitted to execute")
        return store._user_dict(actor)


def main():
    import argparse
    import logging

    from lerobot.data_platform.agent import AgentClient, DataPlatformAgent
    from lerobot.data_platform.environment import check_server_environment

    check_server_environment()

    parser = argparse.ArgumentParser(description="Run queued local Data Platform requests outside Gunicorn")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    config_path = (
        args.config or Path(os.environ["DATA_PLATFORM_OUTPUT_DIR"]) / "management" / "local-executor.json"
    )
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            if config_path.exists():
                config = json.loads(config_path.read_text())
                state = json.loads(Path(config["state_file"]).read_text())
                agent = DataPlatformAgent(
                    client=AgentClient(state["server_url"]),
                    state_path=Path(config["state_file"]),
                    name=state["name"],
                    allowed_roots=[Path(value) for value in config["allowed_roots"]],
                    writable_roots=[Path(value) for value in config["writable_roots"]],
                    enrollment_token="",
                    allow_source_mutations=config["allow_source_mutations"],
                )
                agent.run_once(sync=False)
        except Exception:
            logging.exception("Local executor will retry reconciliation")
        time.sleep(3)


def agent_capabilities_for_local():
    from lerobot.data_platform.curation_execution import worker_capabilities

    return {
        **worker_capabilities(),
        "job_protocol": 2,
        "data_profile_protocol": 100,
        "local_requests": True,
        "environment": os.environ.get("DATA_PLATFORM_ENV", "legacy"),
        "instance_id": os.environ.get("DATA_PLATFORM_INSTANCE_ID", ""),
        "release": os.environ.get("DATA_PLATFORM_RELEASE", "legacy"),
        "caption_protocol": 1,
        "caption_schemes": ["multiview_semantics", "video_events", "fusion_review"],
        "caption_configured": bool(
            os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_DASHSCOPE_API_KEY")
        ),
    }


if __name__ == "__main__":
    main()

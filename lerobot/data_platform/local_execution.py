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


def launch_background(
    *,
    target,
    name,
    daemon=True,
    thread_factory=None,
    command=None,
    continuation=None,
    on_enqueued=None,
    compatibility_operator=None,
):
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
        if command is not None:
            from lerobot.data_platform.operators import get_operator

            spec = get_operator(command["operation"])
            if spec.effect == "source":
                # Preserve dedicated source guards, backups and cache refresh semantics.
                enqueue_local(app, ctx, job, execution_ref=command["options"]["operator_snapshot"])
            else:
                command["options"]["local_job"] = {
                    key: value for key, value in job.items() if not key.startswith("_")
                }
                enqueue_local(app, ctx, job, command=command)
        else:
            enqueue_local(
                app,
                ctx,
                job,
                continuation=continuation,
                execution_ref={
                    "operator_id": compatibility_operator or "task_mapping_cache",
                    "version": "1.0.0",
                }
                if continuation or compatibility_operator
                else None,
            )
        if on_enqueued is not None:
            on_enqueued()
    except Exception:
        with ctx.jobs_lock:
            ctx.jobs_registry.pop(job["id"], None)
        raise
    return None


def enqueue_local(
    app, ctx, job, *, continuation=None, command=None, idempotency_key=None, execution_ref=None
):
    from lerobot.data_platform.environment import EnvironmentIdentity, verify_directory

    store = ctx.control_plane_store
    user = getattr(g, "control_plane_user", None)
    if not user or user["role"] not in {"admin", "data_manager", "operator"}:
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
        computation = ((command or {}).get("options") or {}).get("local_compute") or {}
        parameters = computation.get("parameters", {})
        primary = next(
            (
                parameters[key]
                for key in ("root", "src_root", "base_root", "dataset_root")
                if parameters.get(key)
            ),
            None,
        )
        source_roots = list(parameters.get("src_roots") or [])
        source_roots.extend(parameters.get("subtract_roots") or [])
        primary = primary or (source_roots[0] if source_roots else None)
        if primary:
            entry = next(
                (item for item in entries if Path(item["root"]).resolve() == Path(primary).resolve()), None
            )
            if entry is None:
                raise PermissionError("Local computations require registered source datasets")
        selected = [entry] if entry else []
        for repo_id in job.get("related_dataset_keys") or []:
            related = next((item for item in entries if item["repo_id"] == str(repo_id)), None)
            if related and related not in selected:
                selected.append(related)
        if entry is None and selected:
            entry = selected[0]
        for path in source_roots:
            candidate = next(
                (item for item in entries if Path(item["root"]).resolve() == Path(path).resolve()), None
            )
            if candidate is None:
                raise PermissionError("Every computation input must be a registered dataset")
            if candidate not in selected:
                selected.append(candidate)
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
        synchronized = store.sync_locations(
            state["node_id"],
            [
                {
                    "dataset_key": item["repo_id"],
                    "root": item["root"],
                    "output_dir": item["output_dir"],
                    "metadata": {
                        **json.loads((Path(item["root"]) / "meta/info.json").read_text()),
                        "local_execution": True,
                    },
                }
                for item in selected
            ]
            or [
                {
                    "dataset_key": job.get("dataset_key") or "local/console",
                    "root": source,
                    "output_dir": output_dir,
                    "metadata": {"local_execution": True},
                }
            ],
        )
        location = next((item for item in synchronized if item["root"] == source), synchronized[0])
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
        if execution_ref is not None:
            options["operator_snapshot"] = execution_ref
        if command is not None:
            from lerobot.data_platform.curation_execution import worker_capabilities

            capabilities = {**agent_capabilities_for_local(), **worker_capabilities()}
            store.heartbeat(state["node_id"], capabilities=capabilities)
            options = extract(command["options"])
        if len(synchronized) > 1:
            options["source_location_ids"] = [item["location_id"] for item in synchronized]
            options["_source_locations"] = synchronized
        if secrets:
            atomic_json(root / "request-secrets" / f"{job['id']}.json", secrets)
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


def permit_local_derived_input(store, node: dict, job: dict, location: dict, management: Path) -> None:
    """Add only a registered, confirmed output to the local executor's existing read scope."""
    if not node["name"].startswith("local-") or not node.get("capabilities", {}).get("local_requests"):
        return
    options = job.get("options") or {}
    if not options.get("operator_snapshot") or not options.get("out_root"):
        return
    from lerobot.data_platform.agent import _require_safe_output, _require_within

    registered = store.get_location(location["location_id"])
    if registered["node_id"] != node["node_id"] or registered["root"] != location["root"]:
        raise PermissionError("Derived input must be registered on this local executor")
    config_path = management / "local-executor.json"
    with (management / ".local-config-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(config_path.read_text())
        state = json.loads(Path(config["state_file"]).read_text())
        if state["node_id"] != node["node_id"] or state["name"] != node["name"]:
            raise PermissionError("Derived input does not belong to the configured local executor")
        source = store.get_location(job["location_id"])
        _require_within(
            Path(source["root"]), [Path(value).resolve() for value in config["allowed_roots"]], "source"
        )
        output = _require_safe_output(
            Path(registered["root"]),
            Path(source["root"]),
            [Path(value).resolve() for value in config["writable_roots"]],
        )
        if output != Path(options["out_root"]).resolve() or not output.is_dir():
            raise PermissionError("Derived input does not match the confirmed output")
        allowed = sorted(set(config["allowed_roots"]) | {str(output)})
        store.configure_local_execution_roots(node["node_id"], allowed, config["writable_roots"])
        atomic_json(config_path, {**config, "allowed_roots": allowed})


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
    progress = (persistent.get("result") or {}).get("progress", {})
    for key in ("current", "total", "progress", "message"):
        if key in progress:
            snapshot[key] = progress[key]
    if persistent.get("events"):
        snapshot["logs"] = [
            {"time": event["created_at"], "message": event["message"]}
            for event in persistent["events"]
            if event.get("payload", {}).get("log_visible", True)
            and persistent["status"] != "queued"
            and event.get("payload", {}).get("attempt_id") in {None, persistent.get("attempt_id")}
        ][-80:]
    if persistent["status"] == "done":
        snapshot["progress"] = 100
        snapshot["message"] = (persistent.get("result", {}).get("local_job") or {}).get(
            "message"
        ) or "Completed"
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

    def execute():
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
            if not lifecycle.repository.claim_job(
                lease_id, "task_mapping_cache", owner=owner, lease_seconds=300
            ):
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

    reference = options.get("operator_snapshot")
    if not reference:
        return execute()  # Requests accepted by earlier releases retain their original adapter.
    from lerobot.data_platform.execution_pools import check_stop
    from lerobot.data_platform.operators import ExecutionContext, execute_operator

    return execute_operator(
        reference["operator_id"],
        ExecutionContext(
            source_root=Path(job["location"]["root"]),
            work_dir=Path(config["work"]),
            check_cancelled=check_stop,
            handlers={reference["operator_id"]: execute},
        ),
        version=reference["version"],
    )


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
        if actor is None or not actor.active or actor.role not in {"admin", "data_manager", "operator"}:
            raise PermissionError("Submitting account is no longer permitted to execute")
        return store._user_dict(actor)


def main():
    from lerobot.data_platform.rolling_deployment import ExecutorGeneration

    with ExecutorGeneration.from_env() as generation:
        _run(generation)


def _run(generation):
    import argparse
    import logging

    from lerobot.data_platform.agent import AgentClient, DataPlatformAgent
    from lerobot.data_platform.environment import check_server_environment

    check_server_environment()
    # The local executor calls run_once synchronously; only the H100 Agent dispatches concurrent jobs.
    os.environ["DATA_PLATFORM_CAPTION_RUNNING_LIMIT"] = "1"

    parser = argparse.ArgumentParser(description="Run queued local Data Platform requests outside Gunicorn")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    config_path = (
        args.config or Path(os.environ["DATA_PLATFORM_OUTPUT_DIR"]) / "management" / "local-executor.json"
    )
    logging.basicConfig(level=logging.INFO)
    agent = None
    loaded_config = None
    try:
        while True:
            try:
                if config_path.exists():
                    config = json.loads(config_path.read_text())
                    state = json.loads(Path(config["state_file"]).read_text())
                    # Retain the transport and caption readers until their configuration changes.
                    if agent is None or loaded_config != (config, state):
                        if agent is not None:
                            agent.close()
                            agent = None
                        client = AgentClient(state["server_url"])
                        try:
                            agent = DataPlatformAgent(
                                client=client,
                                state_path=Path(config["state_file"]),
                                name=state["name"],
                                allowed_roots=[Path(value) for value in config["allowed_roots"]],
                                writable_roots=[Path(value) for value in config["writable_roots"]],
                                enrollment_token="",
                                allow_source_mutations=config["allow_source_mutations"],
                                local_requests=True,
                            )
                        except Exception:
                            client.session.close()
                            raise
                        loaded_config = config, state
                    if generation.draining:
                        from lerobot.data_platform.execution import ExecutionSupervisor

                        supervisor = ExecutionSupervisor(agent)
                        if not supervisor.reconcile() and not supervisor.pending_here():
                            return
                    else:
                        agent.run_once(sync=False)
                else:
                    if agent is not None:
                        agent.close()
                        agent = None
                    if generation.draining:
                        return
            except Exception:
                logging.exception("Local executor will retry reconciliation")
            time.sleep(3)
    finally:
        if agent is not None:
            agent.close()


def agent_capabilities_for_local():
    from lerobot.data_platform.curation_execution import worker_capabilities
    from lerobot.data_platform.operators import list_operators

    return {
        **worker_capabilities(),
        "operator_versions": {spec.operator_id: spec.version for spec in list_operators()},
        "job_protocol": 2,
        "data_profile_protocol": 100,
        "local_requests": True,
        "environment": os.environ.get("DATA_PLATFORM_ENV", "legacy"),
        "instance_id": os.environ.get("DATA_PLATFORM_INSTANCE_ID", ""),
        "release": os.environ.get("DATA_PLATFORM_RELEASE", "legacy"),
        "caption_protocol": 1,
        "caption_dataset_versions": ["v2.1", "v3.0"],
        "caption_schemes": ["multiview_semantics", "video_events", "fusion_review"],
        "caption_configured": bool(
            os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_DASHSCOPE_API_KEY")
        ),
    }


if __name__ == "__main__":
    main()

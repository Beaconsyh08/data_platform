"""Task discovery, catalog editing and dataset mapping routes."""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from functools import wraps
from pathlib import Path

from flask import g, jsonify, render_template, request

from lerobot.data_platform.cli import run_precompute
from lerobot.data_platform.local_execution import launch_background
from lerobot.data_platform.precompute.dataset_io import load_episode_records, load_task_records
from lerobot.data_platform.precompute.viewer_manifest import load_viewer_manifest
from lerobot.data_platform.routes.task_suggestions import register_task_suggestion_routes
from lerobot.data_platform.task_catalog import TASK_CONFIG_PROTOCOL, TaskConfigSnapshot, normalize_task


def register_task_routes(app, ctx) -> None:
    def lifecycle():
        return ctx.lifecycle_store() if callable(ctx.lifecycle_store) else ctx.lifecycle_store

    def author():
        return (getattr(g, "control_plane_user", None) or {}).get("username", "local-user")

    def handled(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except KeyError as exc:
                return jsonify(error=str(exc)), 404
            except (ValueError, TypeError) as exc:
                return jsonify(error=str(exc)), 409 if "conflict" in str(exc) else 400

        return wrapped

    def discover(dataset_key):
        if not dataset_key or not isinstance(dataset_key, str):
            raise ValueError("dataset_key is required")
        control = getattr(ctx, "control_plane_store", None)
        if control:
            location = next(
                (row for row in control.list_locations() if row["dataset_key"] == dataset_key), None
            )
            if location:
                metadata = location.get("metadata") or {}
                tasks = metadata.get("tasks")
                if tasks is None:
                    raise ValueError("Agent has not reported tasks; upgrade the Agent and sync datasets")
                return tasks, metadata.get("episodes", []), location
        entry = ctx.datasets_index.get(ctx.repo_key(dataset_key))
        if entry is None:
            raise KeyError("registered dataset not found")
        root = Path(entry["root"])
        if (root / "meta" / "info.json").is_file():
            return load_task_records(root), load_episode_records(root), None
        manifest = load_viewer_manifest(Path(entry["output_dir"]) / "static")
        if not manifest:
            raise ValueError("dataset has no task metadata or viewer manifest")
        episodes = manifest.get("episodes", [])
        return [{"task": task} for row in episodes for task in row.get("tasks", [])], episodes, None

    def preview(body):
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        store = lifecycle()
        key = body.get("dataset_key")
        tasks, episodes, location = discover(key)
        current = store.tasks.current(key)
        catalog_id = body.get("catalog_version_id")
        if not catalog_id and current:
            catalog_id = current.snapshot["catalog"]["catalog_version_id"]
        if not catalog_id:
            catalog_id = store.tasks.catalogs()[0].catalog_version_id
        mappings = body.get("mappings")
        if mappings is None and current:
            mappings = current.snapshot["mappings"]
        result = store.tasks.preview(key, tasks, catalog_id, mappings)
        by_text = defaultdict(list)
        tasks_by_index = {int(row["task_index"]): row["task"] for row in tasks if "task_index" in row}
        for row in episodes:
            texts = store._episode_tasks(row, tasks_by_index)
            for text in texts:
                by_text[normalize_task(text)].append(int(row["episode_index"]))
        for row in result["tasks"]:
            row["episode_indices"] = sorted(set(by_text[normalize_task(row["raw_task"])]))
            row["episode_count"] = len(row["episode_indices"])
        result["mapping"] = current.to_dict() if current else None
        result["applied_snapshot"] = store.tasks.snapshot(key).to_dict()
        result["remote"] = location is not None
        result["analysis_url"] = (
            f"/remote/{location['location_id']}/analysis" if location else f"/{key}/analysis"
        )
        return result, tasks, location

    register_task_suggestion_routes(app, ctx, preview=preview)

    def audit(operation, **details):
        if ctx.append_operation_log:
            ctx.append_operation_log(lifecycle().root, operation, status="success", details=details)

    @app.get("/tasks")
    def task_page():
        return render_template("data_platform_tasks.html", dataset_key=request.args.get("dataset_key", ""))

    @app.get("/api/task-catalogs")
    def task_catalogs():
        return jsonify(catalogs=[catalog.to_dict() for catalog in lifecycle().tasks.catalogs()])

    @app.post("/api/task-catalogs")
    @handled
    def create_task_catalog():
        body = request.get_json() or {}
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        catalog = lifecycle().tasks.create_catalog(
            body.get("tasks", []),
            expected_version_id=body.get("expected_version_id", ""),
            created_by=author(),
        )
        audit("task_catalog_create", catalog_version_id=catalog.catalog_version_id)
        return jsonify(catalog=catalog.to_dict())

    @app.get("/api/task-mappings")
    @handled
    def get_task_mapping():
        result, _, _ = preview({"dataset_key": request.args.get("dataset_key")})
        return jsonify(result)

    @app.post("/api/task-mappings/preview")
    @handled
    def preview_task_mapping():
        result, _, _ = preview(request.get_json() or {})
        return jsonify(result)

    @app.post("/api/task-mappings")
    @handled
    def apply_task_mapping():
        body = request.get_json() or {}
        result, tasks, location = preview(body)
        store = lifecycle()
        lease_id = f"task-mapping-cache:{result['dataset_key']}"
        lease_owner = uuid.uuid4().hex
        if location is None and not store.repository.claim_job(
            lease_id, "task_mapping_cache", owner=lease_owner, lease_seconds=300
        ):
            raise ValueError("task cache conflict; wait for the current cache rebuild before applying")

        def release():
            if location is None:
                store.repository.release_job(lease_id, owner=lease_owner)

        def progress(job, payload):
            if not store.repository.heartbeat_job(lease_id, owner=lease_owner, lease_seconds=300):
                raise ValueError("task cache lease lost; retry the cache rebuild")
            ctx.update_job(job, payload)

        try:
            response = save_and_rebuild(body, result, tasks, location, release, progress)
        except Exception:
            release()
            raise
        if location is not None or "job" not in response:
            release()
        return jsonify(response)

    def save_and_rebuild(body, result, tasks, location, release, progress):
        key = result["dataset_key"]
        snapshot = TaskConfigSnapshot.from_dict(result["snapshot"])
        mapping = lifecycle().tasks.apply(
            key,
            tasks,
            catalog_version_id=snapshot.catalog.catalog_version_id,
            mappings=snapshot.mappings,
            expected_version_id=body.get("expected_version_id"),
            expected_inventory_digest=body.get("expected_inventory_digest", ""),
            created_by=author(),
        )
        audit("task_mapping_apply", dataset_key=key, mapping_version_id=mapping.mapping_version_id)
        response = {"mapping": mapping.to_dict(), "message": "Task mapping saved"}
        if location:
            control = ctx.control_plane_store
            node = next(row for row in control.list_nodes() if row["node_id"] == location["node_id"])
            if node.get("capabilities", {}).get("task_config_protocol") != TASK_CONFIG_PROTOCOL:
                response["message"] = "Mapping saved; upgrade the Agent to rebuild task-aware cache"
            else:
                user = getattr(g, "control_plane_user", None) or {}
                response["job"] = control.create_job(
                    location_id=location["location_id"],
                    requested_by=user.get("user_id"),
                    operation="viewer.prepare",
                    options={"task_config": mapping.snapshot, "prepare_videos": False, "prepare_csv": True},
                    reuse_active=True,
                )
                response["message"] = "Mapping saved; Agent cache rebuild queued"
        else:
            entry = ctx.datasets_index[ctx.repo_key(key)]
            root = Path(entry["root"])
            if not (root / "meta" / "info.json").is_file():
                response["message"] = "Mapping saved; source unavailable, showing the previous cache version"
                return response
            job = {
                "id": uuid.uuid4().hex,
                "job_type": "task_mapping_cache",
                "dataset_key": key,
                "status": "queued",
                "current": 0,
                "total": 100,
                "progress": 0,
                "logs": [],
            }
            with ctx.jobs_lock:
                ctx.jobs_registry[job["id"]] = job

            def rebuild():
                try:
                    ctx.update_job(
                        job, {"status": "running", "message": "Refreshing task configuration cache"}
                    )
                    run_precompute(
                        root=root,
                        repo_id=key,
                        output_dir=Path(entry["output_dir"]),
                        prepare_videos=False,
                        prepare_csv=True,
                        visualize_only=True,
                        task_config=mapping.snapshot,
                        show_progress=False,
                        progress_callback=lambda payload: progress(job, payload),
                    )
                    if ctx.clear_dataset_caches:
                        ctx.clear_dataset_caches(ctx.repo_key(key))
                    ctx.finish_job(job, "Task configuration cache refreshed")
                except Exception as exc:
                    ctx.fail_job(job, "Task configuration cache refresh failed", exc)
                finally:
                    release()

            launch_background(
                target=rebuild, name=f"task-mapping-{job['id']}", daemon=True, thread_factory=threading.Thread
            )
            response["job"] = job
            response["message"] = "Mapping saved; local cache rebuild started"
        return response

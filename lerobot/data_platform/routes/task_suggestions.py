"""Background task suggestions using the shared server-side Qwen configuration."""

from __future__ import annotations

import threading
import time
import uuid
from functools import wraps

from flask import g, jsonify, request

from lerobot.data_platform.local_execution import launch_background
from lerobot.data_platform.qwen import QwenClient, dashscope_env_api_key
from lerobot.data_platform.task_catalog import TaskConfigSnapshot
from lerobot.data_platform.task_suggestions import (
    PROMPT_VERSION,
    accepted_definitions,
    generate_suggestions,
    task_model,
)


def register_task_suggestion_routes(app, ctx, *, preview) -> None:
    def store():
        return ctx.lifecycle_store() if callable(ctx.lifecycle_store) else ctx.lifecycle_store

    def handled(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except KeyError:
                return jsonify(error="Task suggestion job or dataset not found"), 404
            except (ValueError, TypeError) as exc:
                return jsonify(error=str(exc)), 409 if "conflict" in str(exc) else 400

        return wrapped

    def body():
        value = request.get_json() or {}
        if not isinstance(value, dict):
            raise ValueError("Request body must be an object")
        return value

    def job_by_id(job_id):
        if app.extensions.get("data_platform_route_context") is ctx and ctx.control_plane_store is not None:
            from lerobot.data_platform.local_execution import local_job_payload

            try:
                persistent = ctx.control_plane_store.get_job(job_id)
                restored = local_job_payload(ctx.control_plane_store, persistent)
                if restored.get("job_type") == "task_suggestions":
                    return restored
            except KeyError:
                pass
        with ctx.jobs_lock:
            job = ctx.jobs_registry.get(job_id)
            if not job or job.get("job_type") != "task_suggestions":
                raise KeyError(job_id)
            return job

    def response_for(job):
        with ctx.jobs_lock:
            return {
                "job": {key: job.get(key) for key in ("id", "status", "progress", "message", "error")},
                "result": job.get("suggestion_result"),
            }

    @app.get("/api/task-suggestions/capabilities")
    def task_suggestion_capabilities():
        return jsonify(token_configured=bool(dashscope_env_api_key()), default_model=task_model())

    @app.post("/api/task-suggestions")
    @handled
    def start_task_suggestions():
        data = body()
        discovered, _, _ = preview(data)
        snapshot = TaskConfigSnapshot.from_dict(discovered["snapshot"])
        if snapshot.catalog.catalog_version_id != store().tasks.catalogs()[0].catalog_version_id:
            raise ValueError(
                "Catalog revision conflict; select the latest catalog before requesting suggestions"
            )
        pending = [row for row in discovered["tasks"] if row["status"] in {"unmapped", "conflict"}]
        if not pending:
            return jsonify(job=None, result={"suggestions": []})
        client = QwenClient()
        model = data.get("model") or task_model()
        if not isinstance(model, str) or not model.strip() or len(model) > 160:
            raise ValueError("Model must be a nonempty model ID of at most 160 characters")
        job = {
            "id": uuid.uuid4().hex,
            "job_type": "task_suggestions",
            "dataset_key": discovered["dataset_key"],
            "status": "queued",
            "created_at": time.time(),
            "current": 0,
            "total": len(pending),
            "progress": 0,
            "logs": [],
            "_task_input": discovered,
        }
        with ctx.jobs_lock:
            ctx.jobs_registry[job["id"]] = job

        def generate():
            try:
                ctx.update_job(job, {"status": "running", "message": "Suggesting task definitions with Qwen"})
                suggestions = generate_suggestions(
                    [row["raw_task"] for row in pending],
                    snapshot.catalog,
                    client=client,
                    model=model.strip(),
                    progress=lambda current, total: ctx.update_job(job, {"current": current, "total": total}),
                )
                counts = {row["mapping_key"]: row["episode_count"] for row in pending}
                result = {
                    "dataset_key": discovered["dataset_key"],
                    "catalog_version_id": snapshot.catalog.catalog_version_id,
                    "task_inventory_digest": discovered["task_inventory_digest"],
                    "model": model.strip(),
                    "prompt_version": PROMPT_VERSION,
                    "suggestions": [
                        {**row.to_dict(), "episode_count": counts[row.to_dict()["mapping_key"]]}
                        for row in suggestions
                    ],
                }
                with ctx.jobs_lock:
                    job["suggestion_result"] = result
                    job["result_summary"] = {"suggestion_count": len(suggestions), "model": model.strip()}
                ctx.finish_job(job, "Task suggestions are ready for review")
            except Exception as exc:
                ctx.fail_job(job, "Task suggestions failed", exc)

        launch_background(
            target=generate,
            name=f"task-suggestions-{job['id']}",
            daemon=True,
            thread_factory=threading.Thread,
        )
        return jsonify(response_for(job)), 202

    @app.get("/api/task-suggestions/<job_id>")
    @handled
    def get_task_suggestions(job_id):
        return jsonify(response_for(job_by_id(job_id)))

    @app.post("/api/task-suggestions/<job_id>/accept")
    @handled
    def accept_task_suggestions(job_id):
        job = job_by_id(job_id)
        if job["status"] != "done":
            raise ValueError("Task suggestion job conflict; wait for suggestions to finish")
        data = body()
        original = job["_task_input"]
        snapshot = TaskConfigSnapshot.from_dict(original["snapshot"])
        mapping_body = {
            "dataset_key": original["dataset_key"],
            "catalog_version_id": snapshot.catalog.catalog_version_id,
            "mappings": snapshot.mappings,
        }
        current, _, _ = preview(mapping_body)
        if current["task_inventory_digest"] != original["task_inventory_digest"] or (
            (current["mapping"] or {}).get("mapping_version_id")
            != (original["mapping"] or {}).get("mapping_version_id")
        ):
            raise ValueError(
                "Task mapping or inventory conflict; reload the dataset and regenerate suggestions"
            )
        definitions, mappings = accepted_definitions(
            data.get("suggestions"),
            snapshot.catalog,
            [row["raw_task"] for row in original["tasks"] if row["status"] in {"unmapped", "conflict"}],
        )
        author = (getattr(g, "control_plane_user", None) or {}).get("username", "local-user")
        catalog = store().tasks.create_catalog(
            definitions, expected_version_id=snapshot.catalog.catalog_version_id, created_by=author
        )
        if ctx.append_operation_log:
            ctx.append_operation_log(
                store().root,
                "task_suggestions_accept",
                status="success",
                details={
                    "dataset_key": original["dataset_key"],
                    "job_id": job_id,
                    "catalog_version_id": catalog.catalog_version_id,
                    "accepted_count": len(mappings),
                    "model": job["suggestion_result"]["model"],
                    "prompt_version": PROMPT_VERSION,
                },
            )
        result, _, _ = preview(
            {
                **mapping_body,
                "catalog_version_id": catalog.catalog_version_id,
                "mappings": {**snapshot.mappings, **mappings},
            }
        )
        return jsonify(catalog=catalog.to_dict(), preview=result)

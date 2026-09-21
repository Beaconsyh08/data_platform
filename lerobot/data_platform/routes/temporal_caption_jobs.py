"""Queue remote captions and receive attempt-scoped, validated artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from flask import abort, jsonify, request

from lerobot.data_platform.routes.control_plane import _current_user, _role_denied
from lerobot.data_platform.temporal_caption_demo import SCHEMES
from lerobot.data_platform.temporal_caption_jobs import (
    FILES,
    OPERATION,
    import_archive,
    publish_run,
    validate_options,
    validate_source,
)


def caption_scheme_status(capabilities, reason, user):
    # Caption protocol 1 originally supported these two schemes without advertising a list.
    supported = capabilities.get("caption_schemes", ["multiview_semantics", "video_events"])
    allowed = bool(user and user["role"] in {"operator", "data_manager", "admin"})
    return [
        {
            **scheme,
            "can_submit": allowed and reason is None and scheme_id in supported,
            "unavailable_reason": reason
            or (f"Upgrade the executor to support {scheme['name']}" if scheme_id not in supported else None)
            or ("Read-only account." if not allowed else None),
        }
        for scheme_id, scheme in SCHEMES.items()
    ]


def register_caption_job_routes(app, ctx, resolve):
    store = ctx.control_plane_store
    if store is None:
        return

    def job_root(job):
        _, _, dataset_root, _, _ = resolve(location_id=job["location_id"])
        return dataset_root.parent

    def staged(job):
        attempt = request.headers.get("X-Job-Attempt", "")
        if not attempt or any(c not in "0123456789abcdef-" for c in attempt):
            abort(409, description="Caption jobs require a current execution attempt")
        return job_root(job) / ".jobs" / job["job_id"] / attempt

    def readiness(location):
        try:
            validate_source(location.get("metadata") or {})
        except ValueError as exc:
            return str(exc)
        node = next((n for n in store.list_nodes() if n["node_id"] == location["node_id"]), {})
        capabilities = node.get("capabilities") or {}
        if capabilities.get("caption_protocol") != 1 or capabilities.get("job_protocol") != 2:
            return "Upgrade the remote Agent to caption protocol 1"
        if not capabilities.get("caption_configured"):
            return "Configure DASHSCOPE_API_KEY in the remote Agent service environment"
        return None

    @app.get("/api/control/locations/<location_id>/temporal-caption/jobs")
    def caption_jobs(location_id):
        resolve(location_id=location_id)
        user = _current_user()
        location = store.get_location(location_id)
        reason = readiness(location)
        node = next((n for n in store.list_nodes() if n["node_id"] == location["node_id"]), {})
        return jsonify(
            schemes=caption_scheme_status(node.get("capabilities") or {}, reason, user),
            can_import=bool(user and user["role"] == "admin"),
            can_submit=bool(
                user and user["role"] in {"operator", "data_manager", "admin"} and reason is None
            ),
            unavailable_reason=reason,
            jobs=[
                store.job_manager.decorate(job, user)
                for job in store.list_jobs(limit=200, actor=user)
                if job["location_id"] == location_id and job["operation"] == OPERATION
            ],
        )

    @app.post("/api/control/locations/<location_id>/temporal-caption/import")
    def import_captions(location_id):
        denied = _role_denied("admin")
        if denied:
            return denied
        key, name, dataset_root, _, _ = resolve(location_id=location_id)
        if request.content_length is None or request.content_length > 512 * 1024**2:
            abort(413)
        uploaded = request.files.get("results")
        if uploaded is None:
            return jsonify(error="Upload a review ZIP as results"), 400
        import zipfile

        try:
            runs = import_archive(uploaded.stream, dataset_root.parent, key, name)
        except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as exc:
            return jsonify(error=f"Invalid or conflicting caption import: {type(exc).__name__}"), 400
        return jsonify(runs=runs, review_url=f"/remote/{location_id}/temporal-caption"), 201

    @app.post("/api/control/locations/<location_id>/temporal-caption/jobs")
    def queue_caption(location_id):
        denied = _role_denied("admin", "data_manager", "operator")
        if denied:
            return denied
        resolve(location_id=location_id)
        try:
            options = validate_options(request.get_json(silent=True))
            location = store.get_location(location_id)
            reason = readiness(location)
            node = next(n for n in store.list_nodes() if n["node_id"] == location["node_id"])
            selected = next(
                item
                for item in caption_scheme_status(node.get("capabilities") or {}, reason, _current_user())
                if item["id"] == options["scheme"]
            )
            if not selected["can_submit"]:
                return jsonify(error=selected["unavailable_reason"]), 409
            user = _current_user()
            # Scope idempotency to the owner, dataset, and exact requested operation.
            delivery = request.headers.get("Idempotency-Key")
            if not delivery or len(delivery) > 128:
                return jsonify(error="An Idempotency-Key (1–128 characters) is required"), 400
            identity = [user["user_id"], location_id, OPERATION, options, delivery]
            job = store.create_job(
                location_id=location_id,
                requested_by=user["user_id"],
                operation=OPERATION,
                options=options,
                idempotency_key=hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(job=store.job_manager.decorate(job, user)), 202

    @app.put("/api/agents/jobs/<job_id>/caption-artifacts/<filename>")
    def upload_caption(job_id, filename):
        # The control-plane execution guard authenticates node, lease and attempt before this handler.
        job = store.get_job(job_id)
        if job["operation"] != OPERATION or job["status"] != "running":
            abort(409)
        if filename not in FILES:
            abort(400)
        folder = staged(job)
        folder.mkdir(parents=True, exist_ok=True)
        limit = 256 * 1024 * 1024 if filename == "video.mp4" else 16 * 1024 * 1024
        if request.content_length is not None and request.content_length > limit:
            abort(413)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
                temporary = Path(handle.name)
                count = 0
                while chunk := request.stream.read(1024 * 1024):
                    count += len(chunk)
                    if count > limit:
                        abort(413)
                    handle.write(chunk)
            os.replace(temporary, folder / filename)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return jsonify(uploaded=filename)

    def complete_caption(job, result):
        location = store.get_location(job["location_id"])
        try:
            publish_run(
                staged(job),
                job_root(job),
                location["dataset_key"],
                job["job_id"],
                options=validate_options(job["options"]),
            )
        except (KeyError, TypeError, OSError) as exc:
            raise ValueError("Missing or invalid caption artifacts") from exc
        return {
            "episode_index": job["options"]["episode_index"],
            "scheme": job["options"]["scheme"],
            "review_url": f"/remote/{job['location_id']}/temporal-caption"
            f"?scheme={job['options']['scheme']}&episode={job['options']['episode_index']}"
            f"&run={job['job_id']}",
            "run": job["job_id"],
        }

    @app.route("/api/temporal-caption/<dataset_namespace>/<dataset_name>/jobs", methods=["GET", "POST"])
    def local_caption_jobs(dataset_namespace, dataset_name):
        from lerobot.data_platform.local_execution import agent_capabilities_for_local, enqueue_local

        key = f"{dataset_namespace}/{dataset_name}"
        try:
            dataset, _ = ctx.ensure_dataset_loaded(ctx.repo_key(key))
            validate_source(json.loads((Path(dataset.root) / "meta" / "info.json").read_text()))
            capabilities = agent_capabilities_for_local()
            ready = capabilities.get("caption_configured", False)
            user = _current_user()
            if request.method == "GET":
                locations = {
                    item["location_id"]
                    for item in store.list_locations()
                    if item["dataset_key"] == key and (item.get("metadata") or {}).get("local_execution")
                }
                return jsonify(
                    schemes=caption_scheme_status(
                        capabilities,
                        None if ready else "Configure the model credential in the local executor environment",
                        user,
                    ),
                    can_import=False,
                    can_submit=bool(user and user["role"] in {"operator", "data_manager", "admin"} and ready),
                    unavailable_reason=None
                    if ready
                    else "Configure the model credential in the local executor environment",
                    jobs=[
                        store.job_manager.decorate(job, user)
                        for job in store.list_jobs(limit=200, actor=user)
                        if job["location_id"] in locations and job["operation"] == OPERATION
                    ],
                )
            denied = _role_denied("operator", "data_manager", "admin")
            if denied:
                return denied
            if not ready:
                return jsonify(error="Local annotation backend is unavailable"), 409
            options = validate_options(request.get_json(silent=True))
            delivery = request.headers.get("Idempotency-Key")
            if not delivery or len(delivery) > 128:
                return jsonify(error="An Idempotency-Key (1–128 characters) is required"), 400
            job = enqueue_local(
                app,
                ctx,
                {"id": str(uuid.uuid4()), "dataset_key": key},
                command={"operation": OPERATION, "options": options},
                idempotency_key=hashlib.sha256(
                    json.dumps([user["user_id"], key, OPERATION, options, delivery], sort_keys=True).encode()
                ).hexdigest(),
            )
            return jsonify(job=store.job_manager.decorate(job, user)), 202
        except (ValueError, KeyError, TypeError) as exc:
            return jsonify(error=str(exc)), 400

    app.extensions["data_platform_complete_caption"] = complete_caption
    app.extensions["data_platform_cleanup_caption"] = lambda job: shutil.rmtree(
        job_root(job) / ".jobs" / job["job_id"], ignore_errors=True
    )

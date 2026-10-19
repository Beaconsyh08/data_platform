"""Machine-authenticated source-loss proofs and durable cache-cleanup acknowledgements."""

from pathlib import Path

from flask import jsonify, request

from lerobot.data_platform.source_cleanup import SourceCleanupManager
from lerobot.data_platform.source_cleanup_files import prune_server_job_cache, prune_server_location_cache


def register_source_cleanup_routes(app, ctx):
    store = ctx.control_plane_store
    ledger = ctx.lifecycle_store() if callable(ctx.lifecycle_store) else ctx.lifecycle_store
    manager = SourceCleanupManager(store, Path(ctx.remote_cache_root))
    app.extensions["data_platform_source_cleanup"] = manager

    def node():
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        value = store.authenticate_node(token)
        if not value:
            raise PermissionError("Invalid Agent token")
        import os

        expected = os.environ.get("DATA_PLATFORM_RELEASE")
        if expected and (
            value["capabilities"].get("release") != expected
            or request.headers.get("X-Agent-Release") != expected
        ):
            raise PermissionError("This executor is draining")
        if value["capabilities"].get("source_cleanup_protocol") != 1:
            raise PermissionError("Upgrade this Agent for source cleanup")
        return value

    def checked(function):
        from functools import wraps

        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except PermissionError as exc:
                return jsonify(error=str(exc)), 403
            except KeyError:
                return jsonify(error="Source cleanup was not found"), 404
            except (ValueError, TypeError) as exc:
                return jsonify(error=str(exc)), 409

        return wrapped

    @app.get("/api/agents/source-cleanup")
    @checked
    def source_cleanup_poll():
        return jsonify(manager.poll(node(), request.args.get("cursor", "")))

    @app.post("/api/agents/source-cleanup/presence")
    @checked
    def source_cleanup_presence():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or request.content_length and request.content_length > 8192:
            raise ValueError("Expected a bounded source-presence result")
        return jsonify(manager.report(node(), body))

    @app.get("/api/agents/source-cleanup/<token>/jobs")
    @checked
    def source_cleanup_jobs(token):
        return jsonify(manager.jobs(node()["node_id"], token, request.args.get("cursor", "")))

    @app.post("/api/agents/source-cleanup/<token>/ack")
    @checked
    def source_cleanup_ack(token):
        agent = node()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or request.content_length and request.content_length > 8192:
            raise ValueError("Expected a bounded cleanup acknowledgement")
        if body.get("status") == "error":
            manager.error(agent["node_id"], token, body.get("error", "Cleanup failed"))
            return jsonify(status="retry")
        if body.get("status") != "done":
            raise ValueError("Invalid cleanup acknowledgement")
        lease = manager.begin_ack(agent["node_id"], token)
        if lease is None:
            return jsonify(status="done")
        if "owner" not in lease:
            return jsonify(lease)
        try:
            batch = manager.jobs(agent["node_id"], token, lease["cursor"])
            cache_root = Path(ctx.remote_cache_root)
            prune_server_job_cache(cache_root, ledger.root, batch["job_ids"])
            cleanup_caption = app.extensions.get("data_platform_cleanup_caption")
            if cleanup_caption:
                for job_id in batch["job_ids"]:
                    cleanup_caption(store.get_job(job_id))
            if not batch["next_cursor"]:
                prune_server_location_cache(cache_root, lease["location"]["location_id"])
                if ctx.unregister_missing_dataset:
                    ctx.unregister_missing_dataset(lease["location"])
            result = manager.finish_ack(agent["node_id"], token, lease["owner"], batch["next_cursor"])
            return jsonify(result)
        except Exception as exc:
            manager.error(agent["node_id"], token, exc)
            raise

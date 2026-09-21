"""Authenticated sidecar replication from the web cache to its owning Agent."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path

from flask import g, jsonify, request

from lerobot.data_platform.dataset_results import dataset_signature, result_lock, result_path, sync_status
from lerobot.data_platform.execution import atomic_json
from lerobot.data_platform.operation_log import append_operation_event


def register_dataset_result_routes(app, store, remote_cache_root: Path):
    def cache_for(location):
        cache = Path((location.get("metadata") or {}).get("cache_root") or "")
        if not (location.get("metadata") or {}).get("viewer_ready"):
            raise ValueError("Dataset viewer is not prepared")
        if not cache.resolve().is_relative_to(remote_cache_root.resolve()):
            raise ValueError("Dataset results are outside the configured cache root")
        return cache

    def node_location(location_id):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        node = store.authenticate_node(token)
        if not node:
            return None, (jsonify(error="invalid node token"), 401)
        try:
            location = store.get_location(location_id)
            if location["node_id"] != node["node_id"]:
                raise KeyError(location_id)
            cache_for(location)
        except (KeyError, ValueError):
            return None, (jsonify(error="dataset results not found for this node"), 404)
        return location, None

    def request_location():
        args = request.view_args or {}
        key = request.args.get("dataset_key")
        if args.get("dataset_namespace") and args.get("dataset_name"):
            key = f"{args['dataset_namespace']}/{args['dataset_name']}"
        if not key and request.is_json:
            body = request.get_json(silent=True)
            key = body.get("dataset_key") if isinstance(body, dict) else None
        if not key:
            return None
        return next(
            (
                item
                for item in store.list_locations()
                if item["dataset_key"] == key and (item.get("metadata") or {}).get("viewer_ready")
            ),
            None,
        )

    @app.before_request
    def lock_dataset_results():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or request.path.startswith(
            "/api/agents/"
        ):
            return
        location = request_location()
        if location:
            cache = cache_for(location)
            lock = result_lock(cache)
            lock.__enter__()
            g.dataset_result_lock = lock
            g.dataset_result_cache = cache

    @app.after_request
    def record_dataset_results(response):
        cache = getattr(g, "dataset_result_cache", None)
        if cache is not None and response.status_code < 400:
            try:
                _, state = sync_status(cache)
            except (OSError, ValueError):
                logging.exception("Could not snapshot dataset results")
                state = {"state": "error", "error": "Results saved; synchronization needs attention"}
            if response.is_json:
                body = response.get_json()
                if isinstance(body, dict):
                    body["result_sync"] = state
                    response.set_data(app.json.dumps(body))
        return response

    @app.teardown_request
    def unlock_dataset_results(error):
        lock = g.pop("dataset_result_lock", None)
        if lock is not None:
            lock.__exit__(None, None, None)

    @app.get("/api/dataset-results/status")
    def dataset_result_status():
        location = request_location()
        if location is None:
            return jsonify(state="local")
        try:
            cache = cache_for(location)
            with result_lock(cache):
                _, state = sync_status(cache)
            node = next((item for item in store.list_nodes() if item["node_id"] == location["node_id"]), {})
            if node.get("capabilities", {}).get("result_sync_protocol", 0) < 1:
                state = {**state, "state": "unsupported", "error": "Upgrade the Agent to synchronize results"}
            return jsonify(**state, location_id=location["location_id"])
        except (OSError, ValueError):
            return jsonify(state="error", error="Could not read dataset results"), 503

    @app.get("/api/agents/dataset-results")
    def agent_dataset_results():
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        node = store.authenticate_node(token)
        if not node:
            return jsonify(error="invalid node token"), 401
        locations = []
        for location in store.list_locations():
            if location["node_id"] != node["node_id"] or not (location.get("metadata") or {}).get(
                "viewer_ready"
            ):
                continue
            try:
                cache = cache_for(location)
                with result_lock(cache):
                    snapshot, state = sync_status(cache)
                    manifest = json.loads((cache / "static/viewer_manifest.json").read_text())
                locations.append(
                    {
                        "location_id": location["location_id"],
                        "root": location["root"],
                        "output_dir": location["output_dir"],
                        "revision": snapshot.revision,
                        "files": snapshot.files,
                        "dataset": dataset_signature(manifest),
                        "state": state["state"],
                    }
                )
            except (OSError, ValueError):
                logging.exception("Could not snapshot results for %s", location["location_id"])
        return jsonify(locations=locations)

    @app.get("/api/agents/locations/<location_id>/results")
    def agent_result_bundle(location_id):
        location, error = node_location(location_id)
        if error:
            return error
        cache = cache_for(location)
        with result_lock(cache):
            snapshot, _ = sync_status(cache)
            contents = {}
            for name, expected in snapshot.files.items():
                content = result_path(cache / "static", name).read_bytes()
                if hashlib.sha256(content).hexdigest() != expected["sha256"]:
                    return jsonify(error="Results changed; retry synchronization"), 409
                contents[name] = base64.b64encode(content).decode("ascii")
            manifest = json.loads((cache / "static/viewer_manifest.json").read_text())
        return jsonify(
            revision=snapshot.revision,
            files=snapshot.files,
            contents=contents,
            dataset=dataset_signature(manifest),
        )

    @app.post("/api/agents/locations/<location_id>/results/ack")
    def agent_result_ack(location_id):
        location, error = node_location(location_id)
        if error:
            return error
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="JSON object required"), 400
        cache = cache_for(location)
        with result_lock(cache):
            snapshot, state = sync_status(cache)
            if body.get("revision") != snapshot.revision:
                return jsonify(error="Results changed; retry synchronization"), 409
            state.update(state="error" if body.get("error") else "synced", updated_at=time.time())
            if body.get("error"):
                state["error"] = "Agent could not save results; synchronization will retry"
            else:
                state.pop("error", None)
            atomic_json(cache / ".result-sync.json", state)
            append_operation_event(
                cache / "static",
                "dataset.results.sync",
                status=state["state"],
                source="agent",
                dataset_keys=[location["dataset_key"]],
                dataset_roots=[location["root"]],
                details={"revision": snapshot.revision, "output_dir": location["output_dir"]},
            )
        return jsonify(state)

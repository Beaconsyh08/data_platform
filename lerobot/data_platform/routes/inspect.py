"""Read-only dataset inspection pages and APIs, independent of Viewer preparation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from urllib.parse import urlencode

import pyarrow as pa
from flask import Response, jsonify, render_template, request

from lerobot.data_platform.precompute.inspection import (
    MAX_RESPONSE_BYTES,
    DatasetInspection,
    contained_path,
    read_inspection_json,
)


def register_inspect_routes(app, ctx) -> None:
    def local(namespace, name):
        key = (namespace, name)
        entry = ctx.datasets_index.get(key)
        if entry is None:
            raise FileNotFoundError("Dataset is not registered")
        if entry.get("internal_remote_cache"):
            raise ValueError("Open remote inspection using its registered location")
        root = Path(entry["root"]).expanduser()
        static_dir = ctx.static_dir_for_key(key) if ctx.static_dir_for_key else None
        if (root / "meta/info.json").is_file():
            return DatasetInspection.local(root, static_dir)
        if static_dir is None:
            raise FileNotFoundError("Dataset metadata is missing")
        manifest = read_inspection_json(static_dir, static_dir / "viewer_manifest.json")
        return DatasetInspection(
            None,
            static_dir,
            manifest,
            {int(row["episode_index"]): row for row in manifest.get("episodes", [])},
        )

    def remote(location_id):
        location = ctx.control_plane_store.get_location(location_id)
        report = location.get("metadata") or {}
        if ctx.remote_cache_root is None:
            raise FileNotFoundError("Remote cache storage is not configured")
        base = Path(ctx.remote_cache_root).resolve()
        expected = base / location_id
        if contained_path(base, expected) != expected:
            raise PermissionError("Published cache may not redirect to another location")
        recorded = Path(report.get("cache_root") or expected)
        if recorded.resolve() != expected:
            raise PermissionError("Inspection requires this location's published cache")
        static_dir = contained_path(expected, expected / "static")
        manifest_path = static_dir / "viewer_manifest.json"
        manifest = read_inspection_json(static_dir, manifest_path) if manifest_path.is_file() else {}
        if manifest.get("repo_id") and manifest["repo_id"] != location["dataset_key"]:
            raise PermissionError("Published cache identity does not match this location")
        episodes = {
            int(row["episode_index"]): row
            for row in [*(report.get("episodes") or []), *(manifest.get("episodes") or [])]
        }
        node = next(
            (item for item in ctx.control_plane_store.list_nodes() if item["node_id"] == location["node_id"]),
            {},
        )
        return DatasetInspection(
            None,
            static_dir,
            {**report, **manifest},
            episodes,
            {
                "location_id": location_id,
                "state": location.get("state"),
                "agent_status": node.get("status", "unknown"),
                "last_synced_at": location.get("last_seen_at"),
                "viewer_ready": bool(report.get("viewer_ready")),
            },
        )

    def integer(name, default=None):
        value = request.args.get(name)
        if value is None:
            return default
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if number < 0:
            raise ValueError(f"{name} must be nonnegative")
        return number

    def response(inspection, kind):
        source = request.args.get("source", "csv" if inspection.root is None else "parquet")
        episode = integer("episode_index")
        if kind == "schema":
            payload = inspection.schema(source, episode)
        else:
            if episode is None:
                raise ValueError("episode_index is required")
            fields = request.args.getlist("columns") if "columns" in request.args else None
            payload = inspection.rows(source, episode, integer("offset", 0), integer("limit", 50), fields)
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(body.encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("Inspection response exceeds 2 MiB. Select fewer fields.")
        return Response(body, mimetype="application/json", headers={"Cache-Control": "private, no-store"})

    def handle(factory, kind):
        try:
            return response(factory(), kind)
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        except (FileNotFoundError, KeyError):
            return jsonify(
                error="Dataset, episode or published CSV is missing. Open Cache to prepare CSV."
            ), 404
        except (ValueError, TypeError, csv.Error, pa.ArrowInvalid) as exc:
            return jsonify(error=str(exc)), 400

    def page(dataset_key, api_base, home_url, *, remote_page=False, cache_only=False):
        return render_template(
            "visualize_dataset_inspect.html",
            dataset_key=dataset_key,
            inspect_api_base=api_base,
            inspect_cache_only=remote_page or cache_only,
            inspect_remote=remote_page,
            home_url=home_url,
            cache_url=home_url + ("&" if "?" in home_url else "?") + "page=preprocessing&tab=cache",
        )

    @app.get("/<dataset_namespace>/<dataset_name>/inspect")
    def dataset_inspect_page(dataset_namespace, dataset_name):
        entry = ctx.datasets_index.get((dataset_namespace, dataset_name))
        if entry is None:
            return jsonify(error="Dataset is not registered"), 404
        if entry.get("internal_remote_cache"):
            return jsonify(error="Use this dataset's remote location inspection page"), 400
        key = ctx.repo_id_from_key((dataset_namespace, dataset_name))
        return page(
            key,
            f"/api/datasets/{key}/inspect",
            "/?" + urlencode({"select": key}),
            cache_only=not (Path(entry["root"]).expanduser() / "meta/info.json").is_file(),
        )

    @app.get("/api/datasets/<dataset_namespace>/<dataset_name>/inspect/schema")
    def dataset_inspect_schema(dataset_namespace, dataset_name):
        return handle(lambda: local(dataset_namespace, dataset_name), "schema")

    @app.get("/api/datasets/<dataset_namespace>/<dataset_name>/inspect/rows")
    def dataset_inspect_rows(dataset_namespace, dataset_name):
        return handle(lambda: local(dataset_namespace, dataset_name), "rows")

    if ctx.control_plane_store is not None:

        @app.get("/remote/<location_id>/inspect")
        def remote_inspect_page(location_id):
            try:
                location = ctx.control_plane_store.get_location(location_id)
            except KeyError:
                return jsonify(error="Remote dataset location is not registered"), 404
            return page(
                location["dataset_key"],
                f"/api/control/locations/{location_id}/inspect",
                "/?" + urlencode({"select": "remote:" + location_id}),
                remote_page=True,
            )

        @app.get("/api/control/locations/<location_id>/inspect/schema")
        def remote_inspect_schema(location_id):
            return handle(lambda: remote(location_id), "schema")

        @app.get("/api/control/locations/<location_id>/inspect/rows")
        def remote_inspect_rows(location_id):
            return handle(lambda: remote(location_id), "rows")

"""Temporal caption review and jobs, also available before remote Viewer preparation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlencode

from flask import Response, abort, jsonify, render_template, request, send_file, stream_with_context
from werkzeug.http import parse_range_header

from lerobot.data_platform.caption_reads import VIDEO_CHUNK_BYTES
from lerobot.data_platform.temporal_caption import artifact_key, validate_result
from lerobot.data_platform.temporal_caption_demo import REVIEW_SCHEMES, SCHEMES, review_context, scheme_info


def register_temporal_caption_routes(app, ctx) -> None:
    if ctx.control_plane_store is not None:
        from lerobot.data_platform.routes.caption_reads import register_caption_read_routes

        register_caption_read_routes(app, ctx)

    def source_location(kwargs):
        location_id = kwargs.get("location_id")
        if not location_id or not getattr(ctx, "caption_read_relay", None):
            return None
        location = ctx.control_plane_store.get_location(location_id)
        node = next(
            item for item in ctx.control_plane_store.list_nodes() if item["node_id"] == location["node_id"]
        )
        if node.get("capabilities", {}).get("caption_read_protocol") != 1:
            return None
        return {**location, "caption_agent_online": node["status"] == "online"}

    def source_read(location, action, **options):
        if not location["caption_agent_online"]:
            abort(503, description="Dataset Agent is offline")
        try:
            reply = ctx.caption_read_relay.request(
                location["node_id"],
                {
                    "root": location["root"],
                    "dataset_key": location["dataset_key"],
                    "action": action,
                    **options,
                },
                timeout=210 if action == "video-info" else None,
            )
        except TimeoutError as exc:
            abort(503, description=str(exc))
        if reply.status != 200:
            abort(reply.status, description="Unable to read the dataset caption result")
        return reply

    def source_json(location, action, **options):
        reply = source_read(location, action, **options)
        if reply.binary:
            abort(502, description="Invalid caption Agent reply")
        try:
            value = json.loads(reply.body)
            if not isinstance(value, dict):
                raise ValueError("Expected a caption object")
            return value
        except ValueError:
            abort(502, description="Invalid caption Agent reply")

    def resolve(dataset_namespace=None, dataset_name=None, location_id=None):
        if location_id is not None:
            if ctx.control_plane_store is None:
                abort(404)
            try:
                location = ctx.control_plane_store.get_location(location_id)
            except KeyError:
                abort(404)
            key = location["dataset_key"]
            name = Path(location["root"]).name
            api = f"/api/control/locations/{location_id}/temporal-caption"
            home = "/?" + urlencode(
                {"select": f"remote:{location_id}", "page": "annotation", "tab": "temporal_caption"}
            )
        else:
            key = f"{dataset_namespace}/{dataset_name}"
            if ctx.static_dir_for_key(ctx.repo_key(key)) is None:
                abort(404)
            name = dataset_name
            api = f"/api/temporal-caption/{key}"
            home = f"/?select={key}&page=annotation&tab=temporal_caption"
        configured = os.environ.get("DATA_PLATFORM_TEMPORAL_CAPTION_ROOT")
        if configured:
            root = Path(configured)
        else:
            store = ctx.lifecycle_store() if callable(ctx.lifecycle_store) else ctx.lifecycle_store
            root = store.root / "temporal_captions"
        return key, name, root / artifact_key(key), api, home

    def read_artifact(root, run, variant, key):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run) or variant not in {
            "coarse",
            "refined",
            "caption",
            "fusion_draft",
            "fusion_reviewed",
        }:
            abort(404)
        path = root / run / f"{variant}.json"
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            abort(404)
        try:
            artifact = json.loads(path.read_text())
            if artifact["dataset_key"] != key or artifact["variant"] != variant:
                abort(404)
            validate_result(artifact["result"], artifact["frame_count"])
        except (ValueError, KeyError, TypeError):
            abort(422, description="Invalid temporal caption artifact")
        return artifact

    def caption_page(review=False, **kwargs):
        _, name, _, api, home = resolve(**kwargs)
        base = (
            f"/remote/{kwargs['location_id']}/temporal-caption"
            if kwargs.get("location_id")
            else f"/{kwargs['dataset_namespace']}/{kwargs['dataset_name']}/temporal-caption"
        )
        return render_template(
            "visualize_dataset_temporal_caption.html"
            if review
            else "visualize_dataset_temporal_caption_annotation.html",
            page_kind="Temporal Caption Review" if review else "Data Curation / Annotation",
            page_title=name,
            caption_schemes=list((REVIEW_SCHEMES if review else SCHEMES).values()),
            api_base=api,
            home_url=home,
            annotation_url=base,
            review_url=base + "/review",
            remote_caption=ctx.control_plane_store is not None,
        )

    @app.get("/<dataset_namespace>/<dataset_name>/temporal-caption")
    @app.get("/remote/<location_id>/temporal-caption")
    def temporal_caption_page(**kwargs):
        return caption_page(**kwargs)

    @app.get("/<dataset_namespace>/<dataset_name>/temporal-caption/review")
    @app.get("/remote/<location_id>/temporal-caption/review")
    def temporal_caption_review_page(**kwargs):
        return caption_page(review=True, **kwargs)

    @app.get("/api/temporal-caption/<dataset_namespace>/<dataset_name>")
    @app.get("/api/control/locations/<location_id>/temporal-caption")
    def temporal_caption_runs(**kwargs):
        key, _, root, _, _ = resolve(**kwargs)
        runs = []
        location = source_location(kwargs)
        source = {"runs": [], "warnings": []}
        if location:
            if location["caption_agent_online"]:
                source = source_json(location, "runs")
            else:
                source["warnings"].append("Dataset Agent is offline; dataset-local results are unavailable.")
        source_hashes = {run["source_caption_sha256"] for run in source["runs"]}
        if root.is_dir():
            for run in sorted(root.iterdir(), reverse=True):
                if not run.is_dir() or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run.name):
                    continue
                for variant in ("coarse", "refined", "caption", "fusion_draft", "fusion_reviewed"):
                    if not (run / f"{variant}.json").is_file():
                        continue
                    artifact = read_artifact(root, run.name, variant, key)
                    if artifact.get("import_provenance", {}).get("caption_sha256") in source_hashes:
                        continue
                    runs.append(
                        {
                            "run": run.name,
                            "scheme": scheme_info(artifact),
                            **{
                                field: artifact[field]
                                for field in (
                                    "variant",
                                    "episode_index",
                                    "model",
                                    "created_at",
                                )
                            },
                        }
                    )
        return jsonify(
            runs=[*source["runs"], *runs], schemes=list(SCHEMES.values()), warnings=source["warnings"]
        )

    @app.get("/api/temporal-caption/<dataset_namespace>/<dataset_name>/<run>/<variant>")
    @app.get("/api/control/locations/<location_id>/temporal-caption/<run>/<variant>")
    def temporal_caption_result(run, variant, **kwargs):
        key, _, root, api, _ = resolve(**kwargs)
        location = source_location(kwargs)
        if location and re.fullmatch(r"source-[0-9a-f]{24}", run) and variant == "caption":
            artifact = source_json(location, "result", run=run)
            return jsonify(**artifact, video_url=f"{api}/{run}/{variant}/video")
        artifact = read_artifact(root, run, variant, key)
        return jsonify(
            **artifact,
            scheme=scheme_info(artifact),
            context=review_context(root / run),
            video_url=f"{api}/{run}/{variant}/video",
        )

    @app.get("/api/temporal-caption/<dataset_namespace>/<dataset_name>/<run>/<variant>/video")
    @app.get("/api/control/locations/<location_id>/temporal-caption/<run>/<variant>/video")
    def temporal_caption_video(run, variant, **kwargs):
        key, _, root, _, _ = resolve(**kwargs)
        location = source_location(kwargs)
        if location and re.fullmatch(r"source-[0-9a-f]{24}", run) and variant == "caption":
            info = source_json(location, "video-info", run=run)
            size, revision = info["size"], info["revision"]
            start, end, status = 0, size, 200
            headers = {
                "Accept-Ranges": "bytes",
                "ETag": f'"{revision}"',
                "Cache-Control": "private, no-store",
            }
            range_value = request.headers.get("Range")
            if range_value and request.headers.get("If-Range", headers["ETag"]) == headers["ETag"]:
                selected = parse_range_header(range_value)
                interval = selected.range_for_length(size) if selected and selected.units == "bytes" else None
                if interval is None:
                    return Response(status=416, headers={**headers, "Content-Range": f"bytes */{size}"})
                start, end = interval
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
            headers["Content-Length"] = str(end - start)
            if request.method == "HEAD":
                return Response(status=status, headers=headers, mimetype="video/mp4")

            def chunk(offset):
                reply = source_read(
                    location,
                    "video-chunk",
                    run=run,
                    start=offset,
                    length=min(VIDEO_CHUNK_BYTES, end - offset),
                    revision=revision,
                )
                if not reply.binary or len(reply.body) != min(VIDEO_CHUNK_BYTES, end - offset):
                    abort(502, description="Incomplete caption video reply")
                return reply.body

            first = chunk(start)

            def stream():
                yield first
                for offset in range(start + len(first), end, VIDEO_CHUNK_BYTES):
                    yield chunk(offset)

            return Response(
                stream_with_context(stream()), status=status, headers=headers, mimetype="video/mp4"
            )
        artifact = read_artifact(root, run, variant, key)
        path = root / run / "video.mp4"
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            abort(404)
        from lerobot.data_platform.camera_layout import ordered_review_video

        metadata = root / run / "episode.json"
        cameras = json.loads(metadata.read_text()).get("cameras", []) if metadata.exists() else []
        if not cameras and artifact.get("source_frame_convention") == "inclusive":
            # Legacy caption_demo exports used labeled head/left/right horizontal tiles.
            cameras = ["head", "left_wrist", "right_wrist"]
        path = ordered_review_video(path, cameras, root.parent / ".display-videos")
        return send_file(path, mimetype="video/mp4", conditional=True)

    from lerobot.data_platform.routes.temporal_caption_jobs import register_caption_job_routes

    register_caption_job_routes(app, ctx, resolve)

"""Temporal caption review and jobs, also available before remote Viewer preparation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from flask import abort, jsonify, render_template, send_file

from lerobot.data_platform.temporal_caption import artifact_key, validate_result
from lerobot.data_platform.temporal_caption_demo import review_context, scheme_info


def register_temporal_caption_routes(app, ctx) -> None:
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
            home = f"/?remote_location_id={location_id}&page=annotation&tab=temporal_caption"
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

    @app.get("/<dataset_namespace>/<dataset_name>/temporal-caption")
    @app.get("/remote/<location_id>/temporal-caption")
    def temporal_caption_page(**kwargs):
        _, name, _, api, home = resolve(**kwargs)
        return render_template(
            "visualize_dataset_temporal_caption.html",
            page_kind="Data Curation / Annotation",
            page_title=name,
            page_subtitle="Temporal Caption · single-episode experiment",
            api_base=api,
            home_url=home,
            remote_caption=ctx.control_plane_store is not None,
        )

    @app.get("/api/temporal-caption/<dataset_namespace>/<dataset_name>")
    @app.get("/api/control/locations/<location_id>/temporal-caption")
    def temporal_caption_runs(**kwargs):
        key, _, root, _, _ = resolve(**kwargs)
        runs = []
        if root.is_dir():
            for run in sorted(root.iterdir(), reverse=True):
                if not run.is_dir() or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run.name):
                    continue
                for variant in ("coarse", "refined", "caption", "fusion_draft", "fusion_reviewed"):
                    if not (run / f"{variant}.json").is_file():
                        continue
                    artifact = read_artifact(root, run.name, variant, key)
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
        return jsonify(runs=runs)

    @app.get("/api/temporal-caption/<dataset_namespace>/<dataset_name>/<run>/<variant>")
    @app.get("/api/control/locations/<location_id>/temporal-caption/<run>/<variant>")
    def temporal_caption_result(run, variant, **kwargs):
        key, _, root, api, _ = resolve(**kwargs)
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
        read_artifact(root, run, variant, key)
        path = root / run / "video.mp4"
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            abort(404)
        return send_file(path, mimetype="video/mp4", conditional=True)

    from lerobot.data_platform.routes.temporal_caption_jobs import register_caption_job_routes

    register_caption_job_routes(app, ctx, resolve)

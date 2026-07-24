import logging
import threading
import time
import uuid
from pathlib import Path

from flask import jsonify, render_template, request

from lerobot.data_platform.precompute.embedding import (
    get_capabilities as get_embedding_capabilities,
    load_points as load_embedding_points,
    load_source as load_embedding_source,
    project_existing_embeddings,
    reducer_capabilities,
    run_embedding,
)
from lerobot.data_platform.routes.context import RouteContext


def register_embedding_routes(app, ctx: RouteContext) -> None:
    @app.route("/api/embedding/capabilities")
    def api_embedding_capabilities():
        return jsonify({**get_embedding_capabilities(), **reducer_capabilities()})

    @app.route("/api/embedding/start", methods=["POST"])
    def api_start_embedding():
        body = request.get_json(silent=True) or {}
        try:
            dataset_key = ctx.dataset_key_from_body(body)
            dataset_obj, ds_static = ctx.ensure_dataset_loaded(dataset_key)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except KeyError:
            return jsonify({"error": "dataset is not registered"}), 404
        except Exception as exc:
            logging.exception("Failed to load dataset for embedding")
            return jsonify({"error": str(exc)}), 400

        options = body.get("options") or body
        ckpt_value = str(options.get("ckpt_path") or options.get("embed_policy") or "").strip()
        if not ckpt_value:
            return jsonify({"error": "ckpt_path is required"}), 400
        selected_episodes = ctx.parse_int_list(options.get("episodes"))
        layer_hook = str(options.get("layer_hook") or options.get("embed_layer") or "pi_prefix")
        openpi_config = str(options.get("openpi_config") or options.get("embed_config") or "").strip() or None
        workers = int(options.get("workers") or options.get("embed_workers") or 0) or None
        devices = str(options.get("devices") or options.get("embed_devices") or "").strip() or None
        refit = ctx.bool_option(options, "refit", False)
        repo_id = ctx.repo_id_from_key(dataset_key)
        total = len(selected_episodes) if selected_episodes is not None else len(ctx.dataset_episode_ids(dataset_obj, dataset_key))
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        job = {
            "id": job_id,
            "job_type": "embedding",
            "dataset_key": repo_id,
            "status": "queued",
            "progress": 0,
            "current": 0,
            "total": total,
            "message": "Queued",
            "error": None,
            "review_url": f"/{repo_id}/embedding",
            "viewer_url": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "logs": [],
        }
        with ctx.jobs_lock:
            ctx.jobs_registry[job_id] = job

        def _run_job() -> None:
            try:
                ctx.update_job(job, {"status": "running", "message": "Starting embedding"})
                result = run_embedding(
                    root=dataset_obj.root,
                    meta=dataset_obj.meta,
                    episodes=selected_episodes,
                    static_dir=ds_static,
                    ckpt_path=Path(ckpt_value).expanduser(),
                    layer_hook=layer_hook,
                    openpi_config=openpi_config,
                    workers=workers,
                    devices=devices,
                    refit=refit,
                    progress_callback=lambda payload: ctx.update_job(job, payload),
                )
                ctx.finish_job(
                    job,
                    "Embedding complete",
                    current=result.points,
                    total=result.points,
                )
            except Exception as exc:
                logging.exception("Embedding job failed")
                ctx.fail_job(job, "Embedding failed", exc)

        threading.Thread(target=_run_job, name=f"embedding-{job_id}", daemon=True).start()
        return jsonify({"job": ctx.serialize_job(job)})

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/embedding")
    def show_embedding(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        dataset_obj, ds_static = ctx.get_ctx(dataset_namespace, dataset_name)
        repo_id = ctx.repo_id_from_key(dataset_key)
        episode_ids = ctx.dataset_episode_ids(dataset_obj, dataset_key)
        first_episode = episode_ids[0] if episode_ids else 0
        return render_template(
            "visualize_dataset_embedding.html",
            dataset_namespace=dataset_namespace,
            dataset_name=dataset_name,
            dataset_key=repo_id,
            image_key=(ctx.dataset_image_keys(dataset_obj) or [""])[0],
            viewer_url=f"/{repo_id}/episode_{first_episode}",
            **ctx.dataset_nav(repo_id, first_episode, "embedding", dataset_obj, ds_static),
        )

    @app.route("/api/embedding/<string:dataset_namespace>/<string:dataset_name>/points")
    def api_embedding_points(dataset_namespace, dataset_name):
        dataset_obj, ds_static = ctx.get_ctx(dataset_namespace, dataset_name)
        return jsonify({"points": load_embedding_points(ds_static, dataset_obj.meta), "source": load_embedding_source(ds_static)})

    @app.route("/api/embedding/<string:dataset_namespace>/<string:dataset_name>/project", methods=["POST"])
    def api_embedding_project(dataset_namespace, dataset_name):
        dataset_obj, ds_static = ctx.get_ctx(dataset_namespace, dataset_name)
        body = request.get_json(silent=True) or {}
        options = body.get("options") or body
        try:
            projection = project_existing_embeddings(
                ds_static,
                method=str(options.get("method") or "auto"),
                seed=int(options.get("seed") or 42),
                n_neighbors=int(options.get("n_neighbors") or 15),
                min_dist=float(options.get("min_dist") if options.get("min_dist") is not None else 0.1),
                metric=str(options.get("metric") or "euclidean"),
            )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            logging.exception("Embedding projection failed")
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "projection": projection,
                "points": load_embedding_points(ds_static, dataset_obj.meta),
                "source": load_embedding_source(ds_static),
            }
        )

    @app.route("/api/embedding/<string:dataset_namespace>/<string:dataset_name>/episode/<int:episode_index>")
    def api_embedding_episode(dataset_namespace, dataset_name, episode_index):
        dataset_obj, ds_static = ctx.get_ctx(dataset_namespace, dataset_name)
        points = [
            point
            for point in load_embedding_points(ds_static, dataset_obj.meta)
            if int(point["episode_index"]) == int(episode_index)
        ]
        if not points:
            return jsonify({"error": "not found"}), 404
        return jsonify(points[0])

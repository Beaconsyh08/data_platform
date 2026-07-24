import logging
import threading
import time
import uuid

from flask import jsonify, render_template, request

from lerobot.data_platform.precompute.compare import load_compare_json, run_compare_build
from lerobot.data_platform.routes.context import RouteContext


def register_compare_routes(app, ctx: RouteContext) -> None:
    @app.route("/api/compare/candidates")
    def api_compare_candidates():
        return jsonify({"datasets": [ctx.serialize_dataset_light(key) for key in sorted(ctx.datasets_index)]})

    @app.route("/api/compare/start", methods=["POST"])
    def api_start_compare():
        body = request.get_json(silent=True) or {}
        try:
            key_a = ctx.dataset_key_from_body({"dataset_key": body.get("dataset_key_a") or body.get("dataset_key")})
            key_b = ctx.dataset_key_from_body({"dataset_key": body.get("dataset_key_b") or body.get("compare_with")})
            dataset_a, static_a = ctx.ensure_dataset_loaded(key_a)
            dataset_b, static_b = ctx.ensure_dataset_loaded(key_b)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except KeyError as exc:
            return jsonify({"error": f"dataset is not registered: {exc}"}), 404

        repo_id_a = ctx.repo_id_from_key(key_a)
        repo_id_b = ctx.repo_id_from_key(key_b)
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        job = {
            "id": job_id,
            "job_type": "compare",
            "dataset_key": repo_id_a,
            "status": "queued",
            "progress": 0,
            "current": 0,
            "total": 4,
            "message": "Queued",
            "error": None,
            "review_url": f"/{repo_id_a}/compare/{repo_id_b}",
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
                ctx.update_job(job, {"status": "running", "message": "Starting compare build"})
                result = run_compare_build(
                    root_a=dataset_a.root,
                    meta_a=dataset_a.meta,
                    static_a=static_a,
                    repo_id_a=repo_id_a,
                    root_b=dataset_b.root,
                    meta_b=dataset_b.meta,
                    static_b=static_b,
                    repo_id_b=repo_id_b,
                    progress_callback=lambda payload: ctx.update_job(job, payload),
                )
                ctx.finish_job(
                    job,
                    "Compare cache complete",
                    current=4,
                    total=4,
                    output_root=str(result.out_dir),
                )
            except Exception as exc:
                logging.exception("Compare job failed")
                ctx.fail_job(job, "Compare failed", exc)

        threading.Thread(target=_run_job, name=f"compare-{job_id}", daemon=True).start()
        return jsonify({"job": ctx.serialize_job(job)})

    @app.route("/<string:ns_a>/<string:name_a>/compare/<string:ns_b>/<string:name_b>")
    def show_compare(ns_a, name_a, ns_b, name_b):
        dataset_a, static_a = ctx.get_ctx(ns_a, name_a)
        ctx.get_ctx(ns_b, name_b)
        return render_template(
            "visualize_dataset_compare.html",
            dataset_a=f"{ns_a}/{name_a}",
            dataset_b=f"{ns_b}/{name_b}",
            **ctx.dataset_nav(f"{ns_a}/{name_a}", 0, "compare", dataset_a, static_a),
        )

    def _compare_json_response(ns_a, name_a, ns_b, name_b, section: str):
        _, static_a = ctx.get_ctx(ns_a, name_a)
        repo_id_b = f"{ns_b}/{name_b}"
        return jsonify(load_compare_json(static_a, repo_id_b, section))

    @app.route("/api/compare/<string:ns_a>/<string:name_a>/<string:ns_b>/<string:name_b>/summary")
    def api_compare_summary(ns_a, name_a, ns_b, name_b):
        return _compare_json_response(ns_a, name_a, ns_b, name_b, "summary")

    @app.route("/api/compare/<string:ns_a>/<string:name_a>/<string:ns_b>/<string:name_b>/stats")
    def api_compare_stats(ns_a, name_a, ns_b, name_b):
        return _compare_json_response(ns_a, name_a, ns_b, name_b, "stats")

    @app.route("/api/compare/<string:ns_a>/<string:name_a>/<string:ns_b>/<string:name_b>/overlap")
    def api_compare_overlap(ns_a, name_a, ns_b, name_b):
        return _compare_json_response(ns_a, name_a, ns_b, name_b, "overlap")

    @app.route("/api/compare/<string:ns_a>/<string:name_a>/<string:ns_b>/<string:name_b>/visual")
    def api_compare_visual(ns_a, name_a, ns_b, name_b):
        return _compare_json_response(ns_a, name_a, ns_b, name_b, "visual")

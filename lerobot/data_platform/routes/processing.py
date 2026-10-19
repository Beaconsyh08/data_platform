"""Thin authenticated endpoints for operator discovery and versioned pipelines."""

from functools import wraps

from flask import g, jsonify, render_template, request, send_file

from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.operators import get_operator, list_operators
from lerobot.data_platform.processing.planner import availability
from lerobot.data_platform.processing.service import PipelineManager
from lerobot.data_platform.routes.control_plane import _current_user


def register_processing_routes(app, ctx):
    if ctx.control_plane_store is None:
        return
    ledger = ctx.lifecycle_store() if callable(ctx.lifecycle_store) else ctx.lifecycle_store
    manager = PipelineManager(ctx.control_plane_store, ledger)
    app.extensions["data_platform_processing"] = manager

    def checked(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            actor = _current_user()
            if not actor:
                return jsonify(error="authentication required"), 401
            if request.method != "GET" and actor["role"] not in {"admin", "data_manager", "operator"}:
                return jsonify(error="Viewer accounts are read-only"), 403
            if request.content_length and request.content_length > 262144:
                return jsonify(error="Pipeline request is too large"), 413
            try:
                return function(*args, **kwargs)
            except PermissionError as exc:
                return jsonify(error=str(exc)), 403
            except KeyError:
                return jsonify(error="Requested object was not found"), 404
            except JobConflictError as exc:
                return jsonify(error=str(exc)), 409
            except (ValueError, TypeError) as exc:
                return jsonify(error=str(exc)), 400

        return wrapped

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("A JSON object is required")
        return value

    def owner():
        return _current_user()["user_id"]

    def describe(spec):
        value = spec.to_dict()
        reason = "Choose a registered dataset location"
        if request.args.get("location_id"):
            if not hasattr(g, "processing_target"):
                with manager.store.sessions() as session:
                    g.processing_target = manager._location(
                        session, request.args["location_id"], owner(), write=False
                    )
            location, node = g.processing_target
            if not hasattr(g, "processing_info"):
                g.processing_info = manager._info(location)
            info = g.processing_info
            reason = availability(spec, node, info, validate_dataset=bool(info.get("features")))
        if not spec.pipeline_allowed:
            reason = "Use the dedicated authorized operation form"
        if _current_user()["role"] == "viewer":
            reason = "Viewer accounts are read-only"
        return {**value, "available": reason is None, "reason": reason}

    @app.get("/api/operators")
    @checked
    def processing_operators():
        return jsonify(operators=[describe(spec) for spec in list_operators()])

    @app.get("/api/operators/<operator_id>")
    @checked
    def processing_operator(operator_id):
        return jsonify(operator=describe(get_operator(operator_id)))

    @app.post("/api/processing/preview")
    @checked
    def processing_preview():
        value = body()
        return jsonify(manager.preview(owner(), str(value.get("location_id") or ""), value.get("definition")))

    @app.route("/api/pipelines", methods=["GET", "POST"])
    @checked
    def processing_pipelines():
        if request.method == "GET":
            return jsonify(pipelines=manager.list(owner()))
        return jsonify(manager.save(owner(), body().get("definition"))), 201

    @app.route("/api/pipelines/<pipeline_id>", methods=["GET", "PUT"])
    @checked
    def processing_pipeline(pipeline_id):
        if request.method == "GET":
            revision = request.args.get("revision", type=int)
            return jsonify(manager.get(owner(), pipeline_id, revision))
        value = body()
        return jsonify(
            manager.save(
                owner(),
                value.get("definition"),
                pipeline_id=pipeline_id,
                expected_revision=value.get("expected_revision"),
            )
        )

    @app.post("/api/pipelines/<pipeline_id>/runs")
    @checked
    def processing_start(pipeline_id):
        value = body()
        revision = value.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("Select an immutable pipeline revision")
        return jsonify(
            manager.start(
                owner(),
                pipeline_id,
                revision,
                str(value.get("location_id") or ""),
                request.headers.get("Idempotency-Key", ""),
            )
        ), 202

    @app.get("/api/pipeline-runs")
    @checked
    def processing_runs():
        return jsonify(runs=manager.runs(owner(), request.args.get("location_id")))

    @app.get("/api/pipeline-runs/<run_id>")
    @checked
    def processing_run(run_id):
        return jsonify(manager.run(owner(), run_id))

    @app.post("/api/pipeline-runs/<run_id>/<action>")
    @checked
    def processing_action(run_id, action):
        value = body()
        result = (
            manager.decide(owner(), run_id, value)
            if action == "decisions"
            else manager.action(owner(), run_id, action, value)
        )
        return jsonify(result)

    def complete_operator(job, returned):
        import json

        from lerobot.data_platform.curation import publish_bundle

        attempt = request.headers.get("X-Job-Attempt", "")
        if not attempt or any(char not in "0123456789abcdef-" for char in attempt):
            raise ValueError("An authenticated execution attempt is required")
        staged = ledger.root / "curation" / ".jobs" / job["job_id"] / attempt
        destination = ledger.root / "operator-results" / job["job_id"]
        publish_bundle(staged, destination, job["operation"], job["options"]["target"])
        result = json.loads((destination / "result.json").read_text())
        if job["options"].get("version_inputs"):
            from lerobot.data_platform.curation import CurationSnapshot, register_snapshot

            value = CurationSnapshot.from_dict(json.loads((destination / "snapshot.json").read_text()))
            expected = {item["version"]["version_id"] for item in job["options"]["version_inputs"]}
            if (
                set(value.version["parent_version_ids"]) != expected
                or value.version["root"] != job["options"]["out_root"]
            ):
                raise ValueError("Derived version does not match the planned inputs and output")
            derived = manager.store.sync_locations(job["node_id"], [result["dataset_location"]])[0]
            register_snapshot(ledger, value, derived)
            result["output_location_id"] = derived["location_id"]
        for artifact in result.get("artifacts", []):
            from urllib.parse import quote

            artifact["url"] = f"/api/operator-results/{job['job_id']}/" + quote(artifact["path"], safe="/")
        return result

    app.extensions["data_platform_complete_operator"] = complete_operator

    @app.get("/api/operator-results/<job_id>/<path:filename>")
    @checked
    def processing_artifact(job_id, filename):
        from lerobot.data_platform.curation import artifact_path

        job = manager.store.get_job(job_id)
        if job["requested_by"] != owner() or job["status"] != "done":
            raise KeyError(job_id)
        with manager.store.sessions() as session:
            manager._location(session, job["location_id"], owner(), write=False)
        return send_file(artifact_path(ledger.root / "operator-results" / job_id / "static", filename))

    @app.get("/pipelines")
    @checked
    def processing_page():
        return render_template("data_platform_pipelines.html", can_edit=_current_user()["role"] != "viewer")

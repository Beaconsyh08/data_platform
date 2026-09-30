"""Submit, track and acknowledge immediate Agent dataset discovery."""

from dataclasses import asdict

from flask import g, jsonify, request
from sqlalchemy.exc import IntegrityError

from lerobot.data_platform.routes.context import RouteContext


def register_dataset_scan_routes(app, ctx: RouteContext):
    store = ctx.control_plane_store

    @app.post("/api/control/nodes/<node_id>/scans")
    def request_dataset_scan(node_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Expected a JSON object"), 400
        try:
            scan = store.scan_manager.create(node_id, g.control_plane_user["user_id"], body.get("path"))
        except KeyError:
            return jsonify(error="Agent not found"), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        except IntegrityError:
            return jsonify(error="This Agent already has a dataset scan in progress"), 409
        return jsonify(scan=asdict(scan)), 202

    @app.get("/api/control/scans/<request_id>")
    def dataset_scan_status(request_id):
        try:
            scan = store.scan_manager.get(request_id, g.control_plane_user["user_id"])
        except KeyError:
            return jsonify(error="Scan request not found"), 404
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        return jsonify(scan=asdict(scan))

    @app.post("/api/agents/scans/<request_id>")
    def update_dataset_scan(request_id):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        node = store.authenticate_node(token)
        if not node:
            return jsonify(error="Invalid Agent token"), 401
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Expected a JSON object"), 400
        try:
            scan = store.scan_manager.update(
                node["node_id"],
                request_id,
                status=body.get("status"),
                dataset_count=body.get("dataset_count", 0),
                error=body.get("error"),
            )
        except KeyError:
            return jsonify(error="Scan request not found for this Agent"), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        return jsonify(scan=asdict(scan))

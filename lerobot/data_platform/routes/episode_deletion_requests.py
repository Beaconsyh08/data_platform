"""Episode deletion requests never execute until an administrator approves them."""

from flask import g, jsonify, request

from lerobot.data_platform.episode_deletion_requests import create_request, list_requests, review_request
from lerobot.data_platform.job_management import JobConflictError


def register_episode_deletion_request_routes(app, store, *, mutations_enabled):
    def actor():
        return getattr(g, "control_plane_user", None) or {}

    def invoke(callback):
        if not actor().get("user_id"):
            return jsonify(error="Authentication required"), 401
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="JSON object required"), 400
        try:
            return callback(body)
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        except JobConflictError as exc:
            return jsonify(error=str(exc)), 409
        except KeyError:
            return jsonify(error="Dataset or deletion request not found"), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 503

    @app.get("/api/control/episode-deletion-requests")
    def deletion_requests():
        if not actor().get("user_id"):
            return jsonify(error="Authentication required"), 401
        return jsonify(requests=list_requests(store, actor()))

    @app.post("/api/control/locations/<string:location_id>/episode-deletion-requests")
    def request_deletion(location_id):
        return invoke(lambda body: (jsonify(request=create_request(store, location_id, actor(), body)), 202))

    @app.post("/api/control/episode-deletion-requests/<string:request_id>/review")
    def review_deletion(request_id):
        return invoke(
            lambda body: (
                jsonify(
                    review_request(
                        store,
                        request_id,
                        actor(),
                        body,
                        mutations_enabled=mutations_enabled,
                    )
                ),
                202 if body.get("decision") == "approve" else 200,
            )
        )

"""Authenticated endpoints for server-funded Qwen draft assistance."""

from functools import wraps

from flask import jsonify, render_template, request
from sqlalchemy.exc import SQLAlchemyError

from lerobot.data_platform.ai_tools.personalization import conversations, preferences
from lerobot.data_platform.ai_tools.security import DisclosureGuard
from lerobot.data_platform.ai_tools.service import (
    AssistantBusyError,
    AssistantService,
    AssistantUnavailableError,
)
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.routes.control_plane import _current_user


def register_ai_assistant_routes(app, ctx):
    manager = app.extensions.get("data_platform_processing")
    if manager is None:
        return
    app.extensions["data_platform_assistant"] = AssistantService(manager)
    from lerobot.data_platform.routes.strategies import register_strategy_routes

    register_strategy_routes(app, ctx)

    @app.after_request
    def assistant_private_response(response):
        if request.path.startswith("/api/ai-assistant/") or request.path == "/assistant":
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def checked(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            actor = _current_user()
            if not actor:
                return jsonify(error="Authentication required"), 401
            if request.method == "POST" and not request.content_length:
                return jsonify(error="Assistant requests require a positive Content-Length"), 411
            if request.content_length and request.content_length > 16384:
                return jsonify(error="Assistant request is too large"), 413
            try:
                return function(actor["user_id"], app.extensions["data_platform_assistant"], *args, **kwargs)
            except PermissionError as exc:
                return jsonify(error=DisclosureGuard.from_environment().redact(str(exc))), 403
            except KeyError:
                return jsonify(error="Conversation, request or dataset was not found"), 404
            except JobConflictError as exc:
                return jsonify(error=DisclosureGuard.from_environment().redact(str(exc))), 409
            except AssistantBusyError as exc:
                return jsonify(error=DisclosureGuard.from_environment().redact(str(exc))), 429
            except AssistantUnavailableError as exc:
                return jsonify(error=DisclosureGuard.from_environment().redact(str(exc))), 503
            except SQLAlchemyError:
                # Driver exceptions may contain bound conversation text; never log their SQL payloads.
                return jsonify(error="Assistant storage is unavailable. Please retry later."), 503
            except (ValueError, TypeError):
                return jsonify(
                    error="Invalid assistant request. Check IDs, context, revision and draft parameters; file paths, endpoints and credentials are not accepted."
                ), 400
            except Exception:
                # Unexpected failures must not expose stack frames, SQL, or request contents.
                return jsonify(error="Assistant request failed. Please retry later."), 503

        return wrapped

    @app.get("/assistant")
    @checked
    def assistant_page(owner, service):
        return render_template(
            "data_platform_assistant.html", can_edit=service.capabilities(owner)["can_edit"]
        )

    @app.get("/api/ai-assistant/capabilities")
    @checked
    def assistant_capabilities(owner, service):
        return jsonify(service.capabilities(owner))

    @app.get("/api/ai-assistant/page-context")
    @checked
    def assistant_page_context(owner, service):
        from lerobot.data_platform.ai_tools.context import page_context

        return jsonify(
            page_context(service, owner, request.args.get("dataset_key"), request.args.get("episode_index"))
        )

    @app.get("/api/ai-assistant/conversations")
    @checked
    def assistant_history(owner, service):
        return jsonify(
            conversations(
                service, owner, cursor=request.args.get("cursor"), limit=int(request.args.get("limit", "20"))
            )
        )

    @app.route("/api/ai-assistant/preferences", methods=["GET", "PUT", "DELETE"])
    @checked
    def assistant_preferences(owner, service):
        if request.method == "GET":
            return jsonify(preferences(service, owner))
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("Preference object is required")
        return jsonify(preferences(service, owner, change=value, reset=request.method == "DELETE"))

    @app.route("/api/ai-assistant/memory", methods=["GET", "PUT"])
    @checked
    def assistant_memory(owner, service):
        from lerobot.data_platform.ai_tools.memory import change, read

        return jsonify(
            read(service, owner)
            if request.method == "GET"
            else change(service, owner, request.get_json(silent=True))
        )

    @app.get("/api/ai-assistant/followups")
    @checked
    def assistant_followups(owner, service):
        from lerobot.data_platform.ai_tools.followups import read
        from lerobot.data_platform.ai_tools.service import _uuid

        identity = request.args.get("conversation_id")
        return jsonify(read(service, owner, conversation_id=_uuid(identity) if identity else None))

    @app.get("/api/ai-assistant/conversations/<conversation_id>")
    @checked
    def assistant_conversation(owner, service, conversation_id):
        return jsonify(service.conversation(owner, conversation_id))

    @app.route("/api/ai-assistant/turns", methods=["POST"])
    @checked
    def assistant_submit(owner, service):
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("A JSON object is required")
        result = service.submit(owner, value)
        return jsonify(result), 202 if result["status"] == "running" else 200

    @app.get("/api/ai-assistant/turns/<request_id>")
    @checked
    def assistant_turn(owner, service, request_id):
        return jsonify(service.turn(owner, request_id))

    @app.post("/api/ai-assistant/actions/<action_id>/confirm")
    @checked
    def assistant_confirm(owner, service, action_id):
        from lerobot.data_platform.ai_tools.actions import confirm
        from lerobot.data_platform.ai_tools.service import _uuid

        value = request.get_json(silent=True)
        if (
            not isinstance(value, dict)
            or "revision" not in value
            or set(value) - {"revision", "rerun"}
            or type(value.get("rerun", False)) is not bool
        ):
            raise ValueError("An action revision is required")
        return jsonify(
            confirm(service, owner, _uuid(action_id), value["revision"], rerun=value.get("rerun", False))
        )

    @app.get("/api/ai-assistant/actions/<action_id>")
    @checked
    def assistant_action_status(owner, service, action_id):
        from lerobot.data_platform.ai_tools.operation_state import action_status
        from lerobot.data_platform.ai_tools.service import _uuid

        return jsonify(action_status(service, owner, _uuid(action_id)))

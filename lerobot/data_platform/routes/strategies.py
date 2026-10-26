"""Authenticated strategy administration; no model can invoke these management actions."""

import time
from functools import wraps

from flask import jsonify, render_template, request

from lerobot.data_platform.ai_tools.strategies import Strategies, public_summary
from lerobot.data_platform.ai_tools.strategy_evaluation import initial_cases
from lerobot.data_platform.ai_tools.strategy_models import StrategySuite
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.routes.control_plane import _current_user
from lerobot.data_platform.task_catalog import content_digest


def register_strategy_routes(app, ctx):
    def checked(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            actor = _current_user()
            if not actor:
                return jsonify(error="Authentication required"), 401
            if request.method == "POST" and not request.content_length:
                return jsonify(error="A positive Content-Length is required"), 411
            if request.content_length and request.content_length > 262144:
                return jsonify(error="Strategy request is too large"), 413
            service = Strategies(app.extensions["data_platform_assistant"])
            try:
                if request.path != "/api/ai-assistant/strategies":
                    with service.store.sessions() as session:
                        service.admin(session, actor["user_id"])
                return function(service, actor["user_id"], *args, **kwargs)
            except PermissionError:
                return jsonify(error="Administrator or dataset access is required"), 403
            except KeyError:
                return jsonify(error="Strategy resource not found"), 404
            except JobConflictError as exc:
                return jsonify(error=str(exc)), 409
            except (ValueError, TypeError):
                return jsonify(error="Invalid strategy configuration or synthetic fixture"), 400

        return wrapped

    @app.get("/admin/strategies")
    @checked
    def strategy_page(service, owner):
        return render_template("data_platform_strategies.html")

    @app.get("/api/ai-assistant/strategies")
    @checked
    def strategy_summary(service, owner):
        location = request.args.get("location_id")
        return jsonify(strategy=public_summary(service.select(owner, location)) if location else None)

    @app.get("/api/ai-assistant/strategies/admin")
    @checked
    def strategy_dashboard(service, owner):
        return jsonify(service.dashboard(owner))

    @app.post("/api/ai-assistant/strategies/<action>")
    @checked
    def strategy_action(service, owner, action):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        revision = body.get("expected_revision")
        fields = {
            "configure": "configuration",
            "review-suite": "suite_id",
            "start": "suite_id",
            "activate": "version_id",
            "rollback": "version_id",
            "disable": "version_id",
            "cancel": "experiment_id",
        }
        if action in fields:
            if set(body) != {"expected_revision", fields[action]}:
                raise ValueError("Unknown fields")
            method = {
                "configure": service.configure,
                "review-suite": service.review_suite,
                "start": service.start,
                "activate": service.activate,
                "rollback": service.activate,
                "disable": service.disable,
                "cancel": service.cancel,
            }[action]
            return jsonify(method(owner, revision, body[fields[action]]))
        if action == "suites" and set(body) <= {"expected_revision", "cases"}:
            cases = body.get("cases")
            if cases is None:
                import json

                cases = json.loads(json.dumps(initial_cases()))
                digest = content_digest(cases)
                with service.store.sessions.begin() as session:
                    service.edit(session, owner, revision)
                    if session.get(StrategySuite, digest) is None:
                        session.add(
                            StrategySuite(
                                suite_id=digest, digest=digest, cases=cases, created_at=int(time.time())
                            )
                        )
                    service.manager._audit(session, owner, "strategy.suite.created", suite_id=digest)
            else:
                from lerobot.data_platform.ai_tools.strategy_evaluation import validate_cases

                cases = validate_cases(cases)
                digest = content_digest(cases)
                with service.store.sessions.begin() as session:
                    service.edit(session, owner, revision)
                    if session.get(StrategySuite, digest):
                        raise JobConflictError("This evaluation suite already exists")
                    # Exposed/consumed cases cannot become a new holdout by renaming their IDs.
                    from sqlalchemy import select

                    for existing in session.scalars(select(StrategySuite)):
                        known = {
                            content_digest({k: v for k, v in c.items() if k != "case_id"})
                            for group in existing.cases.values()
                            for c in group
                        }
                        if any(
                            content_digest({k: v for k, v in c.items() if k != "case_id"}) in known
                            for c in cases["holdout"]
                        ):
                            raise ValueError("Hidden cases must be fresh")
                    session.add(
                        StrategySuite(
                            suite_id=digest, digest=digest, cases=cases, created_at=int(time.time())
                        )
                    )
                    service.manager._audit(session, owner, "strategy.suite.created", suite_id=digest)
            return jsonify(service.dashboard(owner))
        raise ValueError("Unknown strategy action")

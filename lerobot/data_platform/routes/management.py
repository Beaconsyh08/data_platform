"""Existing-role administration and job control endpoints."""

from flask import g, jsonify, request
from sqlalchemy import select

from lerobot.data_platform.job_management import JobConflictError


def register_management_routes(app, store):
    def user():
        return getattr(g, "control_plane_user", None) or {}

    def admin():
        if user().get("role") != "admin":
            return jsonify({"error": "Administrator role required"}), 403
        return None

    def page():
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        if not 1 <= limit <= 200 or not 0 <= offset <= 100000:
            raise ValueError("limit must be 1-200 and offset 0-100000")
        return limit, offset

    @app.errorhandler(JobConflictError)
    def conflict(exc):
        return jsonify({"error": str(exc)}), 409

    @app.get("/api/admin/usage-events")
    def usage_events():
        if denied := admin():
            return denied
        logs = app.extensions["data_platform_logs"]
        if logs is None:
            return jsonify({"error": "Configure DATA_PLATFORM_LOG_DATABASE_URL to enable log queries"}), 503
        try:
            limit, offset = page()
            return jsonify(logs.query(request.args, limit=limit, offset=offset))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            return jsonify({"error": "Log database temporarily unavailable"}), 503

    @app.get("/api/admin/usage-summary")
    def usage_summary():
        if denied := admin():
            return denied
        delivery = app.extensions["data_platform_log_delivery"]
        if delivery is None:
            return jsonify({"configured": False, "counts": [], "error": "Log database is not configured"})
        try:
            counts = delivery.logs.summary(request.args)
        except Exception:
            counts = {"counts": [], "error": "Log database temporarily unavailable"}
        return jsonify({**counts, "configured": True, "delivery": delivery.status()})

    @app.get("/api/admin/database/tables")
    def database_tables():
        if denied := admin():
            return denied
        return jsonify({"tables": app.extensions["data_platform_database_browser"].describe()})

    @app.get("/api/admin/database/tables/<string:table_key>/rows")
    def database_rows(table_key):
        if denied := admin():
            return denied
        browser = app.extensions["data_platform_database_browser"]
        if not browser.rate_limit(user()["user_id"]):
            return jsonify({"error": "Database browsing rate limit reached"}), 429
        try:
            limit, offset = page()
            return jsonify(
                browser.rows(
                    table_key,
                    limit=limit,
                    offset=offset,
                    field=request.args.get("field"),
                    value=request.args.get("value"),
                )
            )
        except KeyError:
            return jsonify({"error": "Table is not available"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            return jsonify({"error": "Database query unavailable or timed out"}), 503

    def control(job_id, action, *, owner_only=False):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object required"}), 400
        try:
            job = store.job_manager.command(job_id, user(), action, body, owner_only=owner_only)
            return jsonify({"job": store.job_manager.decorate(job, user())}), 202 if job[
                "status"
            ] == "cancel_requested" else 200
        except JobConflictError:
            raise
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except KeyError:
            return jsonify({"error": "Job not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.post("/api/control/jobs/<string:job_id>/retry")
    def retry_job(job_id):
        return control(job_id, "retry")

    @app.post("/api/jobs/<string:job_id>/retry")
    def retry_own_job(job_id):
        return control(job_id, "retry", owner_only=True)

    @app.post("/api/jobs/<string:job_id>/cancel")
    def cancel_own_job(job_id):
        return control(job_id, "cancel", owner_only=True)

    @app.post("/api/control/jobs/<string:job_id>/cancel")
    def cancel_job(job_id):
        return control(job_id, "cancel")

    @app.post("/api/control/jobs/<string:job_id>/terminate")
    def terminate_job(job_id):
        return control(job_id, "terminate")

    @app.patch("/api/control/jobs/<string:job_id>/priority")
    def priority_job(job_id):
        return control(job_id, "priority")

    @app.get("/api/control/jobs/<string:job_id>/attempts")
    def job_attempts(job_id):
        try:
            store.get_job(job_id)
        except KeyError:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({"attempts": store.job_manager.attempts(job_id)})

    @app.get("/api/control/jobs/<string:job_id>/events")
    def job_events(job_id):
        from lerobot.data_platform.control_plane import RemoteJobEvent

        try:
            store.get_job(job_id)
            limit, offset = page()
            after = int(request.args.get("after", 0))
        except KeyError:
            return jsonify({"error": "Job not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        with store.sessions() as session:
            rows = session.scalars(
                select(RemoteJobEvent)
                .where(RemoteJobEvent.job_id == job_id, RemoteJobEvent.event_id > after)
                .order_by(RemoteJobEvent.event_id)
                .offset(offset)
                .limit(limit + 1)
            ).all()
            return jsonify(
                {"events": [store._event_dict(row) for row in rows[:limit]], "has_more": len(rows) > limit}
            )

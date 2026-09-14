"""Environment banner, browser request isolation, and maintenance responses."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from flask import jsonify, render_template, request, send_file

from lerobot.data_platform.environment import EnvironmentIdentity, session_cookie_name
from lerobot.data_platform.maintenance import MaintenanceError, is_maintenance


def csrf_token(token: str | None) -> str:
    return hashlib.sha256(("data-platform-csrf:" + token).encode()).hexdigest() if token else "anonymous"


def install_environment_web(app, store):
    identity = EnvironmentIdentity.from_env()

    @app.errorhandler(PermissionError)
    def permission_error(exc):
        return jsonify({"error": str(exc)}), 403

    @app.errorhandler(MaintenanceError)
    def maintenance_error(exc):
        return jsonify({"error": str(exc), "maintenance": True}), 503, {"Retry-After": "30"}

    @app.before_request
    def environment_request_guard():
        if identity and (
            request.path.startswith("/static/lifecycle/")
            or request.path == "/static/datasets_registry.json"
            or request.path.endswith((".db", ".db-wal", ".db-shm"))
        ):
            return jsonify({"error": "Not found"}), 404
        internal = bool(os.environ.get("DATA_PLATFORM_INTERNAL_EXECUTION"))
        machine = request.path.startswith("/api/agents/")
        write = request.method not in {"GET", "HEAD", "OPTIONS"}
        if identity and write and not machine and not internal:
            # A non-simple header also protects unauthenticated login/bootstrap requests.
            origin = request.headers.get("Origin")
            expected_origin = request.host_url.rstrip("/")
            if origin != expected_origin or not hmac.compare_digest(
                request.headers.get("X-Data-Platform-CSRF", ""),
                csrf_token(request.cookies.get(session_cookie_name())),
            ):
                return jsonify({"error": "Invalid request origin or CSRF token"}), 403
        if write and not machine and not internal and request.path != "/api/auth/logout":
            with store.sessions() as session:
                if is_maintenance(session):
                    raise MaintenanceError("Environment is under maintenance; writes are paused")
        return None

    if identity is None:
        return

    @app.get("/environment.js")
    def environment_script():
        return send_file(Path(__file__).parent / "static" / "environment.js", max_age=0)

    @app.after_request
    def environment_response(response):
        response.headers["X-Data-Platform-Environment"] = identity.name
        cookies = response.headers.getlist("Set-Cookie")
        if cookies:
            del response.headers["Set-Cookie"]
            for cookie in cookies:
                if cookie.startswith(session_cookie_name() + "="):
                    response.headers.add("Set-Cookie", cookie)
        if response.mimetype == "text/html" and not response.direct_passthrough and not response.is_streamed:
            fragment = render_template(
                "data_platform_environment.html",
                environment=identity.name,
                release=os.environ.get("DATA_PLATFORM_RELEASE", "legacy"),
            ).encode()
            response.set_data(response.get_data().replace(b"</head>", fragment + b"</head>", 1))
        if request.path.startswith(("/api/auth/", "/api/dev/")):
            response.headers["Cache-Control"] = "no-store"
        return response

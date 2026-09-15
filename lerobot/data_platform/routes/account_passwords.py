"""Thin HTTP boundaries for central account password changes and recovery."""

from flask import jsonify, render_template, request

from lerobot.data_platform.account_passwords import allow_attempt, change_password, issue_reset, redeem_reset
from lerobot.data_platform.environment import session_cookie_name


def register_account_password_routes(app, store):
    from lerobot.data_platform.routes.control_plane import _current_user, _role_denied

    @app.get("/account/password")
    @app.get("/reset-password")
    def account_password_page():
        response = app.make_response(
            render_template("data_platform_password.html", reset=request.path == "/reset-password")
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def limited(key, limit=10):
        if not allow_attempt(store, key, limit=limit):
            return (
                jsonify({"error": "Too many attempts. Try again in 5 minutes."}),
                429,
                {"Retry-After": "300"},
            )
        return None

    @app.post("/api/auth/users/<string:user_id>/password-reset")
    def account_issue_reset(user_id):
        denied = _role_denied("admin")
        if denied:
            return denied
        actor = _current_user()
        if actor.get("original_actor"):
            return jsonify({"error": "Exit role preview before managing passwords"}), 403
        denied = limited("issue:" + actor["user_id"])
        if denied:
            return denied
        try:
            token = issue_reset(store, user_id, actor)
        except KeyError:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"token": token, "expires_in": 900}), 200, {"Cache-Control": "no-store"}

    @app.post("/api/auth/password")
    @app.post("/api/auth/reset-password")
    def account_save_password():
        reset = request.path == "/api/auth/reset-password"
        actor = None if reset else _current_user()
        if not reset and (not actor or actor.get("original_actor")):
            return jsonify({"error": "Sign in with your own account to change your password"}), 403
        key = "reset:" + str(request.remote_addr) if reset else "change:" + actor["user_id"]
        denied = limited(key)
        if denied:
            return denied
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        new_password = body.get("new_password")
        if not isinstance(new_password, str) or new_password != body.get("confirm_password"):
            return jsonify({"error": "Password confirmation does not match"}), 400
        try:
            if reset:
                token = body.get("token")
                if not isinstance(token, str) or not 1 <= len(token) <= 256:
                    raise ValueError("Reset link is invalid or expired")
                redeem_reset(store, token, new_password)
            else:
                current = body.get("current_password")
                if not isinstance(current, str) or len(current) > 256:
                    raise ValueError("Current password is required")
                change_password(store, actor["user_id"], current, new_password, actor)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except (PermissionError, KeyError):
            return jsonify({"error": "Current password is incorrect"}), 403
        response = jsonify({"status": "ok"})
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(session_cookie_name(), samesite="Strict")
        return response

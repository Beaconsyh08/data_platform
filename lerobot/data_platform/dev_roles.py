"""Development-only role sessions backed by real, explicitly seeded test users."""

from __future__ import annotations

import os
import secrets

from flask import jsonify, request
from sqlalchemy import select

from lerobot.data_platform.environment import EnvironmentIdentity, session_cookie_name

TEST_ROLES = {"operator_a": "operator", "operator_b": "operator", "viewer": "viewer"}


def enabled() -> bool:
    identity = EnvironmentIdentity.from_env()
    return bool(
        identity and identity.name == "dev" and os.environ.get("DATA_PLATFORM_ENABLE_DEV_ROLE_SWITCH") == "1"
    )


def seed_test_users(store) -> None:
    from lerobot.data_platform.control_plane import DevTestUser
    from lerobot.data_platform.job_management import scheduler_lock

    if not enabled():
        raise RuntimeError("Test users can only be seeded with development role switching enabled")
    if store.user_count() == 0:
        raise RuntimeError("Bootstrap the development administrator before seeding test users")
    for name, role in TEST_ROLES.items():
        with store.sessions.begin() as session:
            scheduler_lock(session)
            if session.get(DevTestUser, name) is not None:
                continue
            user = store._new_user(f"dev_test_{name}", secrets.token_urlsafe(48), f"开发测试 {name}", role)
            session.add(user)
            session.add(DevTestUser(name=name, user_id=user.user_id))


def effective_user(session, login, administrator, *, original=False):
    from lerobot.data_platform.control_plane import (
        ControlPlaneStore,
        ControlPlaneUser,
        DevRoleSession,
        DevTestUser,
    )

    if not enabled():
        return None
    switched = session.get(DevRoleSession, login.session_id)
    if switched is None:
        return None
    # A disabled/demoted owner cannot retain an impersonated login or use the return endpoint.
    if not administrator.active or administrator.role != "admin":
        raise PermissionError("The original development administrator is no longer authorized")
    if original:
        return ControlPlaneStore._user_dict(administrator)
    user = session.get(ControlPlaneUser, switched.user_id)
    entry = session.scalar(select(DevTestUser).where(DevTestUser.user_id == switched.user_id))
    if user is None or not user.active or entry is None or user.role != TEST_ROLES.get(entry.name):
        raise PermissionError("The selected development test user is no longer valid")
    result = ControlPlaneStore._user_dict(user)
    result["original_actor"] = ControlPlaneStore._user_dict(administrator)
    return result


def register_dev_role_routes(app, store):
    if not enabled():
        return

    @app.route("/api/dev/role-session", methods=["GET", "POST", "DELETE"])
    def role_session():
        from lerobot.data_platform.control_plane import (
            ControlPlaneSession,
            DevRoleSession,
            DevTestUser,
            _token_digest,
        )
        from lerobot.data_platform.management_storage import enqueue_event
        from lerobot.data_platform.operation_log import build_operation_event

        token = request.cookies.get(session_cookie_name())
        actor = store.verify_session(token, original=True)
        if not actor or actor["role"] != "admin":
            return jsonify({"error": "Development administrator required"}), 403
        if request.method == "GET":
            try:
                current = store.verify_session(token)
            except PermissionError:
                current = {
                    "user_id": "invalid",
                    "username": "invalid",
                    "role": "unavailable",
                    "original_actor": actor,
                }
            return jsonify({"actor": actor, "user": current, "choices": ["admin", *TEST_ROLES]})
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object required"}), 400
        name = "admin" if request.method == "DELETE" else body.get("identity")
        if name not in {"admin", *TEST_ROLES}:
            return jsonify({"error": "Unknown development test identity"}), 400
        with store.sessions.begin() as session:
            login = session.scalar(
                select(ControlPlaneSession).where(ControlPlaneSession.token_digest == _token_digest(token))
            )
            switched = session.get(DevRoleSession, login.session_id)
            if name == "admin":
                if switched is not None:
                    session.delete(switched)
            else:
                target = session.get(DevTestUser, name)
                if target is None:
                    return jsonify({"error": "Run seed-dev-users first"}), 409
                if switched is None:
                    session.add(DevRoleSession(session_id=login.session_id, user_id=target.user_id))
                else:
                    switched.user_id = target.user_id
            enqueue_event(
                session,
                build_operation_event(
                    "auth.dev_role_switch",
                    status="done",
                    actor=actor,
                    details={"identity": name},
                    source="control-plane",
                ),
            )
        return jsonify({"user": store.verify_session(token)})

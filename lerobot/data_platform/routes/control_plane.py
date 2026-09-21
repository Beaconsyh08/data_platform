"""Flask routes for central accounts, node agents, and remote jobs."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, redirect, render_template, request

from lerobot.data_platform.environment import EnvironmentIdentity, session_cookie_name
from lerobot.data_platform.precompute.data_profile import (
    DATA_PROFILE_PROTOCOL,
    default_processing_profile,
    profile_from_data_version,
    profile_from_info,
    require_operation,
    required_data_profile_protocol,
)

if TYPE_CHECKING:
    from lerobot.data_platform.control_plane import ControlPlaneStore

CONTROL_PLANE_SESSION_COOKIE = "data_platform_session"
REMOTE_PREPROCESS_OPS = {
    "convert_action",
    "convert_v3",
    "drop_field",
    "merge",
    "smooth_action",
    "split",
    "standardize",
    "value_edit",
}
REMOTE_SOURCE_MUTATION_OPS = {
    "trim_episode",
    "delete_episodes",
    "repair_v3_video_timestamps",
    "value_edit",
}
_REMOTE_MUTATION_OPTION_KEYS = {
    "trim_episode": {"episode_id", "start_frame", "end_frame", "reason"},
    "delete_episodes": {"episodes", "reason"},
    "repair_v3_video_timestamps": {"dry_run", "reason"},
    "value_edit": {"dry_run", "edits", "episode_ids", "reason"},
}
_FORBIDDEN_REMOTE_OPTION_KEYS = {
    "in_place",
    "output_mode",
    "root",
    "roots",
    "source_root",
    "source_roots",
    "src_root",
    "src_roots",
    "_source_locations",
}
_REMOTE_MERGE_OPTION_KEYS = {
    "source_location_ids",
    "dimension_policy",
    "dimension_names",
    "dimension_indices",
    "padding_value",
    "exclude_episodes",
    "workers",
    "dry_run",
    "out_root",
}
_DERIVED_VIEWER_OPERATIONS = {
    "preprocess.standardize",
    "preprocess.merge",
    "preprocess.split",
    "curation.materialize",
    "curation.construction",
}


def _normalize_remote_path(value: object, label: str) -> PurePosixPath:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required")
    path = PurePosixPath(posixpath.normpath(raw))
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path on the Agent")
    return path


def _validate_remote_output_path(
    store: ControlPlaneStore,
    location_id: str,
    value: object,
) -> str:
    location = store.get_location(location_id)
    node = next(
        (item for item in store.list_nodes() if item["node_id"] == location["node_id"]),
        None,
    )
    if node is None:
        raise KeyError(location["node_id"])
    output = _normalize_remote_path(value, "out_root")
    writable_roots = [
        _normalize_remote_path(root, "Agent writable root") for root in node.get("writable_roots") or []
    ]
    if not any(output == root or root in output.parents for root in writable_roots):
        allowed = ", ".join(str(root) for root in writable_roots) or "none configured"
        raise PermissionError(f"out_root must be inside this Agent's writable roots: {allowed}")
    source = _normalize_remote_path(location["root"], "dataset root")
    if output == source or output in source.parents or source in output.parents:
        raise ValueError("out_root must be separate from the source dataset and may not contain it")
    return str(output)


def _bearer_token() -> str | None:
    value = str(request.headers.get("Authorization") or "")
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _safe_next_url(value: str | None) -> str:
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    if candidate.startswith("/") and not candidate.startswith("//") and not parsed.netloc:
        return candidate
    return "/"


def _current_user() -> dict | None:
    value = getattr(g, "control_plane_user", None)
    return dict(value) if isinstance(value, dict) else None


def _role_denied(*roles: str):
    user = _current_user()
    if user is None:
        return jsonify({"error": "authentication required"}), 401
    if user.get("role") not in set(roles):
        return jsonify({"error": f"one of these roles is required: {', '.join(roles)}"}), 403
    return None


def register_control_plane_auth_routes(
    app: Flask,
    store: ControlPlaneStore,
    *,
    bootstrap_token: str,
    allow_registration: bool,
) -> None:
    from lerobot.data_platform.admin_management import install_usage_audit
    from lerobot.data_platform.dev_roles import register_dev_role_routes
    from lerobot.data_platform.environment_web import install_environment_web

    install_environment_web(app, store)
    from lerobot.data_platform.promotion import register_promotion_routes

    register_promotion_routes(app, store)
    register_dev_role_routes(app, store)
    install_usage_audit(app)
    from lerobot.data_platform.routes.account_passwords import register_account_password_routes

    register_account_password_routes(app, store)
    public_paths = {
        "/reset-password",
        "/api/auth/reset-password",
        "/api/auth/bootstrap",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/status",
        "/healthz",
        "/environment.js",
        "/login",
    }

    @app.before_request
    def _require_control_plane_session():
        path = request.path
        if path in public_paths or path.startswith("/api/agents/") or path.startswith("/static/"):
            return None
        from lerobot.data_platform.local_execution import internal_execution_user

        try:
            user = internal_execution_user(store) or store.verify_session(
                request.cookies.get(session_cookie_name()),
                original=path in {"/api/dev/role-session", "/api/auth/logout"},
            )
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        if user is not None:
            g.control_plane_user = user
            if request.method in {"GET", "HEAD"} and user.get("role") != "admin":
                job_path = re.fullmatch(r"/api/(?:control/)?jobs/([^/]+)(?:/.*)?", path)
                if job_path:
                    try:
                        job = store.get_job(job_path.group(1))
                    except KeyError:
                        return jsonify({"error": "job not found"}), 404
                    if job.get("requested_by") != user["user_id"]:
                        return jsonify({"error": "job not found"}), 404
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and path not in {"/api/auth/logout", "/api/dev/role-session", "/api/auth/password"}
                and user.get("role") == "viewer"
            ):
                return jsonify({"error": "viewer accounts are read-only"}), 403
            return None
        if path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
            return jsonify({"error": "authentication required"}), 401
        return redirect(f"/login?next={request.full_path.rstrip('?')}")

    @app.route("/healthz")
    def control_plane_health():
        identity = EnvironmentIdentity.from_env()
        from lerobot.data_platform.maintenance import active_jobs, is_maintenance

        with store.sessions() as session:
            maintenance = is_maintenance(session)
        return jsonify(
            {
                "status": "ok",
                "control_plane": True,
                "data_profile_protocol": DATA_PROFILE_PROTOCOL,
                "environment": identity.name if identity else "legacy",
                "instance_id": identity.instance_id if identity else None,
                "release": os.environ.get("DATA_PLATFORM_RELEASE", "legacy"),
                "agent_manifest_sha256": os.environ.get("DATA_PLATFORM_AGENT_MANIFEST_SHA256", ""),
                "maintenance": maintenance,
                "active_jobs": active_jobs(store),
            }
        )

    @app.route("/login")
    def control_plane_login_page():
        existing = store.verify_session(request.cookies.get(session_cookie_name()))
        if existing is not None:
            return redirect(_safe_next_url(request.args.get("next")))
        return render_template(
            "data_platform_login.html",
            configured=store.user_count() > 0,
            allow_registration=bool(allow_registration),
            next_url=_safe_next_url(request.args.get("next")),
        )

    @app.route("/api/auth/status")
    def control_plane_auth_status():
        from lerobot.data_platform.dev_roles import enabled
        from lerobot.data_platform.environment_web import csrf_token

        try:
            user = store.verify_session(request.cookies.get(session_cookie_name()))
        except PermissionError:
            user = store.verify_session(request.cookies.get(session_cookie_name()), original=True)
        return jsonify(
            {
                "configured": store.user_count() > 0,
                "allow_registration": bool(allow_registration),
                "authenticated": user is not None,
                "csrf_token": csrf_token(request.cookies.get(session_cookie_name())),
                "dev_role_switch": enabled()
                and bool(user and (user.get("original_actor") or user.get("role") == "admin")),
                "user": user,
            }
        )

    @app.route("/api/auth/bootstrap", methods=["POST"])
    def control_plane_bootstrap():
        body = request.get_json(silent=True) or {}
        try:
            user = store.bootstrap_admin(
                username=body.get("username"),
                password=body.get("password"),
                bootstrap_token=body.get("bootstrap_token"),
                expected_token=bootstrap_token,
            )
            token, user = store.authenticate_user(body.get("username"), body.get("password"))
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return _login_response(token, user)

    @app.route("/api/auth/register", methods=["POST"])
    def control_plane_register():
        if not allow_registration:
            return jsonify({"error": "self-registration is disabled"}), 403
        body = request.get_json(silent=True) or {}
        try:
            user = store.register_user(
                username=body.get("username"),
                password=body.get("password"),
                active=False,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        return (
            jsonify(
                {
                    "status": "pending_approval",
                    "message": "Registration submitted. An administrator must approve the account.",
                    "user": user,
                }
            ),
            202,
        )

    @app.route("/api/auth/login", methods=["POST"])
    def control_plane_login():
        body = request.get_json(silent=True) or {}
        try:
            token, user = store.authenticate_user(body.get("username"), body.get("password"))
        except (PermissionError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 403
        return _login_response(token, user)

    def _login_response(token: str, user: dict):
        response = jsonify({"status": "ok", "user": user})
        response.set_cookie(
            session_cookie_name(),
            token,
            httponly=True,
            secure=request.is_secure,
            samesite="Strict",
            max_age=7 * 24 * 60 * 60,
        )
        return response

    @app.route("/api/auth/logout", methods=["POST"])
    def control_plane_logout():
        store.logout(request.cookies.get(session_cookie_name()))
        response = jsonify({"status": "ok"})
        response.delete_cookie(session_cookie_name(), samesite="Strict")
        return response

    @app.route("/api/auth/me")
    def control_plane_me():
        return jsonify({"user": _current_user()})

    @app.route("/api/auth/users", methods=["GET", "POST"])
    def control_plane_users():
        denied = _role_denied("admin")
        if denied:
            return denied
        if request.method == "GET":
            return jsonify({"users": store.list_users()})
        body = request.get_json(silent=True) or {}
        try:
            user = store.register_user(
                username=body.get("username"),
                password=body.get("password"),
                role=str(body.get("role") or "viewer"),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"user": user}), 201

    @app.route("/api/auth/users/<string:user_id>", methods=["PATCH"])
    def control_plane_update_user(user_id: str):
        denied = _role_denied("admin")
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        try:
            user = store.update_user(user_id, role=body.get("role"), active=body.get("active"))
        except KeyError:
            return jsonify({"error": "user not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"user": user})

    @app.route("/api/auth/users/<string:user_id>/data-scopes", methods=["GET", "PUT"])
    def control_plane_data_scopes(user_id: str):
        actor = _current_user()
        if request.method == "PUT" or not actor or actor["user_id"] != user_id:
            denied = _role_denied("admin")
            if denied:
                return denied
        if request.method == "GET":
            return jsonify(store.mutation_scope(user_id))
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or "location_ids" not in body:
            return jsonify(error="location_ids array required"), 400
        try:
            selected = store.set_mutation_locations(
                user_id, body["location_ids"], actor=actor, all_locations=body.get("all_locations", False)
            )
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        except KeyError:
            return jsonify(error="user not found"), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(location_ids=selected, all_locations=body.get("all_locations", False))


def register_control_plane_routes(
    app: Flask,
    store: ControlPlaneStore,
    *,
    enrollment_token: str,
    remote_cache_root: Path,
    register_remote_cache: Callable[[dict, Path], str],
    legacy_mutations_enabled: bool = False,
    task_catalog_store=None,
) -> None:
    from lerobot.data_platform.routes.episode_deletion_requests import (
        register_episode_deletion_request_routes,
    )

    register_episode_deletion_request_routes(app, store, mutations_enabled=legacy_mutations_enabled)
    remote_cache_root = Path(remote_cache_root).expanduser().resolve()
    from lerobot.data_platform.admin_management import configure_management
    from lerobot.data_platform.execution import atomic_json, serialize_completion
    from lerobot.data_platform.job_management import JobConflictError
    from lerobot.data_platform.routes.management import register_management_routes

    configure_management(
        app, store, remote_cache_root.parent, getattr(task_catalog_store, "repository", None)
    )
    register_management_routes(app, store)
    from lerobot.data_platform.routes.dataset_results import register_dataset_result_routes

    register_dataset_result_routes(app, store, remote_cache_root)

    def _execution_credentials():
        return {
            "attempt_id": request.headers.get("X-Job-Attempt"),
            "credential": request.headers.get("X-Job-Credential"),
        }

    @app.before_request
    def _validate_execution_request():
        if not request.path.startswith("/api/agents/jobs/") or request.path == "/api/agents/jobs/claim":
            return None
        job_id = (request.view_args or {}).get("job_id")
        if not job_id:
            return None
        node = store.authenticate_node(_bearer_token())
        if node is None:
            return jsonify({"error": "invalid node token"}), 401
        from lerobot.data_platform.control_plane import RemoteJob

        try:
            with store.sessions.begin() as session:
                job = session.get(RemoteJob, job_id)
                store.job_manager.validate(
                    session,
                    job,
                    node["node_id"],
                    **_execution_credentials(),
                    allow_terminal=request.path.endswith("/complete"),
                )
        except KeyError:
            return jsonify({"error": "job not found for this node"}), 404
        except JobConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        return None

    @app.post("/api/agents/jobs/<string:job_id>/checkpoint")
    def agent_job_checkpoint(job_id):
        node, error = _node_or_error()
        if error:
            return error
        body = request.get_json(silent=True) or {}
        try:
            result = store.job_manager.checkpoint(
                job_id,
                node["node_id"],
                **_execution_credentials(),
                phase=body.get("phase"),
                fingerprint=body.get("fingerprint"),
            )
        except JobConflictError:
            raise
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    def _node_or_error():
        node = store.authenticate_node(_bearer_token())
        if node is None:
            return None, (jsonify({"error": "invalid node token"}), 401)
        return node, None

    def _validate_semantic_operation(location_id: str, operation: str, options: dict) -> None:
        location = store.get_location(location_id)
        metadata = location.get("metadata") or {}
        profile = profile_from_info(metadata)
        required_protocol = required_data_profile_protocol(metadata)
        if operation == "viewer.prepare" and options.get("force_recompute_stage"):
            required_protocol = DATA_PROFILE_PROTOCOL
        if required_protocol:
            node = next(item for item in store.list_nodes() if item["node_id"] == location["node_id"])
            if node.get("capabilities", {}).get("data_profile_protocol", 0) < required_protocol:
                raise RuntimeError(
                    f"Upgrade the Agent to support UMI/unknown data profile protocol {required_protocol}"
                )
        if options.get("data_version") and profile.robot_profile != "umi":
            profile = profile_from_data_version(
                options["data_version"],
                metadata.get("features") or {},
                resolution_source="explicit_override",
                confirmed=True,
            )
        if metadata.get("features") or metadata.get("data_profile"):
            require_operation(metadata, operation, profile=profile)
            if operation == "viewer.prepare" and options.get("force_recompute_stage"):
                require_operation(metadata, "auto_stage", profile=profile)
        if operation in {"viewer.prepare", "standardize"} and not options.get("data_version"):
            default_version = default_processing_profile(
                profile, metadata.get("features") or {}
            ).legacy_data_version
            if default_version is not None:
                options["data_version"] = default_version
        if operation == "viewer.prepare":
            count = int(options.get("fallback_stage_count", 5))
            if count < 2:
                raise ValueError("fallback_stage_count must be at least 2")
        if profile.robot_profile == "umi":
            if options.get("data_version"):
                raise ValueError("UMI data cannot use a DVT processing profile")
            if options.get("dimension_policy", "strict") != "strict":
                raise ValueError("UMI merge requires dimension_policy='strict'")

    @app.route("/control-plane")
    def control_plane_page():
        return render_template("data_platform_control_plane.html", user=_current_user())

    @app.route("/api/agents/enroll", methods=["POST"])
    def agent_enroll():
        body = request.get_json(silent=True) or {}
        try:
            token, node = store.enroll_node(
                name=body.get("name"),
                hostname=body.get("hostname"),
                allowed_roots=list(body.get("allowed_roots") or []),
                writable_roots=list(body.get("writable_roots") or []),
                capabilities=dict(body.get("capabilities") or {}),
                enrollment_token=body.get("enrollment_token"),
                expected_token=enrollment_token,
            )
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"node": node, "node_token": token})

    @app.route("/api/agents/heartbeat", methods=["POST"])
    def agent_heartbeat():
        node, error = _node_or_error()
        if error:
            return error
        body = request.get_json(silent=True) or {}
        try:
            refreshed = store.heartbeat(node["node_id"], capabilities=body.get("capabilities"))
        except KeyError:
            return jsonify({"error": "node not found"}), 404
        return jsonify({"node": refreshed, "result_sync_protocol": 1})

    @app.route("/api/agents/locations/sync", methods=["POST"])
    def agent_sync_locations():
        node, error = _node_or_error()
        if error:
            return error
        body = request.get_json(silent=True) or {}
        locations = body.get("locations") or []
        if not isinstance(locations, list):
            return jsonify({"error": "locations must be a list"}), 400
        try:
            synced = store.sync_locations(node["node_id"], locations)
        except (KeyError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"locations": synced})

    @app.route("/api/agents/jobs/claim", methods=["POST"])
    def agent_claim_job():
        node, error = _node_or_error()
        if error:
            return error
        body = request.get_json(silent=True) or {}
        job = store.claim_job(
            node["node_id"],
            lease_seconds=int(body.get("lease_seconds") or 60),
            worker_instance_id=body.get("worker_instance_id"),
        )
        return jsonify({"job": job})

    @app.route("/api/agents/jobs/<string:job_id>/heartbeat", methods=["POST"])
    def agent_job_heartbeat(job_id: str):
        node, error = _node_or_error()
        if error:
            return error
        body = request.get_json(silent=True) or {}
        result = store.job_manager.heartbeat(
            job_id,
            node["node_id"],
            lease_seconds=int(body.get("lease_seconds") or 60),
            **_execution_credentials(),
        )
        return jsonify(result)

    @app.route("/api/agents/jobs/<string:job_id>/events", methods=["POST"])
    def agent_job_event(job_id: str):
        node, error = _node_or_error()
        if error:
            return error
        body = request.get_json(silent=True) or {}
        try:
            event = store.add_job_event(
                job_id,
                node_id=node["node_id"],
                message=str(body.get("message") or ""),
                level=str(body.get("level") or "info"),
                payload=dict(body.get("payload") or {}),
                **_execution_credentials(),
            )
        except KeyError:
            return jsonify({"error": "job not found for this node"}), 404
        return jsonify({"event": event})

    def _store_agent_artifact(job_id: str, filename: str, *, derived: bool):
        node, error = _node_or_error()
        if error:
            return error
        try:
            job = store.get_job(job_id)
        except KeyError:
            return jsonify({"error": "job not found"}), 404
        accepted_operations = _DERIVED_VIEWER_OPERATIONS if derived else {"viewer.prepare"}
        if job["node_id"] != node["node_id"] or job["operation"] not in accepted_operations:
            return jsonify({"error": "job does not accept viewer artifacts"}), 403
        if job["status"] != "running":
            return jsonify({"error": "job is not running"}), 409
        attempt = request.headers.get("X-Job-Attempt")
        cache_key = Path(".jobs") / job_id / attempt if attempt else Path(".jobs") / job_id
        base = (remote_cache_root / cache_key / "static").resolve()
        target = (base / filename).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return jsonify({"error": "artifact path escapes the viewer cache"}), 400
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                while chunk := request.stream.read(1024 * 1024):
                    handle.write(chunk)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return jsonify({"status": "ok", "path": filename, "size": target.stat().st_size})

    @app.route("/api/agents/jobs/<string:job_id>/artifacts/<path:filename>", methods=["PUT"])
    def agent_upload_artifact(job_id: str, filename: str):
        return _store_agent_artifact(job_id, filename, derived=False)

    @app.route(
        "/api/agents/jobs/<string:job_id>/derived-artifacts/<path:filename>",
        methods=["PUT"],
    )
    def agent_upload_derived_artifact(job_id: str, filename: str):
        return _store_agent_artifact(job_id, filename, derived=True)

    def _promote_derived_cache(
        job_id: str, location: dict, *, staged_cache: Path | None = None
    ) -> tuple[Path, str]:
        from lerobot.data_platform.dataset_results import result_lock

        cache_output_dir = remote_cache_root / location["location_id"]
        with result_lock(cache_output_dir):
            return _promote_locked_cache(job_id, location, staged_cache=staged_cache)

    def _promote_locked_cache(job_id: str, location: dict, *, staged_cache: Path | None) -> tuple[Path, str]:
        from lerobot.data_platform.dataset_results import preserve_results

        if staged_cache is None:
            staged_cache = remote_cache_root / ".jobs" / job_id
            if request.headers.get("X-Job-Attempt"):
                staged_cache /= request.headers["X-Job-Attempt"]
        manifest = staged_cache / "static" / "viewer_manifest.json"
        cache_output_dir = remote_cache_root / location["location_id"]
        owner = {"job_id": job_id, "attempt_id": request.headers.get("X-Job-Attempt")}
        if staged_cache == cache_output_dir or not manifest.is_file():
            receipt = cache_output_dir / ".execution-owner.json"
            if receipt.is_file() and json.loads(receipt.read_text()) == owner:
                return cache_output_dir, register_remote_cache(location, cache_output_dir)
            raise ValueError("job did not upload viewer_manifest.json")
        previous = Path((location.get("metadata") or {}).get("cache_root") or cache_output_dir)
        if previous.resolve() != staged_cache.resolve():
            if not previous.resolve().is_relative_to(remote_cache_root):
                raise ValueError("Registered cache is outside the configured cache root")
            preserve_results(previous, staged_cache)
        (staged_cache / ".execution-owner.json").write_text(json.dumps(owner))
        backup = cache_output_dir.with_name(f".{cache_output_dir.name}.backup-{uuid.uuid4().hex}")
        if cache_output_dir.exists():
            os.replace(cache_output_dir, backup)
        try:
            os.replace(staged_cache, cache_output_dir)
            viewer_url = register_remote_cache(location, cache_output_dir)
        except Exception:
            if cache_output_dir.exists():
                os.replace(cache_output_dir, staged_cache)
            if backup.exists():
                os.replace(backup, cache_output_dir)
            raise
        return cache_output_dir, viewer_url

    @app.route("/api/agents/jobs/<string:job_id>/complete", methods=["POST"])
    @serialize_completion(remote_cache_root)
    def agent_complete_job(job_id: str):
        node, error = _node_or_error()
        if error:
            return error
        body = request.get_json(silent=True) or {}
        status = str(body.get("status") or "")
        result = dict(body.get("result") or {})
        try:
            current = store.get_job(job_id)
            if current["node_id"] != node["node_id"]:
                raise KeyError(job_id)
            if current["status"] in {"done", "error", "cancelled"}:
                if current["status"] != status:
                    raise JobConflictError("Conflicting terminal status")
                return jsonify({"job": current})
            if status == "done" and request.headers.get("X-Job-Attempt"):
                store.job_manager.checkpoint(
                    job_id, node["node_id"], **_execution_credentials(), phase="finalizing"
                )
            if status == "done" and current["operation"] == "viewer.prepare":
                task_config = (current.get("options") or {}).get("task_config")
                location = store.get_location(current["location_id"])
                cache_output_dir = remote_cache_root / ".jobs" / job_id
                if request.headers.get("X-Job-Attempt"):
                    cache_output_dir = remote_cache_root / ".jobs" / job_id / request.headers["X-Job-Attempt"]
                archive = remote_cache_root / ".history" / location["location_id"] / job_id
                if request.headers.get("X-Job-Attempt"):
                    archive /= request.headers["X-Job-Attempt"]
                owner = {"job_id": job_id, "attempt_id": request.headers.get("X-Job-Attempt")}
                manifest = cache_output_dir / "static" / "viewer_manifest.json"
                if not manifest.is_file():
                    for published in (remote_cache_root / current["location_id"], archive):
                        receipt = published / ".execution-owner.json"
                        if receipt.is_file() and json.loads(receipt.read_text()) == owner:
                            cache_output_dir = published
                            manifest = published / "static" / "viewer_manifest.json"
                            break
                    if not manifest.is_file():
                        raise ValueError("viewer job did not upload viewer_manifest.json")
                if task_config:
                    from lerobot.data_platform.task_catalog import TaskConfigSnapshot

                    recorded = TaskConfigSnapshot.from_dict(
                        json.loads(manifest.read_text()).get("task_config")
                    )
                    if recorded.to_dict()["digest"] != task_config["digest"]:
                        raise ValueError("Agent cache task configuration does not match the queued job")
                desired = (
                    task_catalog_store.snapshot(location["dataset_key"]).to_dict()
                    if task_catalog_store
                    else None
                )
                if task_config and desired and task_config["digest"] != desired["digest"]:
                    result["task_config_stale"] = True
                    if cache_output_dir not in {archive, remote_cache_root / location["location_id"]}:
                        archive.parent.mkdir(parents=True, exist_ok=True)
                        if archive.exists():
                            raise ValueError("Archived viewer result already exists")
                        atomic_json(cache_output_dir / ".execution-owner.json", owner)
                        os.replace(cache_output_dir, archive)
                        cache_output_dir = archive
                    result["cache_root"] = str(cache_output_dir)
                else:
                    cache_output_dir, viewer_url = _promote_derived_cache(
                        job_id, location, staged_cache=cache_output_dir
                    )
                    store.mark_viewer_ready(
                        current["location_id"],
                        viewer_url=viewer_url,
                        cache_root=str(cache_output_dir),
                    )
                    result["viewer_url"] = viewer_url
            if status == "done" and current["operation"] == "caption.annotate":
                complete_caption = app.extensions.get("data_platform_complete_caption")
                if complete_caption is None:
                    raise ValueError("Caption completion handler is unavailable")
                result = complete_caption(current, result)
            if status == "done" and current["operation"].startswith("curation."):
                complete_curation = app.extensions.get("data_platform_complete_curation")
                if complete_curation is None:
                    raise ValueError("Curation completion handler is unavailable")
                result = complete_curation(current, result)
            derived = result.get("dataset_location")
            if status == "done" and isinstance(derived, dict):
                synced_location = store.sync_locations(node["node_id"], [derived])[0]
                result["synced_location"] = synced_location
                result["output_location_id"] = synced_location["location_id"]
                if current["operation"] in _DERIVED_VIEWER_OPERATIONS and (
                    current["operation"] != "preprocess.split"
                    or result.get("viewer_cache")
                    or profile_from_info(derived.get("metadata") or {}).robot_profile == "umi"
                ):
                    cache_output_dir, viewer_url = _promote_derived_cache(job_id, synced_location)
                    refreshed_location = store.mark_viewer_ready(
                        synced_location["location_id"],
                        viewer_url=viewer_url,
                        cache_root=str(cache_output_dir),
                    )
                    result["synced_location"] = refreshed_location
                    result["viewer_url"] = viewer_url
            elif status != "done":
                shutil.rmtree(remote_cache_root / ".jobs" / job_id, ignore_errors=True)
            if (
                status == "done"
                and current["operation"].startswith("mutation.")
                and result.get("source_changed", True)
            ):
                cache_output_dir = remote_cache_root / current["location_id"]
                result["synced_location"] = store.mark_viewer_stale(current["location_id"])
                if cache_output_dir.exists():
                    stale_cache = cache_output_dir.with_name(
                        f".{cache_output_dir.name}.stale-{uuid.uuid4().hex}"
                    )
                    os.replace(cache_output_dir, stale_cache)
                    shutil.rmtree(stale_cache, ignore_errors=True)
            if status == "done" and current["operation"].startswith("local.request."):
                if not node.get("name", "").startswith("local-"):
                    raise PermissionError("Local completion requires the local executor")
                complete_local = app.extensions.get("data_platform_complete_local")
                if complete_local is None:
                    raise ValueError("Local completion handler is unavailable")
                complete_local(result)
            completed = store.complete_job(
                job_id,
                node_id=node["node_id"],
                status=status,
                result=result,
                error=body.get("error"),
                **_execution_credentials(),
            )
            if current["operation"] == "caption.annotate":
                cleanup_caption = app.extensions.get("data_platform_cleanup_caption")
                if cleanup_caption is not None:
                    cleanup_caption(current)
        except KeyError:
            return jsonify({"error": "job not found for this node"}), 404
        except JobConflictError:
            raise
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"job": completed})

    @app.route("/api/control/nodes")
    def control_nodes():
        return jsonify({"nodes": store.list_nodes()})

    @app.route("/api/control/locations")
    def control_locations():
        allowed = set(store.mutation_location_ids(_current_user()["user_id"]))
        return jsonify(
            {
                "locations": [
                    {**location, "source_mutation_allowed": location["location_id"] in allowed}
                    for location in store.list_locations()
                ]
            }
        )

    @app.route("/api/control/jobs")
    def control_jobs():
        return jsonify(
            {
                "jobs": [
                    store.job_manager.decorate(job, _current_user())
                    for job in store.list_jobs(
                        limit=int(request.args.get("limit") or 200), actor=_current_user()
                    )
                ]
            }
        )

    @app.route("/api/control/jobs/<string:job_id>")
    def control_job(job_id: str):
        try:
            return jsonify({"job": store.job_manager.decorate(store.get_job(job_id), _current_user())})
        except KeyError:
            return jsonify({"error": "job not found"}), 404

    @app.route("/api/control/locations/<string:location_id>/viewer-jobs", methods=["POST"])
    def control_create_viewer_job(location_id: str):
        denied = _role_denied("admin", "data_manager", "operator")
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        allowed = {
            "data_version",
            "downsample",
            "episodes",
            "image_keys",
            "max_frames",
            "overwrite",
            "overwrite_csv",
            "prepare_csv",
            "prepare_videos",
            "prepare_workers",
            "fallback_stage_count",
            "force_recompute_stage",
        }
        options = {key: value for key, value in body.items() if key in allowed}
        try:
            _validate_semantic_operation(location_id, "viewer.prepare", options)
            if task_catalog_store is not None:
                from lerobot.data_platform.task_catalog import TASK_CONFIG_PROTOCOL

                location = store.get_location(location_id)
                snapshot = task_catalog_store.snapshot(location["dataset_key"])
                node = next(row for row in store.list_nodes() if row["node_id"] == location["node_id"])
                if node.get("capabilities", {}).get("task_config_protocol") != TASK_CONFIG_PROTOCOL:
                    return jsonify(error="Upgrade the Agent to support task configuration protocol 1"), 409
                options["task_config"] = snapshot.to_dict()
            job = store.create_job(
                location_id=location_id,
                requested_by=_current_user()["user_id"],
                operation="viewer.prepare",
                options=options,
                idempotency_key=request.headers.get("Idempotency-Key"),
                reuse_active=True,
            )
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except KeyError:
            return jsonify({"error": "dataset location not found"}), 404
        return jsonify({"job": job}), 202

    @app.route("/api/control/locations/<string:location_id>/preprocess-jobs", methods=["POST"])
    def control_create_preprocess_job(location_id: str):
        denied = _role_denied("admin", "data_manager", "operator")
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        op = str(body.get("op") or "").strip()
        options = body.get("options") or {}
        if op not in REMOTE_PREPROCESS_OPS:
            return jsonify({"error": f"unsupported remote preprocess op: {op}"}), 400
        if not isinstance(options, dict):
            return jsonify({"error": "options must be an object"}), 400
        blocked = sorted(_FORBIDDEN_REMOTE_OPTION_KEYS.intersection(options))
        if blocked:
            return jsonify({"error": f"remote path/in-place options are not allowed: {blocked}"}), 400
        if "overwrite_output" in options and op != "standardize":
            return jsonify({"error": "overwrite_output is only supported for standardize"}), 400
        if bool(options.get("overwrite", False)) and op not in {"convert_v3", "standardize"}:
            return jsonify({"error": f"overwrite is not supported for remote {op}"}), 400
        if options.get("delete_episodes") not in (None, "", []) and op != "standardize":
            return jsonify({"error": "delete_episodes is only supported for standardize"}), 400
        try:
            _validate_semantic_operation(location_id, op, options)
            if op == "merge" and isinstance(options.get("source_location_ids"), list):
                for source_id in options["source_location_ids"]:
                    _validate_semantic_operation(source_id, op, options)
                metadata = [
                    store.get_location(sid).get("metadata") or {} for sid in options["source_location_ids"]
                ]
                if any(profile_from_info(item).robot_profile == "umi" for item in metadata):
                    first = metadata[0]
                    if any(
                        profile_from_info(item) != profile_from_info(first)
                        or item.get("fps") != first.get("fps")
                        or item.get("features") != first.get("features")
                        for item in metadata[1:]
                    ):
                        raise ValueError(
                            "UMI merge requires identical robot profile, FPS and full feature semantics"
                        )
        except KeyError:
            return jsonify(error="dataset location not found"), 404
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if op == "merge":
            unexpected = sorted(set(options) - _REMOTE_MERGE_OPTION_KEYS)
            if unexpected:
                return jsonify({"error": f"unsupported remote merge options: {unexpected}"}), 400
            source_ids = options.get("source_location_ids")
            if (
                not isinstance(source_ids, list)
                or len(source_ids) < 2
                or any(not isinstance(value, str) or not value for value in source_ids)
                or len(set(source_ids)) != len(source_ids)
                or source_ids[0] != location_id
            ):
                return jsonify(
                    {
                        "error": "source_location_ids must contain at least two distinct locations, starting with the job location"
                    }
                ), 400
            from lerobot.data_platform.merge_options import validate_alignment_options

            try:
                validate_alignment_options(
                    options.get("dimension_policy", "strict"),
                    options.get("dimension_names"),
                    options.get("padding_value", 0),
                    len(source_ids),
                    options.get("dimension_indices"),
                )
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            if "dry_run" in options and not isinstance(options["dry_run"], bool):
                return jsonify({"error": "dry_run must be a boolean"}), 400
            if "workers" in options and (type(options["workers"]) is not int or options["workers"] < 1):
                return jsonify({"error": "workers must be a positive integer"}), 400
            excluded = options.get("exclude_episodes")
            if excluded is not None and (not isinstance(excluded, list) or len(excluded) != len(source_ids)):
                return jsonify({"error": "exclude_episodes must align with source_location_ids"}), 400
            try:
                sources = [store.get_location(source_id) for source_id in source_ids]
                if len({source["node_id"] for source in sources}) != 1:
                    raise ValueError("merge sources must belong to the same Agent")
                if any(source["state"] != "available" for source in sources):
                    raise ValueError("all merge sources must be available")
                node = next(item for item in store.list_nodes() if item["node_id"] == sources[0]["node_id"])
                if "preprocess.merge" not in (node.get("capabilities", {}).get("operations") or []):
                    return jsonify(
                        {"error": "this Agent does not support merge; upgrade and restart it first"}
                    ), 409
                if (
                    options.get("dimension_indices") is not None
                    and (node.get("capabilities") or {}).get("merge_alignment_protocol", 0) < 3
                ):
                    return jsonify(
                        error="Upgrade this Agent to support index mapping (merge alignment protocol 3)"
                    ), 409
                if (
                    options.get("dimension_policy") == "pad" or options.get("dimension_names") is not None
                ) and (node.get("capabilities") or {}).get("merge_alignment_protocol", 0) < 2:
                    return jsonify(
                        error="Upgrade this Agent to support explicit dimension mapping and padding (merge alignment protocol 2)"
                    ), 409
                if str(options.get("out_root") or "").strip():
                    for source_id in source_ids:
                        _validate_remote_output_path(store, source_id, options["out_root"])
            except KeyError:
                return jsonify({"error": "merge source location not found"}), 404
            except (PermissionError, ValueError) as exc:
                return jsonify({"error": str(exc)}), 400
            options = {
                **options,
                "_source_locations": [
                    {
                        **{
                            key: source[key]
                            for key in ("location_id", "node_id", "dataset_key", "root", "output_dir")
                        },
                        "metadata": {"stage": (source.get("metadata") or {}).get("stage")},
                    }
                    for source in sources
                ],
            }
        if str(options.get("out_root") or "").strip():
            try:
                options = dict(options)
                options["out_root"] = _validate_remote_output_path(
                    store,
                    location_id,
                    options["out_root"],
                )
            except KeyError:
                return jsonify({"error": "dataset location or Agent not found"}), 404
            except (PermissionError, ValueError) as exc:
                return jsonify({"error": str(exc)}), 400
        try:
            job = store.create_job(
                location_id=location_id,
                requested_by=_current_user()["user_id"],
                operation=f"preprocess.{op}",
                options=options,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        except KeyError:
            return jsonify({"error": "dataset location not found"}), 404
        return jsonify({"job": job}), 202

    @app.route("/api/control/locations/<string:location_id>/mutation-jobs", methods=["POST"])
    def control_create_mutation_job(location_id: str):
        denied = _role_denied("admin", "data_manager")
        if denied:
            return denied
        if not legacy_mutations_enabled:
            return jsonify({"error": "remote source mutations are disabled on the central server"}), 403
        body = request.get_json(silent=True) or {}
        op = str(body.get("op") or "").strip()
        if op not in REMOTE_SOURCE_MUTATION_OPS:
            return jsonify({"error": f"unsupported remote source mutation: {op}"}), 400
        options = body.get("options") or {}
        if not isinstance(options, dict):
            return jsonify({"error": "options must be an object"}), 400
        unexpected = sorted(set(options) - _REMOTE_MUTATION_OPTION_KEYS[op])
        if unexpected:
            return jsonify({"error": f"unsupported remote mutation options: {unexpected}"}), 400
        try:
            location = store.get_location(location_id)
        except KeyError:
            return jsonify({"error": "dataset location not found"}), 404
        if location_id not in store.mutation_location_ids(_current_user()["user_id"]):
            return jsonify(error="Source mutation permission is required for this dataset location"), 403
        if location["state"] != "available":
            return jsonify(error="Dataset location is not available"), 409
        node = next(
            (item for item in store.list_nodes() if item["node_id"] == location["node_id"]),
            None,
        )
        if not node or not node.get("capabilities", {}).get("source_mutations_enabled"):
            return jsonify({"error": "source mutations are disabled on this Agent"}), 409
        expected_confirmation = f"MUTATE {location['dataset_key']}"
        if str(body.get("confirmation") or "") != expected_confirmation:
            return jsonify({"error": f"confirmation must exactly match: {expected_confirmation}"}), 409
        reason = str(options.get("reason") or "").strip()
        if op in {"delete_episodes", "value_edit", "trim_episode"} and not reason:
            return jsonify({"error": "reason is required for remote source mutations"}), 400
        if len(reason) > 500:
            return jsonify({"error": "reason must be 500 characters or fewer"}), 400
        if op == "trim_episode":
            if "mutation.trim_episode" not in node.get("capabilities", {}).get("operations", []):
                return jsonify({"error": "Upgrade this Agent to enable Trim Apply"}), 409
            episode, start, end = (options.get(key) for key in ("episode_id", "start_frame", "end_frame"))
            if (
                any(type(value) is not int for value in (episode, start, end))
                or episode < 0
                or start < 0
                or end < start
            ):
                return jsonify(
                    {"error": "A valid episode_id and inclusive integer frame range are required"}
                ), 400
        if op == "delete_episodes":
            episodes = options.get("episodes")
            if (
                not isinstance(episodes, list)
                or not 1 <= len(episodes) <= 10000
                or any(type(value) is not int or value < 0 for value in episodes)
            ):
                return jsonify(error="Select 1-10000 explicit non-negative episode indices"), 400
        if op == "value_edit" and not isinstance(options.get("edits"), list):
            return jsonify({"error": "edits must be an array"}), 400
        job_options = dict(options)
        job_options["requested_by"] = {
            "user_id": _current_user()["user_id"],
            "username": _current_user()["username"],
        }
        try:
            job = store.create_job(
                location_id=location_id,
                requested_by=_current_user()["user_id"],
                operation=f"mutation.{op}",
                options=job_options,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        except KeyError:
            return jsonify({"error": "dataset location not found"}), 404
        return jsonify({"job": job}), 202

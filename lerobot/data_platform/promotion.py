"""Restricted bridge from development administrators to the existing release installer."""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import socket
import struct
import subprocess
from pathlib import Path

import requests

SOCKET = "/run/data-platform-promotion/control.sock"
UNIT = "data-platform-production-promotion"


def snapshot():
    from lerobot.data_platform.releases import RELEASE_ROOT, digest, validate_version

    environments = {}
    for name, port in (("dev", 9092), ("prod", 9091)):
        try:
            response = requests.get(f"http://127.0.0.1:{port}/healthz", timeout=3)
            response.raise_for_status()
            health = response.json()
            environments[name] = {
                key: health.get(key) for key in ("environment", "release", "maintenance", "instance_id")
            }
        except (requests.RequestException, ValueError):
            environments[name] = {"environment": "unavailable", "release": None}
    dev, prod = environments["dev"], environments["prod"]
    reason = None
    revision = None
    try:
        version = validate_version(dev.get("release") or "")
        root = RELEASE_ROOT / version
        manifest = json.loads((root / "release.json").read_text())
        revision = digest(root / "release.json")
        dev["commit"] = manifest.get("commit")
        receipt = json.loads((root / "approval.json").read_text())
        approved = (
            receipt.get("environment") == "dev"
            and receipt.get("manifest_sha256") == revision
            and receipt.get("instance_id") == dev.get("instance_id")
        )
    except (OSError, ValueError):
        approved = False
    state = subprocess.run(
        ["systemctl", "show", UNIT, "--property=ActiveState", "--value"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if prod.get("environment") != "prod":
        reason = (
            "Production is unavailable or still uses legacy deployment. Complete production migration first."
        )
    elif dev.get("environment") != "dev":
        reason = "Development is unavailable."
    elif state in {"activating", "active", "deactivating"}:
        reason = "Production deployment is in progress."
    elif dev.get("maintenance") or prod.get("maintenance"):
        reason = "An environment is under maintenance."
    elif dev.get("release") == prod.get("release"):
        reason = "Both environments run the same release."
    elif not approved:
        reason = "This development release requires acceptance approval before production deployment."
    comparison = "unknown"
    if dev.get("environment") == "dev" and prod.get("environment") == "prod":
        comparison = "same" if dev.get("release") == prod.get("release") else "different"
    return {
        **environments,
        "revision": revision,
        "can_promote": reason is None,
        "reason": reason,
        "deployment_state": state or "inactive",
        "comparison": comparison,
    }


def handle_request(payload):
    """Root-owned, no shell arguments; the client cannot select commands or paths."""
    import fcntl

    if os.geteuid() != 0:
        raise PermissionError("Root helper required")
    if payload.get("action") == "status":
        return snapshot()
    if payload.get("action") != "promote":
        raise ValueError("Unsupported action")
    with Path("/run/data-platform-promotion.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = snapshot()
        if not state["can_promote"] or payload.get("revision") != state["revision"]:
            raise ValueError(state["reason"] or "Development release changed; refresh and confirm again")
        from lerobot.data_platform.releases import verify_release

        version = state["dev"]["release"]
        verify_release(version, production=True)
        subprocess.run(["systemctl", "reset-failed", UNIT], capture_output=True, check=False)
        subprocess.run(
            [
                "systemd-run",
                "--quiet",
                "--collect",
                f"--unit={UNIT}",
                "/opt/data-platform/dev/current/.venv/bin/python",
                "-I",
                "-m",
                "lerobot.data_platform.release_cli",
                "deploy",
                "--env",
                "prod",
                "--release",
                version,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return {"status": "started", "release": version}


def call_helper(payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(SOCKET)
        client.sendall(json.dumps(payload).encode() + b"\n")
        with client.makefile("rb") as stream:
            result = json.loads(stream.readline(65536))
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


def serve():
    account = pwd.getpwnam("data-platform-dev")
    path = Path(SOCKET)
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(SOCKET)
        os.chown(SOCKET, 0, account.pw_gid)
        os.chmod(SOCKET, 0o660)
        server.listen(8)
        while True:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(10)
                _, uid, _ = struct.unpack(
                    "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid not in {0, account.pw_uid}:
                    continue
                try:
                    with connection.makefile("rb") as stream:
                        line = stream.readline(4097)
                        if len(line) > 4096:
                            raise ValueError("Request too large")
                        result = handle_request(json.loads(line))
                except Exception:
                    result = {
                        "error": "Deployment request rejected; refresh status and check deployment prerequisites."
                    }
                with contextlib.suppress(OSError):
                    connection.sendall(json.dumps(result).encode() + b"\n")


def register_promotion_routes(app, store):
    from flask import g, jsonify, request

    from lerobot.data_platform.environment import EnvironmentIdentity

    identity = EnvironmentIdentity.from_env()
    if not identity or identity.name != "dev":
        return

    @app.route("/api/dev/production", methods=["GET", "POST"])
    def production():
        actor = getattr(g, "control_plane_user", None)
        if not actor:
            return jsonify(error="Authentication required"), 401
        if request.method == "POST" and (actor["role"] != "admin" or actor.get("original_actor")):
            return jsonify(error="A development administrator session is required"), 403
        try:
            if request.method == "GET":
                return jsonify(call_helper({"action": "status"}))
            body = request.get_json(silent=True) or {}
            if body.get("confirm") is not True or not isinstance(body.get("revision"), str):
                return jsonify(error="Confirm the selected release before deploying"), 400
            from lerobot.data_platform.management_storage import enqueue_event
            from lerobot.data_platform.operation_log import build_operation_event

            # Persist the actor and exact artifact before crossing the privileged boundary.
            with store.sessions.begin() as session:
                enqueue_event(
                    session,
                    build_operation_event(
                        "deployment.production.requested",
                        status="requested",
                        actor={key: actor[key] for key in ("user_id", "username", "role")},
                        details={"manifest_sha256": body["revision"]},
                    ),
                )
            result = call_helper({"action": "promote", "revision": body["revision"]})
            return jsonify(result), 202
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            return jsonify(
                error="Deployment service unavailable. Install the deployment commands and refresh."
            ), 503


if __name__ == "__main__":
    serve()

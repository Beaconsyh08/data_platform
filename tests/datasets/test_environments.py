"""Real database/session tests for environment isolation and development role switching."""

import uuid
from pathlib import Path

import pytest
from flask import Flask, g, jsonify
from sqlalchemy import create_engine, inspect

from lerobot.data_platform.control_plane import ControlPlaneStore
from lerobot.data_platform.dev_roles import seed_test_users
from lerobot.data_platform.environment import (
    check_server_environment,
    session_cookie_name,
    verify_database_environment,
)
from lerobot.data_platform.maintenance import set_maintenance
from lerobot.data_platform.routes.control_plane import register_control_plane_auth_routes


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for name, value in {
        "DATA_PLATFORM_ENV": "dev",
        "DATA_PLATFORM_INSTANCE_ID": str(uuid.uuid4()),
        "DATA_PLATFORM_ENABLE_DEV_ROLE_SWITCH": "1",
        "DATA_PLATFORM_STATE_ROOT": str(tmp_path / "dev"),
        "DATA_PLATFORM_ROOT": str(tmp_path / "dev/datasets"),
        "DATA_PLATFORM_OUTPUT_DIR": str(tmp_path / "dev/console"),
        "DATA_PLATFORM_REMOTE_CACHE_ROOT": str(tmp_path / "dev/remote-cache"),
        "DATA_PLATFORM_DATABASE_URL": f"sqlite:///{tmp_path}/control.db",
        "DATA_PLATFORM_LOG_DATABASE_URL": f"sqlite:///{tmp_path}/logs.db",
    }.items():
        monkeypatch.setenv(name, value)
    check_server_environment(initialize=True)
    return tmp_path


def app_and_store(root):
    store = ControlPlaneStore(f"sqlite:///{root}/control.db", initialize_schema=True)
    app = Flask(__name__, template_folder=str(Path(__file__).parents[2] / "lerobot/data_platform/templates"))
    app.testing = True
    register_control_plane_auth_routes(app, store, bootstrap_token="bootstrap", allow_registration=False)

    @app.post("/api/test/write")
    def write():
        return jsonify(g.control_plane_user)

    return app, store


def post(client, path, payload=None, **kwargs):
    status = client.get("/api/auth/status", base_url="https://localhost:8443").json
    return client.post(
        path,
        json=payload or {},
        base_url="https://localhost:8443",
        headers={
            "Origin": "https://localhost:8443",
            "X-Data-Platform-CSRF": status["csrf_token"],
        },
        **kwargs,
    )


def bootstrap(client):
    return post(
        client,
        "/api/auth/bootstrap",
        {
            "username": "administrator",
            "password": "long-test-password",
            "bootstrap_token": "bootstrap",
        },
    )


def test_identity_check_precedes_business_schema_writes(environment, monkeypatch):
    engine = create_engine(f"sqlite:///{environment}/unknown.db")
    with pytest.raises(RuntimeError, match="not initialized"):
        ControlPlaneStore(str(engine.url))
    assert inspect(engine).get_table_names() == []
    monkeypatch.setenv("DATA_PLATFORM_INSTANCE_ID", str(uuid.uuid4()))
    with pytest.raises(RuntimeError, match="mismatch"):
        ControlPlaneStore(f"sqlite:///{environment}/control.db")
    assert "dp_users" not in inspect(create_engine(f"sqlite:///{environment}/control.db")).get_table_names()


def test_control_logs_cannot_be_swapped_and_legacy_cannot_adopt(environment, monkeypatch):
    engine = create_engine(f"sqlite:///{environment}/logs.db")
    with pytest.raises(RuntimeError, match="mismatch"):
        verify_database_environment(engine, "control")
    monkeypatch.delenv("DATA_PLATFORM_ENV")
    monkeypatch.delenv("DATA_PLATFORM_ENABLE_DEV_ROLE_SWITCH")
    with pytest.raises(RuntimeError, match="explicit environment"):
        verify_database_environment(engine, "logs")


def test_directory_alias_and_production_role_switch_rejected(environment, monkeypatch):
    link = environment / "dev/link"
    link.symlink_to(environment / "dev/console")
    monkeypatch.setenv("DATA_PLATFORM_OUTPUT_DIR", str(link))
    with pytest.raises(RuntimeError, match="symlink"):
        check_server_environment()
    monkeypatch.setenv("DATA_PLATFORM_ENV", "prod")
    with pytest.raises(RuntimeError, match="only available"):
        check_server_environment()


def test_real_role_switch_and_return_do_not_modify_administrator(environment):
    app, store = app_and_store(environment)
    client = app.test_client()
    admin = bootstrap(client).json["user"]
    seed_test_users(store)
    seed_test_users(store)
    result = post(client, "/api/dev/role-session", {"identity": "viewer"})
    assert result.status_code == 200
    viewer = result.json["user"]
    assert viewer["role"] == "viewer"
    assert viewer["original_actor"]["user_id"] == admin["user_id"]
    assert post(client, "/api/test/write").status_code == 403
    assert client.get("/api/auth/users", base_url="https://localhost:8443").status_code == 403
    result = post(client, "/api/dev/role-session", {"identity": "operator_a"})
    assert result.json["user"]["role"] == "operator"
    assert post(client, "/api/test/write").json["user_id"] == result.json["user"]["user_id"]
    assert (
        post(client, "/api/dev/role-session", {"identity": "admin"}).json["user"]["user_id"]
        == admin["user_id"]
    )
    assert next(user for user in store.list_users() if user["user_id"] == admin["user_id"])["role"] == "admin"
    assert post(client, "/api/dev/role-session", {"identity": "arbitrary-user"}).status_code == 400


def test_cookie_origin_and_csrf_are_environment_specific(environment):
    app, _ = app_and_store(environment)
    client = app.test_client()
    result = bootstrap(client)
    assert "data_platform_session_dev=" in result.headers["Set-Cookie"]
    assert "HttpOnly" in result.headers["Set-Cookie"] and "Secure" in result.headers["Set-Cookie"]
    assert client.post("/api/test/write", base_url="https://localhost:8443").status_code == 403
    token = client.get("/api/auth/status", base_url="https://localhost:8443").json["csrf_token"]
    assert (
        client.post(
            "/api/test/write",
            base_url="https://localhost:8443",
            headers={
                "Origin": "https://localhost",
                "X-Data-Platform-CSRF": token,
            },
        ).status_code
        == 403
    )
    client.set_cookie("data_platform_session_prod", "unrelated-production-session")
    assert post(client, "/api/auth/logout").status_code == 200
    assert client.get_cookie("data_platform_session_prod").value == "unrelated-production-session"
    assert client.get_cookie(session_cookie_name()) is None


def test_plain_operator_cannot_switch_and_prod_has_no_route(environment, monkeypatch):
    app, store = app_and_store(environment)
    client = app.test_client()
    bootstrap(client)
    store.register_user(
        username="ordinary", password="long-test-password", display_name="Ordinary", role="operator"
    )
    post(client, "/api/auth/logout")
    post(client, "/api/auth/login", {"username": "ordinary", "password": "long-test-password"})
    assert post(client, "/api/dev/role-session", {"identity": "admin"}).status_code == 403
    monkeypatch.setenv("DATA_PLATFORM_ENABLE_DEV_ROLE_SWITCH", "0")
    monkeypatch.setenv("DATA_PLATFORM_ENV", "prod")
    prod_app = Flask("prod")
    register_control_plane_auth_routes(prod_app, store, bootstrap_token="", allow_registration=False)
    assert "/api/dev/role-session" not in {str(rule) for rule in prod_app.url_map.iter_rules()}


def test_maintenance_blocks_submission_and_claims_but_preserves_logout(environment):
    app, store = app_and_store(environment)
    client = app.test_client()
    admin = bootstrap(client).json["user"]
    _, node = store.enroll_node(
        name="dev-agent",
        hostname="test",
        allowed_roots=[],
        writable_roots=[],
        capabilities={
            "job_protocol": 2,
            "environment": "dev",
            "instance_id": __import__("os").environ["DATA_PLATFORM_INSTANCE_ID"],
        },
        enrollment_token="x",
        expected_token="x",
    )
    location = store.sync_locations(
        node["node_id"], [{"dataset_key": "test/data", "root": "/dev/sample", "metadata": {}}]
    )[0]
    first = store.create_job(
        location_id=location["location_id"], requested_by=admin["user_id"], operation="viewer.prepare"
    )
    set_maintenance(store, True)
    assert post(client, "/api/test/write").status_code == 503
    assert store.claim_job(node["node_id"], worker_instance_id="test-worker") is None
    from lerobot.data_platform.maintenance import MaintenanceError

    with pytest.raises(MaintenanceError):
        store.create_job(
            location_id=location["location_id"], requested_by=admin["user_id"], operation="viewer.prepare"
        )
    assert store.get_job(first["job_id"])["status"] == "queued"
    assert post(client, "/api/auth/logout").status_code == 200
    set_maintenance(store, False)
    assert store.claim_job(node["node_id"], worker_instance_id="test-worker") is not None


def test_disabled_test_identity_can_return_without_gaining_business_permissions(environment):
    app, store = app_and_store(environment)
    client = app.test_client()
    bootstrap(client)
    seed_test_users(store)
    viewer = post(client, "/api/dev/role-session", {"identity": "viewer"}).json["user"]
    store.update_user(viewer["user_id"], active=False)
    assert post(client, "/api/test/write").status_code == 403
    assert post(client, "/api/dev/role-session", {"identity": "admin"}).json["user"]["role"] == "admin"


def test_agent_checks_remote_identity_before_enrollment(environment):
    from lerobot.data_platform.agent import DataPlatformAgent
    from lerobot.data_platform.environment import verify_directory

    state = environment / "agent"
    verify_directory(state, "agent", initialize=True)

    class Client:
        server_url = "https://dev.test"

        def _request(self, method, path):
            return {"environment": "prod", "instance_id": str(uuid.uuid4())}

        def enroll(self, payload):
            pytest.fail("Foreign environment must be rejected before enrollment")

    with pytest.raises(RuntimeError, match="identities"):
        DataPlatformAgent(
            client=Client(),
            state_path=state / "agent.json",
            name="dev-agent",
            allowed_roots=[],
            writable_roots=[],
            enrollment_token="test",
        )
    assert not (state / "agent.json").exists()


def test_browser_wrapper_fetches_current_csrf_for_writes(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("Node.js required for browser request test")
    script = Path(__file__).parents[2] / "lerobot/data_platform/static/environment.js"
    harness = """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
let calls = [];
let csrf = 'first';
const context = {
    window: {fetch: async (url, init) => {
        calls.push({url, init});
        return {ok: true, json: async () => ({csrf_token: csrf})};
    }, addEventListener() {}},
    document: {addEventListener() {}},
    location: {href: 'https://example.test:8443/', origin: 'https://example.test:8443'},
    Headers, Request, URL,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
(async () => {
    await context.window.fetch('/api/test', {method: 'POST'});
    assert.equal(calls.length, 2);
    assert.equal(calls[1].init.headers.get('X-Data-Platform-CSRF'), 'first');
    csrf = 'new-session';
    await context.window.fetch('/api/test', {method: 'DELETE'});
    assert.equal(calls[3].init.headers.get('X-Data-Platform-CSRF'), 'new-session');
    await context.window.fetch('https://example.test/api/test', {method: 'POST'});
    assert.equal(calls.length, 5);
    assert.equal(calls[4].init.headers, undefined);
})().catch(error => { console.error(error); process.exit(1); });
"""
    target = tmp_path / "test.cjs"
    target.write_text(harness)
    subprocess.run(["node", str(target), str(script)], check=True, capture_output=True, text=True, timeout=10)


def test_production_promotion_requires_real_admin_and_confirmation(environment, monkeypatch):
    from lerobot.data_platform import promotion

    app, store = app_and_store(environment)
    client = app.test_client()
    calls = []
    monkeypatch.setattr(
        promotion, "call_helper", lambda payload: calls.append(payload) or {"status": "started"}
    )
    assert post(client, "/api/dev/production", {"confirm": True, "revision": "abc"}).status_code == 401
    bootstrap(client)
    assert post(client, "/api/dev/production", {"revision": "abc"}).status_code == 400
    assert not calls
    assert post(client, "/api/dev/production", {"confirm": True, "revision": "abc"}).status_code == 202
    assert calls == [{"action": "promote", "revision": "abc"}]
    seed_test_users(store)
    assert post(client, "/api/dev/role-session", {"identity": "operator_a"}).status_code == 200
    assert post(client, "/api/dev/production", {"confirm": True, "revision": "abc"}).status_code == 403
    assert len(calls) == 1


def test_promotion_rejects_stale_confirmation(tmp_path, monkeypatch):
    from lerobot.data_platform import promotion

    monkeypatch.setattr(promotion.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        promotion, "snapshot", lambda: {"can_promote": True, "revision": "new", "reason": None}
    )
    original = promotion.Path
    monkeypatch.setattr(
        promotion, "Path", lambda value: tmp_path / "lock" if value.endswith(".lock") else original(value)
    )
    with pytest.raises(ValueError, match="changed"):
        promotion.handle_request({"action": "promote", "revision": "old"})


@pytest.mark.parametrize("production_environment,expected", [("legacy", False), ("prod", True)])
def test_promotion_snapshot_blocks_legacy_production(tmp_path, monkeypatch, production_environment, expected):
    import json
    from types import SimpleNamespace

    from lerobot.data_platform import promotion, releases

    root = tmp_path / "R2"
    root.mkdir()
    (root / "release.json").write_text(json.dumps({"commit": "abc"}))
    (root / "approval.json").write_text(
        json.dumps(
            {
                "environment": "dev",
                "instance_id": "dev-id",
                "manifest_sha256": releases.digest(root / "release.json"),
            }
        )
    )
    monkeypatch.setattr(releases, "RELEASE_ROOT", tmp_path)

    def get(url, **kwargs):
        dev = ":9092/" in url
        value = {
            "environment": "dev" if dev else production_environment,
            "release": "R2" if dev else "R1",
            "instance_id": "dev-id" if dev else "prod-id",
        }
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)

    monkeypatch.setattr(promotion.requests, "get", get)
    monkeypatch.setattr(promotion.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="inactive"))
    state = promotion.snapshot()
    assert state["can_promote"] is expected
    assert state["comparison"] == ("different" if expected else "unknown")

"""MCP transport, isolated credentials and model-free, confirmed-only operations."""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from lerobot.data_platform.ai_tools.actions import confirm
from lerobot.data_platform.ai_tools.external import ExternalTools
from lerobot.data_platform.ai_tools.models import AssistantTurn
from lerobot.data_platform.ai_tools.service import AssistantConfig, AssistantService
from lerobot.data_platform.control_plane import ControlPlaneUser
from lerobot.data_platform.management_storage import AuditOutbox
from lerobot.data_platform.mcp_access import McpCredential, authenticate, credentials
from lerobot.data_platform.routes.ai_assistant import register_ai_assistant_routes
from lerobot.data_platform.routes.mcp import register_mcp_routes
from tests.datasets.test_ai_assistant_context import bind_lifecycle
from tests.datasets.test_control_plane import _app
from tests.datasets.test_processing import setup as processing_setup  # noqa: F401


@pytest.fixture
def mcp(processing_setup, tmp_path, monkeypatch):  # noqa: F811
    store, manager, actor, node, location = processing_setup
    service = AssistantService(manager, config=AssistantConfig(enabled=False))
    bind_lifecycle(service, tmp_path)
    app = _app(tmp_path, store)
    app.extensions["data_platform_processing"] = manager
    register_ai_assistant_routes(app, SimpleNamespace())
    app.extensions["data_platform_assistant"] = service
    register_mcp_routes(app, SimpleNamespace())
    client = app.test_client()
    credential = credentials(store, actor["user_id"], create={"name": "test", "scopes": ["read", "prepare"]})
    return service, client, actor["user_id"], location["location_id"], credential


def rpc(client, token, method, params=None, **headers):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            **headers,
        },
    )


def invoke(fixture, name, args):
    _, client, _, _, credential = fixture
    response = rpc(client, credential["token"], "tools/call", {"name": name, "arguments": args})
    assert response.status_code == 200, response.data
    return response.json["result"]


def test_protocol_and_model_free_queries(mcp):
    service, client, owner, location, credential = mcp
    token = credential["token"]
    response = rpc(
        client,
        token,
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert response.json["result"]["protocolVersion"] == "2025-11-25"
    names = {t["name"] for t in rpc(client, token, "tools/list").json["result"]["tools"]}
    assert "prepare_action" in names and "confirm_action" not in names
    assert client.get("/mcp", headers={"Authorization": "Bearer " + token}).status_code == 405
    for name, args in [
        ("list_datasets", {}),
        ("read_context", {"location_id": location}),
        ("list_operators", {"location_id": location}),
        ("describe_operator", {"location_id": location, "operator_id": "viewer.prepare"}),
    ]:
        result = invoke(mcp, name, args)
        assert not result["isError"], result
        assert result["structuredContent"]
        assert service.store.get_location(location)["root"] not in json.dumps(result)
    assert not service.store.list_jobs()


def test_credentials_hash_only_revocable_password_bound_and_private(mcp):
    service, client, owner, location, credential = mcp
    token = credential["token"]
    with service.store.sessions() as session:
        row = session.get(McpCredential, credential["credential_id"])
        assert row.digest != token
        assert token not in json.dumps([x.payload for x in session.scalars(select(AuditOutbox))])
    assert token not in json.dumps(credentials(service.store, owner))
    assert authenticate(service.store, token).owner == owner
    credentials(service.store, owner, revoke=credential["credential_id"])
    assert rpc(client, token, "tools/list").status_code == 401
    another = credentials(service.store, owner, create={"name": "another"})
    with service.store.sessions.begin() as session:
        session.get(ControlPlaneUser, owner).password_digest = "changed"
    assert authenticate(service.store, another["token"]) is None


def test_expiry_disabled_and_scope_recheck(mcp):
    service, client, owner, location, credential = mcp
    token = credential["token"]
    with service.store.sessions.begin() as session:
        session.get(McpCredential, credential["credential_id"]).expires_at = int(time.time()) - 1
    assert rpc(client, token, "tools/list").status_code == 401
    fresh = credentials(service.store, owner, create={"name": "read"})
    assert "prepare_action" not in {
        t["name"] for t in rpc(client, fresh["token"], "tools/list").json["result"]["tools"]
    }
    result = rpc(
        client,
        fresh["token"],
        "tools/call",
        {
            "name": "prepare_action",
            "arguments": {
                "location_id": location,
                "operator_id": "viewer.prepare",
                "parameters": {},
                "request_id": str(uuid.uuid4()),
            },
        },
    )
    assert result.json["result"]["isError"]
    with service.store.sessions.begin() as session:
        session.get(ControlPlaneUser, owner).active = False
    assert rpc(client, fresh["token"], "tools/list").status_code == 401


def test_auth_origin_protocol_bounds_and_no_notification_execution(mcp):
    service, client, owner, location, credential = mcp
    token = credential["token"]
    assert client.post("/mcp", json={}).status_code == 401
    assert rpc(client, token, "ping", Origin="https://attacker.test").status_code == 403
    assert rpc(client, token, "ping", **{"MCP-Protocol-Version": "invalid"}).status_code == 400
    assert rpc(client, token, "ping", Accept="text/plain").status_code == 406
    assert rpc(client, token, "anything").json["error"]["code"] == -32601
    assert (
        rpc(client, token, "tools/call", {"name": "shell", "arguments": {}}).json["error"]["code"] == -32602
    )
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "prepare_action", "arguments": {}},
        },
        headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 202
    with service.store.sessions() as session:
        assert session.scalar(select(AssistantTurn)) is None
    assert not service.store.list_jobs()


def test_prepare_durable_idempotent_confirm_only_and_private(mcp):
    service, client, owner, location, credential = mcp
    args = {
        "location_id": location,
        "operator_id": "viewer.prepare",
        "parameters": {},
        "request_id": str(uuid.uuid4()),
    }
    result = invoke(mcp, "prepare_action", args)
    assert not result["isError"], result
    value = result["structuredContent"]
    assert value["requires_user_confirmation"] and value["confirmation_url"].startswith(
        "http://localhost/assistant?"
    )
    assert not service.store.list_jobs() and not service.manager.list(owner)
    retry = invoke(mcp, "prepare_action", args)["structuredContent"]
    assert retry["action_id"] == value["action_id"]
    assert invoke(mcp, "prepare_action", {**args, "parameters": {"downsample": 2}})["isError"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: confirm(service, owner, value["action_id"], value["revision"]), range(2))
        )
    assert results[0]["run_id"] == results[1]["run_id"]
    assert len(service.manager.runs(owner)) == 1
    assert not invoke(mcp, "get_action", {"action_id": value["action_id"]})["isError"]
    other = service.store.register_user(
        username="mcp-other", password="strong-other-password", role="operator"
    )
    service.store.update_user(other["user_id"], active=True)
    other_token = credentials(service.store, other["user_id"], create={"name": "other"})
    response = rpc(
        client,
        other_token["token"],
        "tools/call",
        {"name": "get_action", "arguments": {"action_id": value["action_id"]}},
    )
    assert response.json["result"]["isError"]


def test_concurrent_prepare_and_role_revocation(mcp):
    service, client, owner, location, credential = mcp
    identity = authenticate(service.store, credential["token"])
    tools = ExternalTools(service)
    args = {
        "location_id": location,
        "operator_id": "viewer.prepare",
        "parameters": {},
        "request_id": str(uuid.uuid4()),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tools.call(identity, "prepare_action", args), range(2)))
    assert results[0]["action_id"] == results[1]["action_id"]
    with service.store.sessions.begin() as session:
        session.get(ControlPlaneUser, owner).role = "viewer"
    assert invoke(mcp, "prepare_action", args)["isError"]
    with pytest.raises(PermissionError):
        confirm(service, owner, results[0]["action_id"], 1)


def test_cookie_cannot_call_mcp_and_bearer_cannot_manage_credentials(mcp):
    service, client, owner, location, credential = mcp
    assert (
        client.get(
            "/api/mcp/credentials", headers={"Authorization": "Bearer " + credential["token"]}
        ).status_code
        == 401
    )
    client.post("/api/auth/login", json={"username": "admin", "password": "strong-admin-password"})
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    created = client.post("/api/mcp/credentials", json={"name": "browser", "scopes": ["read"]})
    assert created.status_code == 200 and created.json["token"].startswith("dpmcp_")
    assert created.headers["Cache-Control"] == "no-store"
    listed = client.get("/api/mcp/credentials")
    assert "dpmcp_" not in listed.text
    assert client.delete("/api/mcp/credentials/" + created.json["credential_id"]).json["revoked"]
    assert authenticate(service.store, created.json["token"]) is None


def test_dataset_scope_revocation_hides_context_and_prepared_action(mcp):
    service, client, owner, location, credential = mcp
    from lerobot.data_platform.control_plane import DataMutationScope

    args = {
        "location_id": location,
        "operator_id": "viewer.prepare",
        "parameters": {},
        "request_id": str(uuid.uuid4()),
    }
    action = invoke(mcp, "prepare_action", args)["structuredContent"]
    with service.store.sessions.begin() as session:
        user = session.get(ControlPlaneUser, owner)
        user.role = "operator"
        session.add(DataMutationScope(user_id=owner, all_locations=False))
    assert invoke(mcp, "list_datasets", {})["structuredContent"]["datasets"] == []
    assert invoke(mcp, "read_context", {"location_id": location})["isError"]
    assert invoke(mcp, "get_action", {"action_id": action["action_id"]})["isError"]
    with pytest.raises(PermissionError):
        confirm(service, owner, action["action_id"], 1)


def test_mcp_browser_confirmation_without_platform_model(mcp):
    import os
    import threading

    from werkzeug.serving import make_server

    executable = os.environ.get("DATA_PLATFORM_BROWSER_EXECUTABLE")
    if not executable:
        pytest.skip("Run with DATA_PLATFORM_BROWSER_EXECUTABLE for browser acceptance")
    from playwright.sync_api import sync_playwright

    service, client, owner, location, credential = mcp
    prepared = invoke(
        mcp,
        "prepare_action",
        {
            "location_id": location,
            "operator_id": "viewer.prepare",
            "parameters": {},
            "request_id": str(uuid.uuid4()),
        },
    )["structuredContent"]
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=executable, args=["--no-sandbox"])
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            assert page.request.post(
                base + "/api/auth/login", data={"username": "admin", "password": "strong-admin-password"}
            ).ok
            page.goto(base + prepared["confirmation_url"].removeprefix("http://localhost"))
            page.get_by_role("button", name="Confirm and run", exact=True).wait_for()
            assert not service.store.list_jobs() and not service.manager.runs(owner)
            page.get_by_role("button", name="Confirm and run", exact=True).click()
            page.wait_for_function("document.body.innerText.includes('Task')")
            assert len(service.manager.runs(owner)) == 1
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_official_sdk_streamable_http_client(mcp):
    import os
    import subprocess
    import threading

    from werkzeug.serving import make_server

    executable = os.environ.get("MCP_SDK_PYTHON")
    if not executable:
        pytest.skip("Set MCP_SDK_PYTHON to an isolated official MCP SDK interpreter")
    _, client, _, location, credential = mcp
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = """
import asyncio, json, sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
config=json.load(sys.stdin)
async def main():
    async with streamablehttp_client(config['url'], headers={'Authorization': 'Bearer '+config['token']}) as (read,write,_):
        async with ClientSession(read,write) as session:
            initialized=await session.initialize()
            assert initialized.serverInfo.name=='data-platform'
            tools=await session.list_tools()
            assert any(t.name=='prepare_action' for t in tools.tools)
            datasets=await session.call_tool('list_datasets',{})
            assert not datasets.isError and datasets.structuredContent['datasets']
            action=await session.call_tool('prepare_action', {
                'location_id':config['location'], 'request_id':config['request_id'],
                'operator_id':'viewer.prepare','parameters':{}})
            assert not action.isError and action.structuredContent['requires_user_confirmation']
            state=await session.call_tool('get_action', {'action_id': action.structuredContent['action_id']})
            assert not state.isError
asyncio.run(main())
"""
    try:
        result = subprocess.run(
            [executable, "-c", script],
            input=json.dumps(
                {
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                    "token": credential["token"],
                    "location": location,
                    "request_id": str(uuid.uuid4()),
                }
            ),
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr.replace(credential["token"], "[redacted]")
    finally:
        server.shutdown()
        thread.join(timeout=5)

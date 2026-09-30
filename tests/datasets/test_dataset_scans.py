"""Immediate discovery crosses the browser/Server/Agent boundary without a Job slot."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from lerobot.data_platform.agent import AgentClient
from lerobot.data_platform.management_storage import AuditOutbox, DatasetScan
from lerobot.data_platform.routes.dataset_scans import register_dataset_scan_routes
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_data_platform_agent import _agent, _make_dataset


@pytest.fixture
def scan_platform(tmp_path):
    store = _store(tmp_path)
    app = _app(tmp_path, store)
    register_dataset_scan_routes(app, SimpleNamespace(control_plane_store=store))
    client = app.test_client()
    _bootstrap(client)
    root = tmp_path / "datasets"
    root.mkdir()
    token, node = store.enroll_node(
        name="server-b",
        hostname="server-b.internal",
        allowed_roots=[str(root)],
        writable_roots=[str(tmp_path)],
        capabilities={"dataset_scan_protocol": 1},
        enrollment_token="agent-enrollment-secret",
        expected_token="agent-enrollment-secret",
    )
    return store, app, client, root, node, {"Authorization": f"Bearer {token}"}


def test_scan_request_discovers_new_dataset_with_all_job_slots_busy(scan_platform, tmp_path, monkeypatch):
    from threading import Event, Thread

    store, app, client, root, node, headers = scan_platform
    _make_dataset(root / "new")
    response = client.post(f"/api/control/nodes/{node['node_id']}/scans", json={"path": str(root)})
    assert response.status_code == 202
    request = response.get_json()["scan"]
    assert store.list_locations() == []

    class Client(AgentClient):
        def __init__(self):
            super().__init__("https://server-a.example")
            self.web = app.test_client()

        def _request(self, method, path, **kwargs):
            kwargs.pop("token", None)
            response = self.web.open(path, method=method, headers=headers, **kwargs)
            assert response.status_code == 200, response.get_json()
            return response.get_json()

    agent = _agent(tmp_path, root, tmp_path)
    agent.client = Client()
    monkeypatch.setattr(agent, "capabilities", lambda: {"dataset_scan_protocol": 1})
    monkeypatch.setattr(agent.client, "claim", lambda *a, **kw: pytest.fail("Scan must not claim a Job"))
    release, entered = Event(), Event()

    def compute():
        entered.set()
        assert release.wait(5)

    worker = Thread(target=compute)
    worker.start()
    try:
        assert entered.wait(1)
        assert (
            agent.run_once(sync=False, dispatch=lambda job: None, allow_claim=False, sync_results=False)
            is False
        )
        assert worker.is_alive()
        final = client.get(f"/api/control/scans/{request['request_id']}").get_json()["scan"]
        assert final["status"] == "done" and final["dataset_count"] == 1
        assert [row["root"] for row in store.list_locations()] == [str(root / "new")]
        assert store.list_jobs() == []
        with store.sessions() as session:
            events = [row.payload for row in session.scalars(select(AuditOutbox))]
        assert [e["status"] for e in events if e["operation"] == "dataset.scan"] == ["submitted", "done"]
    finally:
        release.set()
        worker.join(5)
        agent.client.session.close()


def test_scan_requests_validate_roots_role_capability_and_deduplicate(scan_platform):
    store, _, client, root, node, _ = scan_platform
    url = f"/api/control/nodes/{node['node_id']}/scans"
    for path, status in [(None, 400), ("relative", 400), (str(root / ".."), 400), ("/outside", 403)]:
        assert client.post(url, json={"path": path}).status_code == status
    first = client.post(url, json={"path": str(root)}).get_json()["scan"]
    assert client.post(url, json={"path": str(root)}).get_json()["scan"]["request_id"] == first["request_id"]
    assert client.post(url, json={"path": str(root / "child")}).status_code == 409
    with store.sessions() as session:
        assert len(list(session.scalars(select(DatasetScan)))) == 1
    client.post("/api/auth/logout", json={})
    registered = client.post(
        "/api/auth/register", json={"username": "reader", "password": "strong-reader-password"}
    )
    assert registered.status_code == 202
    store.update_user(registered.get_json()["user"]["user_id"], active=True)
    client.post("/api/auth/login", json={"username": "reader", "password": "strong-reader-password"})
    assert client.post(url, json={"path": str(root)}).status_code == 403
    assert client.get("/api/control/nodes").get_json()["nodes"][0]["dataset_scan_allowed"] is False
    assert client.get(f"/api/control/scans/{first['request_id']}").status_code == 403


def test_scan_old_or_offline_agent_is_not_accepted(scan_platform):
    from datetime import timedelta

    from lerobot.data_platform.control_plane import ControlPlaneNode, _utcnow

    store, _, client, root, node, _ = scan_platform
    url = f"/api/control/nodes/{node['node_id']}/scans"
    store.heartbeat(node["node_id"], capabilities={})
    assert client.post(url, json={"path": str(root)}).status_code == 409
    store.heartbeat(node["node_id"], capabilities={"dataset_scan_protocol": 1})
    with store.sessions.begin() as session:
        session.get(ControlPlaneNode, node["node_id"]).last_seen_at = _utcnow() - timedelta(minutes=3)
    assert client.post(url, json={"path": str(root)}).status_code == 409


def test_scoped_operator_and_revoked_queued_request_cannot_discover(scan_platform):
    from lerobot.data_platform.control_plane import ControlPlaneUser

    store, _, client, root, node, headers = scan_platform
    user = store.register_user(
        username="operator", display_name="Operator", password="strong-operator-password"
    )
    store.update_user(user["user_id"], role="operator")
    with store.sessions.begin() as session:
        session.get(ControlPlaneUser, user["user_id"]).active = True
    request = store.scan_manager.create(node["node_id"], user["user_id"], str(root))
    store.set_mutation_locations(
        user["user_id"],
        [],
        all_locations=False,
        actor=next(user for user in store.list_users() if user["role"] == "admin"),
    )
    with pytest.raises(PermissionError, match="unrestricted"):
        store.scan_manager.create(node["node_id"], user["user_id"], str(root))
    assert client.post("/api/agents/heartbeat", headers=headers, json={}).get_json()["scan_request"] is None
    with store.sessions() as session:
        assert session.get(DatasetScan, request.request_id).status == "error"


def test_agent_rechecks_symlinks_and_reports_missing_or_broken_metadata(scan_platform, tmp_path, monkeypatch):
    store, _, _, root, node, _ = scan_platform
    agent = _agent(tmp_path, root, tmp_path)
    completions = []
    agent.client.update_scan = lambda state, request_id, **payload: completions.append(payload)
    agent.client.sync_locations = lambda *args: pytest.fail("Invalid scan must not register locations")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    _make_dataset(root / "broken")
    (root / "broken/meta/info.json").write_text("{bad json")
    for target, message in [
        ("escape", "outside configured roots"),
        ("missing", "does not exist"),
        ("broken", "Expecting"),
    ]:
        request = {"request_id": target, "path": str(root / target)}
        agent._process_scan_request(request)
        assert completions[-1]["status"] == "error"
        assert message in completions[-1]["error"]
    assert store.list_locations() == []


def test_completed_scan_ack_retries_without_rescanning(tmp_path, monkeypatch):
    from lerobot.data_platform import agent as module

    root = tmp_path / "datasets"
    _make_dataset(root / "new")
    agent = _agent(tmp_path, root, tmp_path)
    discovered = []
    original = module.discover_datasets

    def discover(*args, **kwargs):
        discovered.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "discover_datasets", discover)
    agent.client.sync_locations = lambda state, rows: rows
    attempts = []

    def update(state, request_id, **payload):
        attempts.append(payload)
        if len(attempts) == 2:
            raise RuntimeError("connection lost during acknowledgement")

    agent.client.update_scan = update
    request = {"request_id": "scan", "path": str(root)}
    with pytest.raises(RuntimeError, match="connection lost"):
        agent._process_scan_request(request)
    agent._process_scan_request(request)
    assert len(discovered) == 1
    assert attempts[-1] == {"status": "done", "dataset_count": 1}


def test_scan_state_survives_reopening_and_ack_is_idempotent(scan_platform):
    from lerobot.data_platform.control_plane import ControlPlaneStore

    store, _, client, root, node, headers = scan_platform
    request = client.post(f"/api/control/nodes/{node['node_id']}/scans", json={"path": str(root)}).get_json()[
        "scan"
    ]
    reopened = ControlPlaneStore(str(store.engine.url), initialize_schema=False)
    assert reopened.scan_manager.pending(node["node_id"]).request_id == request["request_id"]
    endpoint = f"/api/agents/scans/{request['request_id']}"
    assert client.post(endpoint, json={"status": "running"}).status_code == 401
    assert client.post(endpoint, headers=headers, json={"status": []}).status_code == 400
    assert (
        client.post(endpoint, headers=headers, json={"status": "done", "dataset_count": -1}).status_code
        == 400
    )
    assert (
        client.post(endpoint, headers=headers, json={"status": "done", "dataset_count": 0}).status_code == 200
    )
    assert (
        client.post(endpoint, headers=headers, json={"status": "running"}).get_json()["scan"]["status"]
        == "done"
    )
    assert reopened.scan_manager.pending(node["node_id"]) is None
    assert (
        client.post(f"/api/control/nodes/{node['node_id']}/scans", json={"path": str(root)}).status_code
        == 202
    )


def test_scan_schema_migration_preserves_existing_locations(scan_platform):
    from sqlalchemy import delete

    from lerobot.data_platform.management_storage import SchemaMigration, migrate_management

    store, _, _, root, node, _ = scan_platform
    store.sync_locations(node["node_id"], [{"root": str(root), "dataset_key": "local/existing"}])
    DatasetScan.__table__.drop(store.engine)
    with store.sessions.begin() as session:
        session.execute(delete(SchemaMigration).where(SchemaMigration.version == 5))
    migrate_management(store.engine)
    assert [row["dataset_key"] for row in store.list_locations()] == ["local/existing"]
    with store.sessions() as session:
        assert session.get(SchemaMigration, 5) is not None
        assert list(session.scalars(select(DatasetScan))) == []

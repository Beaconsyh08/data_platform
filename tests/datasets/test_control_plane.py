import json
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask
from sqlalchemy import event
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable

from lerobot.data_platform import control_plane
from lerobot.data_platform.control_plane import Base, ControlPlaneStore
from lerobot.data_platform.routes.control_plane import (
    register_control_plane_auth_routes,
    register_control_plane_routes,
)


def _store(tmp_path: Path) -> ControlPlaneStore:
    return ControlPlaneStore(f"sqlite:///{tmp_path / 'control-plane.db'}")


def _app(
    tmp_path: Path,
    store: ControlPlaneStore,
    *,
    legacy_mutations_enabled: bool = False,
) -> Flask:
    template_dir = Path(__file__).parents[2] / "lerobot" / "data_platform" / "templates"
    app = Flask(__name__, template_folder=template_dir)
    app.config["TESTING"] = True
    register_control_plane_auth_routes(
        app,
        store,
        bootstrap_token="bootstrap-secret",
        allow_registration=True,
    )

    def register_remote_cache(location: dict, cache_output_dir: Path) -> str:
        manifest = json.loads((cache_output_dir / "static" / "viewer_manifest.json").read_text())
        return f"/{location['dataset_key']}/episode_{manifest['episodes'][0]['episode_index']}"

    register_control_plane_routes(
        app,
        store,
        enrollment_token="agent-enrollment-secret",
        remote_cache_root=tmp_path / "remote-cache",
        register_remote_cache=register_remote_cache,
        legacy_mutations_enabled=legacy_mutations_enabled,
    )
    return app


def _bootstrap(client):
    return client.post(
        "/api/auth/bootstrap",
        json={
            "username": "platform-admin",
            "display_name": "Platform Admin",
            "password": "strong-admin-password",
            "bootstrap_token": "bootstrap-secret",
        },
    )


def test_bootstrap_token_field_only_exists_before_first_admin(tmp_path: Path):
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()

    first_login_page = client.get("/login").get_data(as_text=True)
    assert 'id="bootstrap-token"' in first_login_page
    assert _bootstrap(client).status_code == 200
    client.post("/api/auth/logout", json={})

    configured_login_page = client.get("/login").get_data(as_text=True)
    assert 'id="bootstrap-token"' not in configured_login_page
    assert "Request a viewer account" in configured_login_page


def test_control_plane_auth_node_location_and_viewer_job_round_trip(tmp_path: Path, monkeypatch):
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()

    assert client.get("/api/control/nodes").status_code == 401
    bootstrap = _bootstrap(client)
    assert bootstrap.status_code == 200
    assert bootstrap.get_json()["user"]["role"] == "admin"
    control_plane_page = client.get("/control-plane").get_data(as_text=True)
    assert "Choose a server" in control_plane_page
    assert 'x-model="selectedNodeId"' in control_plane_page
    assert "filteredLocations()" in control_plane_page
    assert "paginatedLocations()" in control_plane_page
    assert "nodeLocationCount(node.node_id)" in control_plane_page
    assert "locationPageSize: 20" in control_plane_page
    assert "Viewer missing" in control_plane_page
    assert "Open in Dataset console" in control_plane_page
    assert ">registered</span>" not in control_plane_page
    assert "location.state || 'available'" not in control_plane_page
    assert "Start sibling preprocess" not in control_plane_page
    assert "startPreprocess(location)" not in control_plane_page

    enrolled = client.post(
        "/api/agents/enroll",
        json={
            "name": "server-b",
            "hostname": "server-b.internal",
            "allowed_roots": ["/data/datasets"],
            "writable_roots": ["/data"],
            "capabilities": {"cpu_count": 32},
            "enrollment_token": "agent-enrollment-secret",
        },
    )
    assert enrolled.status_code == 200
    node = enrolled.get_json()["node"]
    token = enrolled.get_json()["node_token"]
    agent_headers = {"Authorization": f"Bearer {token}"}

    heartbeat = client.post(
        "/api/agents/heartbeat",
        headers=agent_headers,
        json={"capabilities": {"cpu_count": 32, "gpu_count": 2}},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.get_json()["node"]["capabilities"]["gpu_count"] == 2

    synced = client.post(
        "/api/agents/locations/sync",
        headers=agent_headers,
        json={
            "locations": [
                {
                    "dataset_key": "node-server-b/pick-cups",
                    "root": "/data/datasets/pick-cups",
                    "output_dir": "/data/local_vis_pick-cups",
                    "metadata": {"total_episodes": 12},
                }
            ]
        },
    )
    assert synced.status_code == 200
    location = synced.get_json()["locations"][0]
    listed_location = client.get("/api/control/locations").get_json()["locations"][0]
    assert listed_location["node_name"] == "server-b"
    assert listed_location["node_hostname"] == "server-b.internal"

    viewer_options = {
        "data_version": "DVT2",
        "episodes": [0, 3],
        "downsample": 2,
        "overwrite": True,
        "overwrite_csv": True,
        "prepare_csv": True,
        "prepare_videos": False,
        "prepare_workers": 6,
    }
    created = client.post(
        f"/api/control/locations/{location['location_id']}/viewer-jobs",
        json=viewer_options,
    )
    assert created.status_code == 202
    job_id = created.get_json()["job"]["job_id"]
    duplicate = client.post(f"/api/control/locations/{location['location_id']}/viewer-jobs", json={})
    assert duplicate.status_code == 202
    assert duplicate.get_json()["job"]["job_id"] == job_id
    claimed = client.post(
        "/api/agents/jobs/claim",
        headers=agent_headers,
        json={"lease_seconds": 90},
    ).get_json()["job"]
    assert claimed["job_id"] == job_id
    assert claimed["location"]["root"] == "/data/datasets/pick-cups"
    assert claimed["options"] == viewer_options
    future = datetime.fromisoformat(node["last_seen_at"]).replace(tzinfo=None) + timedelta(minutes=3)
    monkeypatch.setattr(control_plane, "_utcnow", lambda: future)
    assert store.list_nodes()[0]["status"] == "offline"
    lease_heartbeat = client.post(
        f"/api/agents/jobs/{job_id}/heartbeat",
        headers=agent_headers,
        json={"lease_seconds": 90},
    )
    assert lease_heartbeat.status_code == 200
    assert lease_heartbeat.get_json()["renewed"] is True
    refreshed_node = store.list_nodes()[0]
    assert refreshed_node["status"] == "online"
    assert datetime.fromisoformat(refreshed_node["last_seen_at"]).replace(tzinfo=None) == future
    progress = client.post(
        f"/api/agents/jobs/{job_id}/events",
        headers=agent_headers,
        json={"message": "Prepared episode 3/12", "payload": {"current": 3, "total": 12}},
    )
    assert progress.status_code == 200
    listed_job = next(
        job for job in client.get("/api/control/jobs").get_json()["jobs"] if job["job_id"] == job_id
    )
    assert "events" not in listed_job
    detailed_job = client.get(f"/api/control/jobs/{job_id}").get_json()["job"]
    assert detailed_job["events"][-1]["message"] == "Prepared episode 3/12"
    assert detailed_job["events"][-1]["payload"] == {"current": 3, "total": 12}

    manifest = {"episodes": [{"episode_index": 3}], "repo_id": location["dataset_key"]}
    uploaded = client.put(
        f"/api/agents/jobs/{job_id}/artifacts/viewer_manifest.json",
        headers=agent_headers,
        data=json.dumps(manifest),
    )
    assert uploaded.status_code == 200
    completed = client.post(
        f"/api/agents/jobs/{job_id}/complete",
        headers=agent_headers,
        json={"status": "done", "result": {"uploaded_files": 1}},
    )
    assert completed.status_code == 200
    assert completed.get_json()["job"]["result"]["viewer_url"].endswith("/episode_3")
    refreshed = store.get_location(location["location_id"])
    assert refreshed["metadata"]["viewer_ready"] is True
    assert node["node_id"] == refreshed["node_id"]
    resynced = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": location["dataset_key"],
                "root": location["root"],
                "metadata": {"total_episodes": 13},
            }
        ],
    )[0]
    assert resynced["metadata"]["total_episodes"] == 13
    assert resynced["metadata"]["viewer_ready"] is True
    assert resynced["metadata"]["viewer_url"].endswith("/episode_3")
    repeated = store.complete_job(job_id, node_id=node["node_id"], status="done")
    assert repeated["status"] == "done"
    try:
        store.complete_job(job_id, node_id=node["node_id"], status="error", error="late response")
    except ValueError as exc:
        assert "already terminal" in str(exc)
    else:
        raise AssertionError("a completed job was changed to error")
    stale = store.mark_viewer_stale(location["location_id"])
    assert stale["metadata"]["viewer_ready"] is False
    assert "viewer_url" not in stale["metadata"]


def test_locations_with_the_same_dataset_key_include_their_node_names(tmp_path: Path):
    store = _store(tmp_path)
    for node_name in ("server-b", "server-c"):
        _, node = store.enroll_node(
            name=node_name,
            hostname=f"{node_name}.internal",
            allowed_roots=[f"/datasets/{node_name}"],
            writable_roots=["/datasets"],
            capabilities={},
            enrollment_token="agent-enrollment-secret",
            expected_token="agent-enrollment-secret",
        )
        store.sync_locations(
            node["node_id"],
            [
                {
                    "dataset_key": "shared/pick-cups",
                    "root": f"/datasets/{node_name}/pick-cups",
                    "output_dir": f"/datasets/{node_name}/local_vis_pick-cups",
                }
            ],
        )

    locations = store.list_locations()
    assert [location["node_name"] for location in locations] == ["server-b", "server-c"]
    assert {location["dataset_key"] for location in locations} == {"shared/pick-cups"}
    assert {location["node_hostname"] for location in locations} == {
        "server-b.internal",
        "server-c.internal",
    }


def test_location_listing_keeps_large_metadata_out_of_database_sort(tmp_path: Path):
    store = _store(tmp_path)
    assert store.list_locations() == []
    metadata = {"description": "x" * (512 * 1024), "tasks": [{"task": "Open the washer door"}]}
    expected = []
    for node_name in ("server-c", "server-a"):
        _, node = store.enroll_node(
            name=node_name,
            hostname=f"{node_name}.internal",
            allowed_roots=["/datasets"],
            writable_roots=["/datasets"],
            capabilities={},
            enrollment_token="agent-enrollment-secret",
            expected_token="agent-enrollment-secret",
        )
        for dataset_key, suffix in (("shared/z", "z"), ("shared/a", "b"), ("shared/a", "a")):
            root = f"/datasets/{node_name}/{suffix}"
            store.sync_locations(
                node["node_id"],
                [{"dataset_key": dataset_key, "root": root, "metadata": metadata}],
            )
            expected.append((node_name, dataset_key, root))

    statements = []

    def reject_large_json_sort(connection, cursor, statement, parameters, context, executemany):
        # SQLite does not have MySQL's filesort memory limit. Reproduce the
        # production driver error when a sorted query carries the large JSON.
        sql = statement.lower()
        statements.append(sql)
        if "metadata_json" in sql and "order by" in sql:
            raise OperationalError(statement, parameters, Exception(1038, "Out of sort memory"))

    event.listen(store.engine, "before_cursor_execute", reject_large_json_sort)
    try:
        locations = store.list_locations()
    finally:
        event.remove(store.engine, "before_cursor_execute", reject_large_json_sort)

    assert [(row["node_name"], row["dataset_key"], row["root"]) for row in locations] == sorted(expected)
    assert all(row["metadata"] == metadata for row in locations)
    assert all(row["node_hostname"] == f"{row['node_name']}.internal" for row in locations)
    assert len({row["location_id"] for row in locations}) == len(expected)
    assert len(statements) == 2  # Metadata is fetched in bulk, without one query per location.


def test_remote_standardize_registers_output_and_promotes_its_viewer_cache(tmp_path: Path):
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    assert _bootstrap(client).status_code == 200
    token, node = store.enroll_node(
        name="server-b",
        hostname="server-b.internal",
        allowed_roots=["/data"],
        writable_roots=["/data"],
        capabilities={},
        enrollment_token="agent-enrollment-secret",
        expected_token="agent-enrollment-secret",
    )
    source = store.sync_locations(
        node["node_id"],
        [{"dataset_key": "node-server-b/source", "root": "/data/source", "output_dir": "/data/vis"}],
    )[0]
    created = client.post(
        f"/api/control/locations/{source['location_id']}/preprocess-jobs",
        json={
            "op": "standardize",
            "options": {"out_root": "/data/source-standard", "data_version": "DVT2"},
        },
    )
    assert created.status_code == 202
    job_id = created.get_json()["job"]["job_id"]
    headers = {"Authorization": f"Bearer {token}"}
    claimed = client.post(
        "/api/agents/jobs/claim",
        headers=headers,
        json={"lease_seconds": 90},
    )
    assert claimed.get_json()["job"]["job_id"] == job_id

    wrong_scope = client.put(
        f"/api/agents/jobs/{job_id}/artifacts/viewer_manifest.json",
        headers=headers,
        data="{}",
    )
    assert wrong_scope.status_code == 403
    manifest = {"episodes": [{"episode_index": 0}], "repo_id": "node-server-b/source-standard"}
    uploaded = client.put(
        f"/api/agents/jobs/{job_id}/derived-artifacts/viewer_manifest.json",
        headers=headers,
        data=json.dumps(manifest),
    )
    assert uploaded.status_code == 200
    completed = client.post(
        f"/api/agents/jobs/{job_id}/complete",
        headers=headers,
        json={
            "status": "done",
            "result": {
                "preprocess": {
                    "op": "standardize",
                    "out_root": "/data/source-standard",
                    "summary": {"data_version": "DVT2"},
                },
                "dataset_location": {
                    "dataset_key": "node-server-b/source-standard",
                    "root": "/data/source-standard",
                    "output_dir": "/data/local_vis_source-standard",
                    "metadata": {
                        "total_episodes": 7,
                        "data_version": "DVT2",
                        "stage": "standard",
                    },
                },
                "viewer_cache": {"uploaded_files": 1},
            },
        },
    )
    assert completed.status_code == 200
    result = completed.get_json()["job"]["result"]
    output_location_id = result["output_location_id"]
    assert result["synced_location"]["location_id"] == output_location_id
    assert result["synced_location"]["metadata"]["viewer_ready"] is True
    assert result["synced_location"]["metadata"]["stage"] == "standard"
    assert result["viewer_url"].endswith("/episode_0")
    assert (tmp_path / "remote-cache" / output_location_id / "static" / "viewer_manifest.json").is_file()
    assert not (tmp_path / "remote-cache" / ".jobs" / job_id).exists()


def test_registration_waits_for_admin_approval_and_starts_read_only(tmp_path: Path):
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    assert _bootstrap(client).status_code == 200
    client.post("/api/auth/logout", json={})

    registered = client.post(
        "/api/auth/register",
        json={
            "username": "review-user",
            "display_name": "Review User",
            "password": "strong-viewer-password",
        },
    )
    assert registered.status_code == 202
    assert registered.get_json()["status"] == "pending_approval"
    assert registered.get_json()["user"]["role"] == "viewer"
    assert registered.get_json()["user"]["active"] is False
    assert client.get("/api/control/locations").status_code == 401
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "review-user", "password": "strong-viewer-password"},
        ).status_code
        == 403
    )

    assert (
        client.post(
            "/api/auth/login",
            json={"username": "platform-admin", "password": "strong-admin-password"},
        ).status_code
        == 200
    )
    viewer = next(user for user in store.list_users() if user["username"] == "review-user")
    approved = client.patch(f"/api/auth/users/{viewer['user_id']}", json={"active": True})
    assert approved.status_code == 200
    assert approved.get_json()["user"]["active"] is True

    client.post("/api/auth/logout", json={})
    viewer_login = client.post(
        "/api/auth/login",
        json={"username": "review-user", "password": "strong-viewer-password"},
    )
    assert viewer_login.status_code == 200
    assert client.post("/api/datasets/register", json={"root": "/tmp/nope"}).status_code == 403
    assert client.get("/api/control/locations").status_code == 200

    client.post("/api/auth/logout", json={})
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "platform-admin", "password": "strong-admin-password"},
        ).status_code
        == 200
    )
    updated = client.patch(f"/api/auth/users/{viewer['user_id']}", json={"role": "operator"})
    assert updated.status_code == 200
    assert updated.get_json()["user"]["role"] == "operator"

    created = client.post(
        "/api/auth/users",
        json={
            "username": "managed-user",
            "display_name": "Managed User",
            "password": "strong-managed-password",
            "role": "viewer",
        },
    )
    assert created.status_code == 201
    admin = next(user for user in store.list_users() if user["username"] == "platform-admin")
    rejected = client.patch(f"/api/auth/users/{admin['user_id']}", json={"active": False})
    assert rejected.status_code == 400
    assert "last active administrator" in rejected.get_json()["error"]


def test_central_admin_role_replaces_local_admin_mode(tmp_path: Path, monkeypatch):
    from lerobot.data_platform import viewer

    monkeypatch.setenv("DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-secret")
    monkeypatch.setenv("DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN", "agent-enrollment-secret")
    static_dir = tmp_path / "vis" / "_console" / "static"
    static_dir.mkdir(parents=True)
    app = viewer.run_server(
        dataset=None,
        episodes=None,
        max_frames=None,
        prepare_videos=False,
        downsample=None,
        precompute_csv=False,
        precomputed_only=True,
        host="127.0.0.1",
        port=0,
        static_folder=static_dir,
        template_folder=Path(viewer.__file__).parent / "templates",
        datasets_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'viewer-control-plane.db'}",
        allow_registration=True,
        remote_cache_root=tmp_path / "remote-cache",
        start_server=False,
    )
    client = app.test_client()

    assert _bootstrap(client).status_code == 200
    assert client.get("/api/admin/status").get_json() == {
        "authenticated": True,
        "central_account": True,
        "configured": True,
    }
    assert client.post("/api/admin/setup", json={"password": "unused-password"}).status_code == 409
    homepage = client.get("/").get_data(as_text=True)
    assert 'controlPlaneUser: {"active": true' in homepage
    assert "Administrator account" in homepage

    created = client.post(
        "/api/auth/users",
        json={
            "username": "platform-operator",
            "display_name": "Platform Operator",
            "password": "strong-operator-password",
            "role": "operator",
        },
    )
    assert created.status_code == 201
    client.post("/api/auth/logout", json={})
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "platform-operator", "password": "strong-operator-password"},
        ).status_code
        == 200
    )
    assert client.get("/api/admin/status").get_json()["authenticated"] is False
    unregister_denied = client.delete("/api/datasets/local/missing")
    assert unregister_denied.status_code == 403
    assert "administrator access is required" in unregister_denied.get_json()["error"]
    denied = client.post("/api/preprocess/delete_episodes/start", json={})
    assert denied.status_code == 403
    assert "administrator account is required" in denied.get_json()["error"].lower()


def test_remote_preprocess_rejects_paths_and_mysql_schema_compiles(tmp_path: Path):
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    assert _bootstrap(client).status_code == 200
    token, node = store.enroll_node(
        name="server-c",
        hostname="server-c",
        allowed_roots=["/datasets"],
        writable_roots=["/datasets"],
        capabilities={},
        enrollment_token="agent-enrollment-secret",
        expected_token="agent-enrollment-secret",
    )
    del token
    location = store.sync_locations(
        node["node_id"],
        [{"dataset_key": "server-c/data", "root": "/datasets/data", "output_dir": "/cache"}],
    )[0]
    rejected = client.post(
        f"/api/control/locations/{location['location_id']}/preprocess-jobs",
        json={"op": "standardize", "options": {"out_root": "/tmp/escape"}},
    )
    assert rejected.status_code == 400
    source_child = client.post(
        f"/api/control/locations/{location['location_id']}/preprocess-jobs",
        json={"op": "standardize", "options": {"out_root": "/datasets/data/output"}},
    )
    assert source_child.status_code == 400
    accepted = client.post(
        f"/api/control/locations/{location['location_id']}/preprocess-jobs",
        json={
            "op": "standardize",
            "options": {
                "delete_episodes": "1,3-4",
                "out_root": "/datasets/data-standardized",
                "overwrite_output": True,
            },
        },
    )
    assert accepted.status_code == 202
    assert accepted.get_json()["job"]["options"] == {
        "delete_episodes": "1,3-4",
        "out_root": "/datasets/data-standardized",
        "overwrite_output": True,
    }

    statements = [
        str(CreateTable(table).compile(dialect=mysql.dialect())) for table in Base.metadata.tables.values()
    ]
    assert statements
    assert all("CREATE TABLE" in statement for statement in statements)


def test_remote_source_mutation_requires_server_switch_admin_and_typed_confirmation(tmp_path: Path):
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    assert _bootstrap(client).status_code == 200
    _, node = store.enroll_node(
        name="server-b",
        hostname="server-b",
        allowed_roots=["/datasets"],
        writable_roots=["/datasets"],
        capabilities={"source_mutations_enabled": True},
        enrollment_token="agent-enrollment-secret",
        expected_token="agent-enrollment-secret",
    )
    location = store.sync_locations(
        node["node_id"],
        [{"dataset_key": "server-b/data", "root": "/datasets/data", "output_dir": "/datasets/vis"}],
    )[0]
    endpoint = f"/api/control/locations/{location['location_id']}/mutation-jobs"
    payload = {
        "op": "delete_episodes",
        "options": {"episodes": [1], "reason": "corrupt episode"},
        "confirmation": "MUTATE server-b/data",
    }
    assert client.post(endpoint, json=payload).status_code == 403

    enabled_store = ControlPlaneStore(f"sqlite:///{tmp_path / 'enabled-control-plane.db'}")
    enabled_client = _app(
        tmp_path / "enabled",
        enabled_store,
        legacy_mutations_enabled=True,
    ).test_client()
    assert _bootstrap(enabled_client).status_code == 200
    enabled_token, enabled_node = enabled_store.enroll_node(
        name="server-b",
        hostname="server-b",
        allowed_roots=["/datasets"],
        writable_roots=["/datasets"],
        capabilities={"source_mutations_enabled": True},
        enrollment_token="agent-enrollment-secret",
        expected_token="agent-enrollment-secret",
    )
    enabled_location = enabled_store.sync_locations(
        enabled_node["node_id"],
        [{"dataset_key": "server-b/data", "root": "/datasets/data", "output_dir": "/datasets/vis"}],
    )[0]
    enabled_endpoint = f"/api/control/locations/{enabled_location['location_id']}/mutation-jobs"
    wrong = enabled_client.post(
        enabled_endpoint,
        json={**payload, "confirmation": "MUTATE something-else"},
    )
    assert wrong.status_code == 409
    accepted = enabled_client.post(enabled_endpoint, json=payload)
    assert accepted.status_code == 202
    job = accepted.get_json()["job"]
    assert job["operation"] == "mutation.delete_episodes"
    assert job["options"]["requested_by"]["username"] == "platform-admin"
    agent_headers = {"Authorization": f"Bearer {enabled_token}"}
    claimed = enabled_client.post(
        "/api/agents/jobs/claim",
        headers=agent_headers,
        json={"lease_seconds": 90},
    ).get_json()["job"]
    assert claimed["job_id"] == job["job_id"]
    cache_root = tmp_path / "enabled" / "remote-cache" / enabled_location["location_id"]
    (cache_root / "static").mkdir(parents=True)
    (cache_root / "static" / "stale.txt").write_text("stale")
    enabled_store.mark_viewer_ready(
        enabled_location["location_id"],
        viewer_url="/old-viewer",
        cache_root=str(cache_root),
    )
    completed = enabled_client.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        headers=agent_headers,
        json={
            "status": "done",
            "result": {
                "source_changed": True,
                "dataset_location": {
                    "dataset_key": "server-b/data",
                    "root": "/datasets/data",
                    "output_dir": "/datasets/vis",
                    "metadata": {"total_episodes": 9},
                },
            },
        },
    )
    assert completed.status_code == 200
    assert not cache_root.exists()
    refreshed = enabled_store.get_location(enabled_location["location_id"])
    assert refreshed["metadata"]["viewer_ready"] is False
    assert "viewer_url" not in refreshed["metadata"]


def test_viewer_enables_central_login_without_changing_local_mode(tmp_path: Path, monkeypatch):
    from lerobot.data_platform import viewer

    monkeypatch.setenv("DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-secret")
    monkeypatch.setenv("DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN", "agent-enrollment-secret")
    static_dir = tmp_path / "vis" / "_console" / "static"
    static_dir.mkdir(parents=True)
    app = viewer.run_server(
        dataset=None,
        episodes=None,
        max_frames=None,
        prepare_videos=False,
        downsample=None,
        precompute_csv=False,
        precomputed_only=True,
        host="127.0.0.1",
        port=0,
        static_folder=static_dir,
        template_folder=Path(viewer.__file__).parent / "templates",
        datasets_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'viewer-control-plane.db'}",
        remote_cache_root=tmp_path / "remote-cache",
        start_server=False,
    )
    client = app.test_client()

    assert client.get("/").status_code == 302
    assert client.get("/").headers["Location"].startswith("/login")
    assert _bootstrap(client).status_code == 200
    homepage = client.get("/")
    assert homepage.status_code == 200
    homepage_html = homepage.get_data(as_text=True)
    assert 'href="/control-plane"' in homepage_html
    assert 'x-model="datasetServerFilter"' in homepage_html
    assert 'x-model="scanPath"' in homepage_html
    assert '@click="scanSelectedServer()"' in homepage_html
    assert '<option value="all">All servers</option>' not in homepage_html
    assert "Server A (local)" in homepage_html
    assert "this.requestJson('/api/control/locations')" in homepage_html
    assert "async prepareRemoteViewer(location, options = {})" in homepage_html
    assert "useRemoteDataset(location)" in homepage_html
    assert "openDatasetTab(location, 'standardize')" in homepage_html
    assert "openDatasetTab(dataset, 'standardize')" in homepage_html
    assert "openDatasetTab(location, 'cache')" in homepage_html
    assert "openDatasetTab(dataset, 'cache')" in homepage_html
    assert "Prepare cache on Agent" in homepage_html
    assert "await this.prepareRemoteViewer(this.selectedRemoteLocation, remoteOptions)" in homepage_html
    assert 'x-text="remoteDatasetName(location)"' in homepage_html
    assert 'x-text="location.dataset_key"' not in homepage_html
    assert "remoteLocationsForSelectedServer()" in homepage_html
    assert "startRemoteSchemaFix()" in homepage_html
    assert "/mutation-jobs" in homepage_html
    assert "mergeRemoteJobsIntoRuns()" in homepage_html
    assert "mergeRemoteJob(data.job)" in homepage_html
    assert "remoteViewerPending(location)" in homepage_html
    assert "`/api/control/jobs/${this.remoteViewerJob.job_id}`" in homepage_html
    assert '@click="selectJob(job)"' in homepage_html
    assert "async selectJob(job, remember = true)" in homepage_html
    assert "`/api/control/jobs/${jobId}`" in homepage_html
    assert "dataPlatform.selectedJob" in homepage_html
    assert "scheduleSelectedRemoteJobRefresh(jobId)" in homepage_html


def test_internal_remote_viewer_cache_is_not_listed_as_server_a_dataset(tmp_path: Path, monkeypatch):
    from lerobot.data_platform import viewer

    monkeypatch.setenv("DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-secret")
    monkeypatch.setenv("DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN", "agent-enrollment-secret")
    static_dir = tmp_path / "vis" / "_console" / "static"
    static_dir.mkdir(parents=True)
    remote_output = tmp_path / "remote-cache" / "location-1"
    (remote_output / "static").mkdir(parents=True)
    (remote_output / "static" / "viewer_manifest.json").write_text(
        json.dumps(
            {
                "repo_id": "node-server-b/pick-cups",
                "episodes": [{"episode_index": 3}],
                "features": {},
            }
        )
    )
    (static_dir / "datasets_registry.json").write_text(
        json.dumps(
            {
                "datasets": [
                    {
                        "repo_id": "node-server-b/pick-cups",
                        "root": str(remote_output / "remote_source"),
                        "output_dir": str(remote_output),
                    }
                ]
            }
        )
    )
    app = viewer.run_server(
        dataset=None,
        episodes=None,
        max_frames=None,
        prepare_videos=False,
        downsample=None,
        precompute_csv=False,
        precomputed_only=True,
        host="127.0.0.1",
        port=0,
        static_folder=static_dir,
        template_folder=Path(viewer.__file__).parent / "templates",
        datasets_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'viewer-control-plane.db'}",
        remote_cache_root=tmp_path / "remote-cache",
        start_server=False,
    )
    app.config["TESTING"] = True
    client = app.test_client()

    assert _bootstrap(client).status_code == 200
    assert client.get("/api/datasets").get_json() == {"datasets": []}
    internal_detail = client.get("/api/datasets/node-server-b/pick-cups")
    assert internal_detail.status_code == 200
    assert internal_detail.get_json()["dataset"]["root"].endswith("/remote_source")
    assert client.get("/node-server-b/pick-cups/episode_3").status_code == 200


def test_remote_viewer_route_is_restored_after_server_a_dataset_root_changes(tmp_path: Path):
    from lerobot.data_platform import viewer

    database_url = f"sqlite:///{tmp_path / 'control-plane.db'}"
    store = ControlPlaneStore(database_url)
    store.bootstrap_admin(
        username="platform-admin",
        display_name="Platform Admin",
        password="strong-admin-password",
        bootstrap_token="bootstrap-secret",
        expected_token="bootstrap-secret",
    )
    _, node = store.enroll_node(
        name="server-b",
        hostname="server-b.internal",
        allowed_roots=["/data/datasets"],
        writable_roots=["/data"],
        capabilities={},
        enrollment_token="agent-enrollment-secret",
        expected_token="agent-enrollment-secret",
    )
    location = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "node-server-b/pick-cups",
                "root": "/data/datasets/pick-cups",
                "output_dir": "/data/local_vis_pick-cups",
            }
        ],
    )[0]
    remote_output = tmp_path / "remote-cache" / location["location_id"]
    (remote_output / "static").mkdir(parents=True)
    (remote_output / "static" / "viewer_manifest.json").write_text(
        json.dumps(
            {
                "repo_id": location["dataset_key"],
                "episodes": [{"episode_index": 3, "length": 1, "tasks": ["pick cups"]}],
                "features": {},
            }
        )
    )
    store.mark_viewer_ready(
        location["location_id"],
        viewer_url="/stale-route/episode_3",
        cache_root=str(remote_output),
    )

    datasets_root = tmp_path / "new-server-a-root"
    datasets_root.mkdir()
    static_dir = tmp_path / "central-console" / "static"
    static_dir.mkdir(parents=True)
    app = viewer.run_server(
        dataset=None,
        episodes=None,
        max_frames=None,
        prepare_videos=False,
        downsample=None,
        precompute_csv=False,
        precomputed_only=True,
        host="127.0.0.1",
        port=0,
        static_folder=static_dir,
        template_folder=Path(viewer.__file__).parent / "templates",
        datasets_root=datasets_root,
        database_url=database_url,
        remote_cache_root=tmp_path / "remote-cache",
        start_server=False,
    )
    app.config["TESTING"] = True
    client = app.test_client()
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "platform-admin", "password": "strong-admin-password"},
        ).status_code
        == 200
    )

    restored = client.get("/api/control/locations").get_json()["locations"][0]
    assert restored["metadata"]["viewer_url"] == "/node-server-b/pick-cups/episode_3"
    assert client.get(restored["metadata"]["viewer_url"]).status_code == 200
    assert (static_dir / "datasets_registry.json").is_file()
    assert not (datasets_root / "vis" / "_console" / "static" / "datasets_registry.json").exists()


def test_wsgi_factory_trusts_only_explicit_proxy_configuration(tmp_path: Path, monkeypatch):
    from lerobot.data_platform.wsgi import create_app

    monkeypatch.setenv("DATA_PLATFORM_ROOT", str(tmp_path / "datasets"))
    monkeypatch.setenv("DATA_PLATFORM_OUTPUT_DIR", str(tmp_path / "console"))
    monkeypatch.setenv("DATA_PLATFORM_REMOTE_CACHE_ROOT", str(tmp_path / "remote-cache"))
    monkeypatch.setenv("DATA_PLATFORM_DATABASE_URL", f"sqlite:///{tmp_path / 'control-plane.db'}")
    monkeypatch.setenv("DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-secret")
    monkeypatch.setenv("DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN", "agent-enrollment-secret")
    monkeypatch.setenv("DATA_PLATFORM_TRUST_PROXY", "1")
    (tmp_path / "datasets").mkdir()

    client = create_app().test_client()
    response = client.post(
        "/api/auth/bootstrap",
        headers={"X-Forwarded-Proto": "https"},
        json={
            "username": "platform-admin",
            "display_name": "Platform Admin",
            "password": "strong-admin-password",
            "bootstrap_token": "bootstrap-secret",
        },
    )

    assert response.status_code == 200
    assert "Secure" in response.headers["Set-Cookie"]

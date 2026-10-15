"""List refreshes must use bounded reads without weakening current grants or job controls."""

import pytest
from sqlalchemy import event

from lerobot.data_platform.control_plane import DatasetLocation, RemoteJob, RemoteJobEvent, _utcnow
from lerobot.data_platform.management_storage import JobControl
from tests.datasets.test_control_plane import _app
from tests.datasets.test_job_management import setup  # noqa: F401 -- shared fixture


@pytest.mark.parametrize("username", ["admin-user", "owner-user"])
def test_summary_responses_preserve_access_and_full_details(setup, tmp_path, username):  # noqa: F811
    store, admin, owner, _, location, create = setup
    job, foreign = create(), create(admin)
    evidence = "per-episode-artifact-" * 50_000
    metadata = {
        "total_episodes": 12,
        "features": {"action": {"shape": [2]}},
        "viewer_url": "/remote/source/viewer",
        "operation_capabilities": {"viewer": {"supported": True}},
        "episodes": [{"episode_index": 0, "evidence": evidence}],
        "stats": {"action": evidence},
        "tasks": [{"task": evidence}],
    }
    output = {**location, "metadata": metadata}
    with store.sessions.begin() as session:
        session.get(DatasetLocation, location["location_id"]).details = metadata
        row = session.get(RemoteJob, job["job_id"])
        row.options = {
            "source_root": "/data/source",
            "snapshot": {"episodes": evidence},
            "task_config": {"catalog": evidence},
        }
        row.result = {
            "progress": {"progress": 30, "current": 3, "total": 10},
            "viewer_url": "/remote/output/viewer",
            "dataset_location": output,
            "synced_location": output,
            "mcap_import": {
                "files": [{"report": evidence}],
                "rejected": [{"report": evidence}],
                "total_episodes": 12,
            },
            "preprocess": {
                "out_root": "/data/output",
                "summary": {"total_frames": 120},
                "episode_lineage": [evidence],
            },
            "precompute": {"episodes": evidence},
        }
        session.add(
            RemoteJobEvent(
                job_id=row.job_id,
                level="info",
                message="Uploading",
                payload={"current": 3},
                created_at=_utcnow(),
            )
        )
    client = _app(tmp_path, store).test_client()
    assert (
        client.post(
            "/api/auth/login", json={"username": username, "password": "long-test-password"}
        ).status_code
        == 200
    )
    locations = client.get("/api/control/locations?summary=1")
    assert locations.status_code == 200
    assert locations.content_length < 20_000
    small_metadata = locations.json["locations"][0]["metadata"]
    assert small_metadata == {
        key: value for key, value in metadata.items() if key not in {"episodes", "stats", "tasks"}
    }
    assert client.get("/api/control/locations").json["locations"][0]["metadata"] == metadata

    response = client.get("/api/control/jobs?limit=200&summary=1")
    assert response.status_code == 200
    assert response.content_length < 20_000
    jobs = {item["job_id"]: item for item in response.json["jobs"]}
    assert (foreign["job_id"] in jobs) == (username == admin["username"])
    summary = jobs[job["job_id"]]
    assert summary["options"] == {"source_root": "/data/source"}
    assert summary["result"]["dataset_location"]["metadata"] == small_metadata
    assert summary["result"]["synced_location"]["metadata"] == small_metadata
    assert summary["result"]["mcap_import"] == {"total_episodes": 12}
    assert summary["result"]["preprocess"] == {"out_root": "/data/output", "summary": {"total_frames": 120}}
    assert "precompute" not in summary["result"]
    full = client.get(f"/api/control/jobs/{job['job_id']}").json["job"]
    detail = client.get(f"/api/control/jobs/{job['job_id']}?summary=1")
    assert detail.status_code == 200
    assert detail.content_length < 20_000
    assert detail.json["job"]["events"] == full["events"]
    assert detail.json["job"]["result"] == summary["result"]
    for key in (
        "progress",
        "revision",
        "available_actions",
        "queue_ahead",
        "node_slots",
        "requested_by_username",
    ):
        assert summary[key] == full[key]
    assert full["options"]["snapshot"]["episodes"] == evidence
    assert full["result"]["mcap_import"]["files"][0]["report"] == evidence
    assert store.get_job(job["job_id"])["result"] == full["result"]
    if username == owner["username"]:
        assert client.get(f"/api/control/jobs/{foreign['job_id']}?summary=1").status_code == 404
        store.set_mutation_locations(owner["user_id"], [], actor=admin)
        assert client.get("/api/control/locations?summary=1").json["locations"] == []
        assert client.get("/api/control/jobs?summary=1").json["jobs"] == []
        assert client.get(f"/api/control/jobs/{job['job_id']}?summary=1").status_code == 403


@pytest.mark.parametrize("username", ["admin-user", "owner-user"])
def test_job_list_query_count_does_not_grow_with_history(setup, tmp_path, username):  # noqa: F811
    store, admin, owner, node, location, create = setup
    store.job_manager.queue_limit = 100
    jobs = [create() for _ in range(40)]
    with store.sessions.begin() as session:
        for job in jobs[:20]:
            row = session.get(RemoteJob, job["job_id"])
            row.status = "done"
            control = session.get(JobControl, row.job_id)
            control.phase, control.stop_confirmed = "done", True
        session.get(JobControl, jobs[-1]["job_id"]).priority = 2
    active = store.claim_job(node["node_id"], worker_instance_id="test-worker")
    client = _app(tmp_path, store).test_client()
    client.post("/api/auth/login", json={"username": username, "password": "long-test-password"})
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", record)
    try:
        response = client.get("/api/control/jobs")
    finally:
        event.remove(store.engine, "before_cursor_execute", record)
    assert response.status_code == 200
    result = response.json["jobs"]
    assert len(result) == 40
    assert len(statements) <= 20
    by_id = {job["job_id"]: job for job in result}
    assert active["job_id"] == jobs[-1]["job_id"]
    assert by_id[active["job_id"]]["node_slots"]["used"] == 1
    assert by_id[active["job_id"]]["attempt_id"] == active["execution"]["attempt_id"]
    waiting = [job for job in result if job["status"] == "queued"]
    assert sorted(job["queue_ahead"] for job in waiting) == list(range(1, 20))
    assert all(job["queue_wait_reason"] == "data_path_conflict" for job in waiting)
    assert all(job["requested_by_username"] == owner["username"] for job in result)
    assert all("cancel" in job["available_actions"] for job in waiting)
    assert all(
        ("extra-quota" in job["available_actions"]) == (username == admin["username"]) for job in waiting
    )
    assert all(not job["available_actions"] for job in result if job["status"] == "done")


def test_dataset_list_batches_scope_checks_and_rechecks_current_roots(setup):  # noqa: F811
    store, admin, owner, node, location, _ = setup
    extra = store.sync_locations(
        node["node_id"],
        [{"root": f"/data/input-{index}", "dataset_key": f"input-{index}"} for index in range(40)],
    )
    selected = [location["location_id"], *(row["location_id"] for row in extra[:20])]
    for allowed in (None, selected):
        if allowed is not None:
            store.set_mutation_locations(owner["user_id"], allowed, actor=admin)
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany, statements=statements):
            statements.append(statement)

        event.listen(store.engine, "before_cursor_execute", record)
        try:
            result = store.accessible_locations(owner["user_id"])
        finally:
            event.remove(store.engine, "before_cursor_execute", record)
        assert len(statements) <= 6
        assert {row["location_id"] for row in result} == (
            set(selected)
            if allowed is not None
            else {location["location_id"], *(row["location_id"] for row in extra)}
        )
    with store.sessions.begin() as session:
        session.get(DatasetLocation, location["location_id"]).root = "/data/replaced"
    assert location["location_id"] not in {
        row["location_id"] for row in store.accessible_locations(owner["user_id"])
    }
    store.set_mutation_locations(owner["user_id"], [], actor=admin)
    assert store.accessible_locations(owner["user_id"]) == []


def test_local_job_list_reuses_control_payloads(setup, tmp_path):  # noqa: F811
    from pathlib import Path

    from lerobot.data_platform import viewer

    store, _, owner, _, location, _ = setup
    store.job_manager.queue_limit = 100
    jobs = [
        store.create_job(
            location_id=location["location_id"],
            requested_by=owner["user_id"],
            operation="local.request.test",
            options={"local_job": {"dataset_key": "example/source", "message": "Waiting"}},
        )
        for _ in range(20)
    ]
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
        static_folder=tmp_path / "console/static",
        template_folder=Path(viewer.__file__).parent / "templates",
        database_url=str(store.engine.url),
        start_server=False,
    )
    client = app.test_client()
    client.post("/api/auth/login", json={"username": owner["username"], "password": "long-test-password"})
    query_store = app.extensions["data_platform_route_context"].control_plane_store
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(query_store.engine, "before_cursor_execute", record)
    try:
        response = client.get("/api/jobs")
    finally:
        event.remove(query_store.engine, "before_cursor_execute", record)
    assert response.status_code == 200
    result = response.json["jobs"]
    assert {row["id"] for row in result} == {row["job_id"] for row in jobs}
    assert len(statements) <= 20
    assert all(row["available_actions"] == ["cancel"] for row in result)
    assert all(row["control_job_id"] == row["id"] for row in result)


def test_registered_directory_refresh_does_not_walk_the_filesystem(setup, tmp_path, monkeypatch):  # noqa: F811
    from pathlib import Path

    from lerobot.data_platform import viewer
    from tests.datasets.test_data_platform_agent import _make_dataset

    store, admin, _, _, _, _ = setup
    root = tmp_path / "datasets"
    known, unseen = root / "known", root / "nested" / "unseen"
    _make_dataset(known)
    _make_dataset(unseen)
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
        static_folder=tmp_path / "console/static",
        template_folder=Path(viewer.__file__).parent / "templates",
        datasets_root=root,
        database_url=str(store.engine.url),
        start_server=False,
    )
    client = app.test_client()
    assert (
        client.post(
            "/api/auth/login", json={"username": admin["username"], "password": "long-test-password"}
        ).status_code
        == 200
    )
    assert client.post("/api/datasets/register", json={"root": str(known)}).status_code == 200
    walks = []
    original = Path.iterdir

    def counted(path):
        if path == root or path == root / "nested":
            walks.append(path)
        return original(path)

    monkeypatch.setattr(Path, "iterdir", counted)
    response = client.get("/api/dataset_roots", query_string={"root_dir": str(root), "scan": "0"})
    assert response.status_code == 200
    assert walks == []
    assert {row["root"] for row in response.json["candidates"]} == {str(known)}
    assert client.get("/api/dataset_roots?scan=invalid").status_code == 400
    for query in ({"scan": "1"}, {}):
        response = client.get("/api/dataset_roots", query_string=query)
        assert response.status_code == 200
        assert {row["root"] for row in response.json["candidates"]} == {str(known), str(unseen)}
    assert root in walks

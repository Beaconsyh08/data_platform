"""List refreshes must use bounded reads without weakening current grants or job controls."""

import pytest
from sqlalchemy import event

from lerobot.data_platform.control_plane import DatasetLocation, RemoteJob
from lerobot.data_platform.management_storage import JobControl
from tests.datasets.test_control_plane import _app
from tests.datasets.test_job_management import setup  # noqa: F401 -- shared fixture


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

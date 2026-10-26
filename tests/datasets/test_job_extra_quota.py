"""Administrator grants must release only the selected job from the user running quota."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from lerobot.data_platform.control_plane import ControlPlaneNode, ControlPlaneStore, RemoteJob
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.maintenance import set_maintenance
from lerobot.data_platform.management_storage import AuditOutbox, JobControl, migrate_management
from tests.datasets.test_control_plane import _app
from tests.datasets.test_job_management import command, setup  # noqa: F401 -- shared fixture


@pytest.fixture
def queue(setup):  # noqa: F811 -- pytest resolves the imported fixture
    store, admin, owner, node, _, _ = setup
    store.job_manager.node_limit = 4
    store.job_manager.user_limit = 2
    with store.sessions.begin() as session:
        row = session.get(ControlPlaneNode, node["node_id"])
        row.capabilities = {**row.capabilities, "job_concurrent_limit": 4}
    jobs = []
    for index in range(5):
        location = store.sync_locations(
            node["node_id"],
            [
                {
                    "root": f"/data/input-{index}",
                    "dataset_key": f"input-{index}",
                    "output_dir": f"/data/cache-{index}",
                }
            ],
        )[0]
        jobs.append(
            store.create_job(
                location_id=location["location_id"], requested_by=owner["user_id"], operation="viewer.prepare"
            )
        )
    return store, admin, owner, node, jobs


def claim(store, node):
    return store.claim_job(node["node_id"], worker_instance_id="test-worker")


def test_grant_runs_selected_third_job_and_preserves_normal_slots(queue):
    store, admin, owner, node, jobs = queue
    first, second = claim(store, node), claim(store, node)
    assert claim(store, node) is None
    # An earlier queued job must not consume the selected job's additional slot.
    selected = jobs[3]
    granted = command(store, selected, admin, "extra-quota", enabled=True)
    assert granted["extra_quota"] is True
    assert store.job_manager.decorate(store.get_job(selected["job_id"]), admin)["queue_wait_reason"] is None
    extra = claim(store, node)
    assert extra["job_id"] == selected["job_id"]
    assert claim(store, node) is None
    ordinary = store.job_manager.decorate(store.get_job(jobs[2]["job_id"]), owner)
    assert ordinary["queue_wait_reason"] == "user_capacity"
    execution = first["execution"]
    store.complete_job(
        first["job_id"],
        node_id=node["node_id"],
        status="error",
        attempt_id=execution["attempt_id"],
        credential=execution["credential"],
    )
    assert claim(store, node)["job_id"] == jobs[2]["job_id"]
    assert store.get_job(second["job_id"])["status"] == "running"
    assert store.get_job(extra["job_id"])["status"] == "running"
    assert claim(store, node) is None


def test_grant_is_durable_idempotent_revision_checked_and_audited(queue):
    store, admin, _, _, jobs = queue
    job = jobs[2]
    body = {"revision": 0, "idempotency_key": "grant-once", "enabled": True}
    result = store.job_manager.command(job["job_id"], admin, "extra-quota", body)
    assert store.job_manager.command(job["job_id"], admin, "extra-quota", body) == result
    with pytest.raises(JobConflictError):
        store.job_manager.command(job["job_id"], admin, "extra-quota", {**body, "enabled": False})
    with pytest.raises(JobConflictError):
        store.job_manager.command(job["job_id"], admin, "extra-quota", {**body, "idempotency_key": "stale"})
    reopened = ControlPlaneStore(str(store.engine.url))
    assert reopened.job_manager.decorate(reopened.get_job(job["job_id"]), admin)["extra_quota"] is True
    with store.sessions() as session:
        events = [
            row.payload
            for row in session.scalars(select(AuditOutbox))
            if row.payload["operation"] == "job.extra-quota"
        ]
        assert len(events) == 1
        assert events[0]["actor"]["user_id"] == admin["user_id"]
        assert events[0]["details"]["extra_quota"] is True
    assert command(store, job, admin, "extra-quota", enabled=False)["extra_quota"] is False


@pytest.mark.parametrize("role", ["operator", "data_manager", "viewer"])
def test_extra_quota_api_rejects_non_admin(queue, tmp_path, role):
    store, _, owner, _, jobs = queue
    owner = store.update_user(owner["user_id"], role=role)
    client = _app(tmp_path, store).test_client()
    assert (
        client.post(
            "/api/auth/login", json={"username": owner["username"], "password": "long-test-password"}
        ).status_code
        == 200
    )
    response = client.patch(
        f"/api/control/jobs/{jobs[2]['job_id']}/extra-quota",
        json={"revision": 0, "idempotency_key": "forged-grant", "enabled": True},
    )
    assert response.status_code == 403
    with pytest.raises(PermissionError):
        command(store, jobs[2], owner, "extra-quota", enabled=True)
    assert (
        "extra-quota"
        not in store.job_manager.decorate(store.get_job(jobs[2]["job_id"]), owner)["available_actions"]
    )
    assert store.job_manager.decorate(store.get_job(jobs[2]["job_id"]))["extra_quota"] is False


def test_extra_quota_api_admin_validates_and_rejects_running_job(queue, tmp_path):
    store, admin, _, node, jobs = queue
    client = _app(tmp_path, store).test_client()
    client.post("/api/auth/login", json={"username": admin["username"], "password": "long-test-password"})
    url = f"/api/control/jobs/{jobs[0]['job_id']}/extra-quota"
    body = {"revision": 0, "idempotency_key": "grant", "enabled": True}
    assert client.patch(url, json={**body, "enabled": "true"}).status_code == 400
    response = client.patch(url, json=body)
    assert response.status_code == 200
    job = response.get_json()["job"]
    assert job["extra_quota"] is True
    assert "extra-quota" in job["available_actions"]
    running = claim(store, node)
    assert running["job_id"] == jobs[0]["job_id"]
    assert "extra-quota" not in store.job_manager.decorate(running, admin)["available_actions"]
    assert (
        client.patch(
            url, json={**body, "revision": running["revision"], "idempotency_key": "revoke", "enabled": False}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("guard", ["node", "agent", "paths", "maintenance", "scope", "inactive"])
def test_extra_quota_retains_other_admission_guards(queue, guard):
    store, admin, owner, node, jobs = queue
    first = claim(store, node)
    claim(store, node)
    selected = jobs[2]
    command(store, selected, admin, "extra-quota", enabled=True)
    if guard == "node":
        store.job_manager.node_limit = 2
    elif guard == "agent":
        with store.sessions.begin() as session:
            row = session.get(ControlPlaneNode, node["node_id"])
            row.capabilities = {**row.capabilities, "job_concurrent_limit": 2}
    elif guard == "paths":
        with store.sessions.begin() as session:
            session.get(RemoteJob, selected["job_id"]).location_id = first["location_id"]
    elif guard == "maintenance":
        set_maintenance(store, True)
    elif guard == "inactive":
        store.update_user(owner["user_id"], active=False)
    else:
        store.set_mutation_locations(owner["user_id"], [], actor=admin, all_locations=False)
    assert claim(store, node) is None


def test_extra_quota_claim_race_and_manual_retry(queue):
    store, admin, owner, node, jobs = queue
    claim(store, node)
    claim(store, node)
    command(store, jobs[2], admin, "extra-quota", enabled=True)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: claim(store, node), range(2)))
    assert sum(result is not None for result in results) == 1
    extra = next(result for result in results if result)
    execution = extra["execution"]
    store.complete_job(
        extra["job_id"],
        node_id=node["node_id"],
        status="error",
        attempt_id=execution["attempt_id"],
        credential=execution["credential"],
    )
    retried = command(store, extra, owner, "retry")
    assert retried["extra_quota"] is False
    assert claim(store, node) is None


def test_management_migration_adds_quota_without_granting_existing_jobs(queue):
    store, _, _, _, jobs = queue
    with store.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE dp_job_controls DROP COLUMN extra_quota")
        connection.exec_driver_sql("DELETE FROM dp_management_migrations WHERE version = 6")
    migrate_management(store.engine)
    migrate_management(store.engine)
    with store.sessions() as session:
        assert session.get(JobControl, jobs[0]["job_id"]).extra_quota is False


def test_user_quota_and_queue_reason_include_other_nodes(queue):
    store, _, owner, node, jobs = queue
    claim(store, node)
    claim(store, node)
    _, other = store.enroll_node(
        name="node-two",
        hostname="localhost",
        allowed_roots=["/data"],
        writable_roots=["/data"],
        capabilities={"job_protocol": 2, "data_profile_protocol": 100, "job_concurrent_limit": 4},
        enrollment_token="enrollment",
        expected_token="enrollment",
    )
    with store.sessions.begin() as session:
        session.get(RemoteJob, jobs[2]["job_id"]).node_id = other["node_id"]
    assert claim(store, other) is None
    assert (
        store.job_manager.decorate(store.get_job(jobs[2]["job_id"]), owner)["queue_wait_reason"]
        == "user_capacity"
    )


def test_extra_quota_cannot_be_granted_through_job_options(queue):
    store, _, owner, node, jobs = queue
    claim(store, node)
    claim(store, node)
    forged = store.create_job(
        location_id=jobs[4]["location_id"],
        requested_by=owner["user_id"],
        operation="viewer.prepare",
        options={"extra_quota": True},
    )
    assert store.job_manager.decorate(forged, owner)["extra_quota"] is False
    assert claim(store, node) is None


def test_mcap_automatic_recovery_retains_job_grant(tmp_path):
    from tests.datasets.test_mcap_import import _setup
    from tests.datasets.test_mcap_recovery import LOST_COMPUTE_ERROR, complete

    store, _, admin, node, location, _ = _setup(tmp_path)
    job = store.create_job(
        location_id=location["location_id"],
        requested_by=admin["user_id"],
        operation="import.mcap.convert",
        options={"out_root": "/datasets/converted/00001", "config": {"max_files": 0}},
    )
    command(store, job, admin, "extra-quota", enabled=True)
    extra = claim(store, node)
    assert complete(store, node, extra, status="error", error=LOST_COMPUTE_ERROR)["status"] == "queued"
    assert claim(store, node)["extra_quota"] is True

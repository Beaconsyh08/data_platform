"""Regression coverage for durable controls, isolation and independent audit delivery."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from lerobot.data_platform.control_plane import ControlPlaneNode, ControlPlaneStore, RemoteJob, _utcnow
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.management_storage import EventSpool, LogDelivery, UsageLogStore
from lerobot.data_platform.operation_log import build_operation_event
from tests.datasets.test_control_plane import _app, _bootstrap


@pytest.fixture
def setup(tmp_path):
    store = ControlPlaneStore(f"sqlite:///{tmp_path / 'control.db'}")
    admin = store.bootstrap_admin(
        username="admin-user",
        password="long-test-password",
        display_name="Admin",
        bootstrap_token="test",
        expected_token="test",
    )
    owner = store.register_user(
        username="owner-user", password="long-test-password", display_name="Owner", role="operator"
    )
    _, node = store.enroll_node(
        name="node-one",
        hostname="localhost",
        allowed_roots=["/data"],
        writable_roots=["/data"],
        capabilities={"job_protocol": 2, "data_profile_protocol": 100},
        enrollment_token="enrollment",
        expected_token="enrollment",
    )
    location = store.sync_locations(
        node["node_id"], [{"root": "/data/source", "dataset_key": "source", "output_dir": "/data/cache"}]
    )[0]

    def create(user=owner):
        return store.create_job(
            location_id=location["location_id"], requested_by=user["user_id"], operation="viewer.prepare"
        )

    return store, admin, owner, node, location, create


def command(store, job, actor, action, **extra):
    current = store.job_manager.decorate(store.get_job(job["job_id"]), actor)
    return store.job_manager.command(
        job["job_id"],
        actor,
        action,
        {"revision": current["revision"], "idempotency_key": f"{action}-{current['revision']}", **extra},
    )


@pytest.mark.parametrize("status", ["running", "cancel_requested", "interrupted"])
def test_deployment_drain_reports_all_blockers_without_discarding_jobs(setup, status):
    from lerobot.data_platform.maintenance import JobDrainError, active_job_counts, wait_until_idle

    store, _, _, _, _, create = setup
    queued = create()
    job = create()
    with store.sessions.begin() as session:
        session.get(RemoteJob, job["job_id"]).status = status
    counts = active_job_counts(store)
    assert counts.total == 1
    assert getattr(counts, status) == 1
    with pytest.raises(JobDrainError) as error:
        wait_until_idle(store, timeout=0)
    assert error.value.code == ("interrupted_jobs" if status == "interrupted" else "drain_timeout")
    assert error.value.counts == counts
    assert "running or finalizing" in str(error.value)
    assert store.get_job(job["job_id"])["status"] == status
    assert store.get_job(queued["job_id"])["status"] == "queued"


def test_deployment_wait_finishes_after_current_job_is_acknowledged(setup, monkeypatch):
    from lerobot.data_platform.maintenance import active_jobs, wait_until_idle

    store, _, _, _, _, create = setup
    job = create()
    with store.sessions.begin() as session:
        session.get(RemoteJob, job["job_id"]).status = "running"

    def acknowledge(_):
        with store.sessions.begin() as session:
            session.get(RemoteJob, job["job_id"]).status = "done"

    monkeypatch.setattr("time.sleep", acknowledge)
    wait_until_idle(store)
    assert active_jobs(store) == 0


def test_owner_job_listing_sorts_ids_without_large_json(setup):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    store, admin, owner, _, _, create = setup
    first, other, last = create(), create(admin), create()
    payload = {"details": "x" * (512 * 1024)}
    with store.sessions.begin() as session:
        for index, job in enumerate((first, other, last)):
            row = session.get(RemoteJob, job["job_id"])
            row.created_at = _utcnow() + timedelta(seconds=index)
            row.options = payload
            row.result = payload

    def reject_json_sort(connection, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if "order by" in sql and ("options_json" in sql or "result_json" in sql):
            raise OperationalError(statement, parameters, Exception(1038, "Out of sort memory"))

    event.listen(store.engine, "before_cursor_execute", reject_json_sort)
    try:
        own_jobs = store.list_jobs(actor=owner)
        assert [j["job_id"] for j in own_jobs] == [last["job_id"], first["job_id"]]
        assert all(j["options"] == payload and j["result"] == payload for j in own_jobs)
        assert [j["job_id"] for j in store.list_jobs(actor=owner, limit=1)] == [last["job_id"]]
        assert [j["job_id"] for j in store.list_jobs(actor=admin)] == [
            last["job_id"],
            other["job_id"],
            first["job_id"],
        ]
        assert store.list_jobs(actor={"role": "operator", "user_id": "missing"}) == []
    finally:
        event.remove(store.engine, "before_cursor_execute", reject_json_sort)


def test_cancel_queue_and_retry_keep_identity(setup):
    store, _, owner, node, _, create = setup
    job = create()
    cancelled = command(store, job, owner, "cancel")
    assert cancelled["status"] == "cancelled"
    assert store.claim_job(node["node_id"], worker_instance_id="instance") is None
    retried = command(store, job, owner, "retry")
    assert retried["job_id"] == job["job_id"]
    assert retried["status"] == "queued"
    claimed = store.claim_job(node["node_id"], worker_instance_id="instance")
    assert claimed["execution"]["attempt_id"]
    assert store.job_manager.attempts(job["job_id"])[0]["attempt_no"] == 1


def test_claim_race_has_one_winner(setup):
    store, _, _, node, _, create = setup
    create()
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda worker: store.claim_job(node["node_id"], worker_instance_id=worker), ["one", "two"]
            )
        )
    assert sum(result is not None for result in results) == 1


def test_caption_jobs_share_node_only_for_distinct_episodes(setup):
    store, _, owner, node, location, create = setup
    store.job_manager.caption_limit = 2
    with store.sessions.begin() as session:
        row = session.get(ControlPlaneNode, node["node_id"])
        row.capabilities = {**row.capabilities, "caption_concurrent_limit": 2}

    def caption(episode):
        return store.create_job(
            location_id=location["location_id"],
            requested_by=owner["user_id"],
            operation="caption.annotate",
            options={"episode_index": episode, "scheme": "fusion_review"},
        )

    first = caption(0)
    duplicate = caption(0)
    second = caption(1)
    normal = create()
    claimed_first = store.claim_job(node["node_id"], worker_instance_id="one")
    claimed_second = store.claim_job(node["node_id"], worker_instance_id="two")
    assert {claimed_first["job_id"], claimed_second["job_id"]} == {first["job_id"], second["job_id"]}
    assert store.claim_job(node["node_id"], worker_instance_id="three") is None
    assert store.get_job(duplicate["job_id"])["status"] == "queued"
    assert store.get_job(normal["job_id"])["status"] == "queued"

    for claimed in (claimed_first, claimed_second):
        store.complete_job(
            claimed["job_id"],
            node_id=node["node_id"],
            status="error",
            attempt_id=claimed["execution"]["attempt_id"],
            credential=claimed["execution"]["credential"],
        )
    assert store.claim_job(node["node_id"], worker_instance_id="four") is not None


def test_caption_parallelism_requires_matching_agent_capability(setup):
    store, _, owner, node, location, _ = setup
    store.job_manager.caption_limit = 2
    for episode in (0, 1):
        store.create_job(
            location_id=location["location_id"],
            requested_by=owner["user_id"],
            operation="caption.annotate",
            options={"episode_index": episode, "scheme": "fusion_review"},
        )
    assert store.claim_job(node["node_id"], worker_instance_id="one") is not None
    assert store.claim_job(node["node_id"], worker_instance_id="two") is None


def _enable_two_slots(store, node):
    store.job_manager.node_limit = 2
    store.job_manager.user_limit = 2
    with store.sessions.begin() as session:
        row = session.get(ControlPlaneNode, node["node_id"])
        row.capabilities = {**row.capabilities, "job_concurrent_limit": 2}


def test_two_independent_viewer_jobs_claim_and_report_slots(setup):
    store, _, owner, node, location, create = setup
    _enable_two_slots(store, node)
    other = store.sync_locations(
        node["node_id"],
        [{"root": "/data/other", "dataset_key": "other", "output_dir": "/data/other-cache"}],
    )[0]
    first = create()
    second = store.create_job(
        location_id=other["location_id"], requested_by=owner["user_id"], operation="viewer.prepare"
    )
    claimed_first = store.claim_job(node["node_id"], worker_instance_id="one")
    claimed_second = store.claim_job(node["node_id"], worker_instance_id="two")
    assert claimed_first["job_id"] == first["job_id"]
    assert claimed_second["job_id"] == second["job_id"]
    queued = store.create_job(
        location_id=other["location_id"], requested_by=owner["user_id"], operation="viewer.prepare"
    )
    status = store.job_manager.decorate(store.get_job(queued["job_id"]), owner)
    assert status["node_slots"] == {"used": 2, "limit": 2}
    assert status["queue_wait_reason"] == "data_path_conflict"
    third = store.sync_locations(
        node["node_id"],
        [{"root": "/data/third", "dataset_key": "third", "output_dir": "/data/third-cache"}],
    )[0]
    independent = store.create_job(
        location_id=third["location_id"], requested_by=owner["user_id"], operation="viewer.prepare"
    )
    assert (
        store.job_manager.decorate(store.get_job(independent["job_id"]), owner)["queue_wait_reason"]
        == "node_capacity"
    )
    assert command(store, first, owner, "cancel")["status"] == "cancel_requested"
    assert store.claim_job(node["node_id"], worker_instance_id="three") is None
    store.complete_job(
        first["job_id"],
        node_id=node["node_id"],
        status="cancelled",
        attempt_id=claimed_first["execution"]["attempt_id"],
        credential=claimed_first["execution"]["credential"],
    )
    assert store.claim_job(node["node_id"], worker_instance_id="three")["job_id"] == independent["job_id"]
    assert store.get_job(second["job_id"])["status"] == "running"


@pytest.mark.parametrize("other_output", ["/data/cache", "/data/cache/subdir"])
def test_shared_output_and_merge_secondary_input_wait(setup, other_output):
    store, _, owner, node, location, create = setup
    _enable_two_slots(store, node)
    shared_cache = store.sync_locations(
        node["node_id"],
        [{"root": "/data/other", "dataset_key": "other", "output_dir": other_output}],
    )[0]
    first = create()
    shared = store.create_job(
        location_id=shared_cache["location_id"],
        requested_by=owner["user_id"],
        operation="viewer.prepare",
    )
    merge = store.create_job(
        location_id=shared_cache["location_id"],
        requested_by=owner["user_id"],
        operation="preprocess.merge",
        options={"source_location_ids": [location["location_id"]]},
    )
    assert store.claim_job(node["node_id"], worker_instance_id="one")["job_id"] == first["job_id"]
    assert store.claim_job(node["node_id"], worker_instance_id="two") is None
    for job in (shared, merge):
        status = store.job_manager.decorate(store.get_job(job["job_id"]), owner)
        assert status["queue_wait_reason"] == "data_path_conflict"


def test_caption_can_mix_with_other_dataset_but_mutation_waits(setup):
    store, admin, owner, node, location, _ = setup
    _enable_two_slots(store, node)
    other = store.sync_locations(
        node["node_id"],
        [{"root": "/data/other", "dataset_key": "other", "output_dir": "/data/other-cache"}],
    )[0]
    third = store.sync_locations(
        node["node_id"],
        [{"root": "/data/third", "dataset_key": "third", "output_dir": "/data/third-cache"}],
    )[0]
    caption = store.create_job(
        location_id=location["location_id"],
        requested_by=owner["user_id"],
        operation="caption.annotate",
        options={"episode_index": 0},
    )
    viewer = store.create_job(
        location_id=other["location_id"], requested_by=owner["user_id"], operation="viewer.prepare"
    )
    mutation = store.create_job(
        location_id=third["location_id"],
        requested_by=admin["user_id"],
        operation="mutation.value_edit",
    )
    assert store.claim_job(node["node_id"], worker_instance_id="one")["job_id"] == caption["job_id"]
    assert store.claim_job(node["node_id"], worker_instance_id="two")["job_id"] == viewer["job_id"]
    assert (
        store.job_manager.decorate(store.get_job(mutation["job_id"]), admin)["queue_wait_reason"]
        == "exclusive_job"
    )


def test_cancel_does_not_release_running_slot_and_stale_attempt_is_rejected(setup):
    store, _, owner, node, _, create = setup
    job = create()
    claimed = store.claim_job(node["node_id"], worker_instance_id="instance")
    execution = claimed["execution"]
    cancelled = command(store, job, owner, "cancel")
    assert cancelled["status"] == "cancel_requested"
    create()
    assert store.claim_job(node["node_id"], worker_instance_id="another") is None
    with pytest.raises(JobConflictError):
        command(store, job, owner, "retry")
    with pytest.raises(JobConflictError):
        store.complete_job(
            job["job_id"], node_id=node["node_id"], status="cancelled", attempt_id="stale", credential="bad"
        )
    store.complete_job(
        job["job_id"],
        node_id=node["node_id"],
        status="cancelled",
        attempt_id=execution["attempt_id"],
        credential=execution["credential"],
    )
    assert store.claim_job(node["node_id"], worker_instance_id="another") is not None


def test_expired_lease_is_interrupted_without_reclaim(setup):
    store, _, owner, node, _, create = setup
    job = create()
    store.claim_job(node["node_id"], worker_instance_id="instance")
    with store.sessions.begin() as session:
        row = session.get(RemoteJob, job["job_id"])
        row.lease_until = _utcnow() - timedelta(seconds=1)
    assert store.claim_job(node["node_id"], worker_instance_id="other") is None
    assert store.get_job(job["job_id"])["status"] == "interrupted"
    with pytest.raises(JobConflictError):
        command(store, job, owner, "retry")


def test_priority_requires_admin_and_finalizing_rejects_stop(setup):
    store, admin, owner, node, _, create = setup
    job = create()
    with pytest.raises(PermissionError):
        command(store, job, owner, "priority", priority=2)
    command(store, job, admin, "priority", priority=2)
    claimed = store.claim_job(node["node_id"], worker_instance_id="instance")
    execution = claimed["execution"]
    store.job_manager.checkpoint(
        job["job_id"], node["node_id"], execution["attempt_id"], execution["credential"], phase="finalizing"
    )
    with pytest.raises(JobConflictError):
        command(store, job, admin, "terminate", confirm=True, reason="test")


def test_log_failure_does_not_rollback_submission_and_delivery_is_idempotent(setup, tmp_path, monkeypatch):
    store, _, _, _, _, create = setup
    logs = UsageLogStore(f"sqlite:///{tmp_path / 'logs.db'}")
    delivery = LogDelivery(store, logs, EventSpool(tmp_path / "spool"))
    real_append = logs.append
    monkeypatch.setattr(logs, "append", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    job = create()
    delivery.flush()
    assert store.get_job(job["job_id"])["status"] == "queued"
    assert delivery.status()["pending_control_events"] >= 1
    monkeypatch.setattr(logs, "append", real_append)
    delivery.flush()
    delivery.flush()
    assert len(logs.query({"job_id": job["job_id"]})["events"]) == 1
    assert delivery.status()["pending_control_events"] == 0


def test_spool_capacity_and_redaction(tmp_path):
    spool = EventSpool(tmp_path / "spool", max_bytes=4096)
    event = build_operation_event(
        "test",
        status="success",
        details={"credential": "do-not-store", "url": "mysql://user:password@host/db"},
    )
    spool.write(event)
    text = next(spool.root.glob("*.json")).read_text()
    assert "do-not-store" not in text and "user:password" not in text
    spool.max_bytes = 1
    with pytest.raises(RuntimeError):
        spool.write(event)


def test_admin_browser_existing_role_and_denied_login_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PLATFORM_LOG_DATABASE_URL", f"sqlite:///{tmp_path / 'logs.db'}")
    store = ControlPlaneStore(f"sqlite:///{tmp_path / 'control.db'}")
    app = _app(tmp_path, store)
    client = app.test_client()
    assert client.get("/api/admin/database/tables").status_code == 401
    assert (
        client.post("/api/auth/login", json={"username": "unknown", "password": "do-not-store"}).status_code
        == 403
    )
    assert _bootstrap(client).status_code == 200
    html = client.get("/control-plane").get_data(as_text=True)
    assert 'id="admin-usage-panel"' in html and 'id="admin-database-panel"' in html
    records = client.get("/api/admin/database/tables/control.users/rows")
    assert records.status_code == 200
    assert "password" not in records.get_data(as_text=True)
    assert client.get("/api/admin/database/tables/dp_sessions/rows").status_code == 404
    assert (
        client.get("/api/admin/database/tables/control.users/rows?field=password_digest&value=x").status_code
        == 400
    )
    app.extensions["data_platform_log_delivery"].flush()
    logs = client.get("/api/admin/usage-events?status=failed").get_json()["events"]
    assert any(row["operation"] == "control_plane_login" for row in logs)
    assert "do-not-store" not in json.dumps(logs)
    viewer = store.register_user(username="read-only", password="long-test-password", display_name="Viewer")
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"username": viewer["username"], "password": "long-test-password"})
    assert client.get("/api/admin/database/tables").status_code == 403
    assert 'id="admin-usage-panel"' not in client.get("/control-plane").get_data(as_text=True)
    app.extensions["data_platform_log_delivery"].flush()
    denied = app.extensions["data_platform_log_delivery"].logs.query({"status": "failed"})["events"]
    assert any(row["actor"].get("user_id") == viewer["user_id"] for row in denied)


def test_pipeline_controls_require_submitter_even_for_admin(setup, tmp_path):
    store, admin, owner, node, _, create = setup
    other = store.register_user(
        username="other-user", password="long-test-password", display_name="Other", role="operator"
    )
    job = create()
    app = _app(tmp_path, store)
    app.testing = True
    client = app.test_client()

    def login(account):
        response = client.post(
            "/api/auth/login", json={"username": account["username"], "password": "long-test-password"}
        )
        assert response.status_code == 200

    login(other)
    body = {"revision": 0, "idempotency_key": "cancel-test"}
    assert client.post(f"/api/jobs/{job['job_id']}/cancel", json=body).status_code == 403
    assert client.post(f"/api/control/jobs/{job['job_id']}/cancel", json=body).status_code == 403
    login(admin)
    assert client.post(f"/api/jobs/{job['job_id']}/cancel", json=body).status_code == 403
    # Management remains authorized to control everybody's jobs.
    response = client.post(f"/api/control/jobs/{job['job_id']}/cancel", json=body)
    assert response.status_code == 200
    assert response.json["job"]["requested_by_username"] == owner["username"]
    cancelled = response.json["job"]
    login(owner)
    response = client.post(
        f"/api/jobs/{job['job_id']}/retry",
        json={"revision": cancelled["revision"], "idempotency_key": "retry-test"},
    )
    assert response.status_code == 200
    assert response.json["job"]["status"] == "queued"
    claimed = store.claim_job(node["node_id"], worker_instance_id="worker")
    current = store.job_manager.decorate(store.get_job(job["job_id"]), owner)
    response = client.post(
        f"/api/jobs/{job['job_id']}/cancel",
        json={"revision": current["revision"], "idempotency_key": "stop-test"},
    )
    assert response.status_code == 202
    assert response.json["job"]["status"] == "cancel_requested"
    assert claimed["execution"]["attempt_id"]
    assert not response.json["job"]["stop_confirmed"]


def test_job_reads_are_private_and_queue_exposes_only_count(setup, tmp_path):
    store, admin, owner, _, _, create = setup
    other_job = create(admin)
    own_job = create()
    app = _app(tmp_path, store)
    client = app.test_client()
    for actor in (owner, admin):
        client.post("/api/auth/login", json={"username": actor["username"], "password": "long-test-password"})
        jobs = client.get("/api/control/jobs").get_json()["jobs"]
        expected = {own_job["job_id"], other_job["job_id"]} if actor == admin else {own_job["job_id"]}
        assert {job["job_id"] for job in jobs} == expected
        own = next(job for job in jobs if job["job_id"] == own_job["job_id"])
        assert own["queue_ahead"] == 1
        for suffix in ("", "/events", "/attempts"):
            assert client.get(f"/api/control/jobs/{own_job['job_id']}{suffix}").status_code == 200
            assert client.get(f"/api/control/jobs/{other_job['job_id']}{suffix}").status_code == (
                200 if actor == admin else 404
            )
    viewer = store.register_user(
        username="read-only", password="long-test-password", display_name="Viewer", role="viewer"
    )
    client.post("/api/auth/login", json={"username": viewer["username"], "password": "long-test-password"})
    assert client.get("/api/control/jobs").get_json()["jobs"] == []
    assert client.get(f"/api/control/jobs/{own_job['job_id']}").status_code == 404


def test_four_node_slots_keep_two_per_user(setup):
    store, admin, owner, node, _, _ = setup
    store.job_manager.node_limit = 4
    store.job_manager.user_limit = 2
    with store.sessions.begin() as session:
        row = session.get(ControlPlaneNode, node["node_id"])
        row.capabilities = {**row.capabilities, "job_concurrent_limit": 4}
    jobs = []
    for index, user in enumerate([owner, owner, owner, admin, admin, admin]):
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
                location_id=location["location_id"], requested_by=user["user_id"], operation="viewer.prepare"
            )
        )
    first = [store.claim_job(node["node_id"], worker_instance_id=f"worker-{i}") for i in range(2)]
    assert [job["job_id"] for job in first] == [job["job_id"] for job in jobs[:2]]
    assert (
        store.job_manager.decorate(store.get_job(jobs[2]["job_id"]))["queue_wait_reason"] == "user_capacity"
    )
    second = [store.claim_job(node["node_id"], worker_instance_id=f"other-{i}") for i in range(2)]
    assert [job["job_id"] for job in second] == [job["job_id"] for job in jobs[3:5]]
    assert store.claim_job(node["node_id"], worker_instance_id="fifth") is None
    status = store.job_manager.decorate(store.get_job(jobs[5]["job_id"]))
    assert status["node_slots"] == {"used": 4, "limit": 4}
    assert status["queue_wait_reason"] == "node_capacity"


def test_mysql_scheduler_lock_serializes_claims_without_row_changes():
    import os

    from sqlalchemy import select
    from sqlalchemy.engine import make_url

    from lerobot.data_platform.management_storage import SchedulerLock

    url = os.environ.get("DATA_PLATFORM_TEST_MYSQL_URL")
    if not url:
        pytest.skip("Requires an isolated native MySQL acceptance database")
    assert make_url(url).database.startswith("dp_test_")
    store = ControlPlaneStore(url)
    owner = store.bootstrap_admin(
        username="native-admin",
        password="long-native-password",
        display_name="Native Admin",
        bootstrap_token="test",
        expected_token="test",
    )
    _, node = store.enroll_node(
        name="native-node",
        hostname="localhost",
        allowed_roots=["/data"],
        writable_roots=[],
        capabilities={"job_protocol": 2, "data_profile_protocol": 100},
        enrollment_token="test",
        expected_token="test",
    )
    location = store.sync_locations(node["node_id"], [{"root": "/data/source", "dataset_key": "source"}])[0]

    def revision():
        with store.sessions() as session:
            return session.scalar(select(SchedulerLock.revision).where(SchedulerLock.lock_id == 1))

    before = revision()
    for index in range(5):
        assert store.claim_job(node["node_id"], worker_instance_id=str(index)) is None
    assert revision() == before
    store.create_job(
        location_id=location["location_id"],
        requested_by=owner["user_id"],
        operation="viewer.prepare",
    )
    with ThreadPoolExecutor(8) as pool:
        claims = list(
            pool.map(
                lambda index: store.claim_job(node["node_id"], worker_instance_id=str(index)),
                range(8),
            )
        )
    assert sum(claim is not None for claim in claims) == 1
    assert revision() == before
    store.engine.dispose()


@pytest.mark.parametrize("secondary", [False, True])
def test_deleted_input_disables_retry_and_rejects_direct_command(setup, secondary):
    from lerobot.data_platform.control_plane import DatasetLocation

    store, admin, owner, node, location, create = setup
    job = create()
    command(store, job, owner, "cancel")
    retired = location
    if secondary:
        retired = store.sync_locations(
            node["node_id"],
            [{"root": "/data/other", "dataset_key": "other", "output_dir": "/data/other-cache"}],
        )[0]
    with store.sessions.begin() as session:
        session.get(DatasetLocation, retired["location_id"]).state = "deleting"
        if secondary:
            session.get(RemoteJob, job["job_id"]).options = {"source_location_ids": [retired["location_id"]]}
    visible = store.job_manager.decorate(store.get_job(job["job_id"]), owner)
    assert "retry" not in visible["available_actions"]
    assert "deleted" in visible["action_reasons"]["retry"]
    with pytest.raises(JobConflictError, match="deleted"):
        command(store, job, owner, "retry")
    assert store.get_job(job["job_id"])["status"] == "cancelled"


def test_deleted_input_cannot_trigger_automatic_mcap_recovery(setup):
    from types import SimpleNamespace

    from lerobot.data_platform.control_plane import DatasetLocation
    from lerobot.data_platform.job_management import PROCESS_LOSS_ERROR

    store, admin, owner, node, location, create = setup
    job = create()
    with store.sessions.begin() as session:
        row = session.get(RemoteJob, job["job_id"])
        row.operation, row.status = "import.mcap.convert", "running"
        session.get(DatasetLocation, location["location_id"]).state = "deleting"
        assert (
            store.job_manager._recovery_number(
                session,
                row,
                SimpleNamespace(stop_mode=None, phase="computing"),
                SimpleNamespace(protocol=2),
                "error",
                PROCESS_LOSS_ERROR,
            )
            is None
        )

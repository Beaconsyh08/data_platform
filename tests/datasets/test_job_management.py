"""Regression coverage for durable controls, isolation and independent audit delivery."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from lerobot.data_platform.control_plane import ControlPlaneStore, RemoteJob, _utcnow
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

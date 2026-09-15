from pathlib import Path

import pytest

from tests.datasets.test_control_plane import _app, _bootstrap, _store


def setup_requests(tmp_path: Path, *, enabled=True):
    store = _store(tmp_path)
    app = _app(tmp_path, store, legacy_mutations_enabled=enabled)
    admin = app.test_client()
    assert _bootstrap(admin).status_code == 200
    operator = store.register_user(username="delete-operator", password="password-123", role="operator")
    client = app.test_client()
    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": "delete-operator",
                "password": "password-123",
            },
        ).status_code
        == 200
    )
    token, node = store.enroll_node(
        name="node",
        hostname="node",
        allowed_roots=["/datasets"],
        writable_roots=["/datasets"],
        capabilities={"source_mutations_enabled": True, "job_protocol": 1, "data_profile_protocol": 1},
        enrollment_token="enroll",
        expected_token="enroll",
    )
    location = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "node/data",
                "root": "/datasets/data",
                "output_dir": "/datasets/vis",
            }
        ],
    )[0]
    return store, admin, client, operator, node, location, token


def submit(client, location):
    response = client.post(
        f"/api/control/locations/{location['location_id']}/episode-deletion-requests",
        json={
            "episodes": [2, 1, 2],
            "reason": "Incorrect demonstration",
        },
    )
    assert response.status_code == 202
    return response.get_json()["request"]


def test_operator_request_requires_admin_review_and_enqueues_once(tmp_path):
    store, admin, client, operator, node, location, token = setup_requests(tmp_path)
    row = submit(client, location)
    assert row["episodes"] == [1, 2]
    assert store.list_jobs() == []
    endpoint = f"/api/control/episode-deletion-requests/{row['request_id']}/review"
    payload = {"decision": "approve", "reason": "Reviewed", "confirmation": f"DELETE {row['request_id']}"}
    assert client.post(endpoint, json=payload).status_code == 403
    assert (
        client.post(
            f"/api/control/locations/{location['location_id']}/mutation-jobs",
            json={
                "op": "delete_episodes",
                "options": {"episodes": [1], "reason": "bypass"},
                "confirmation": "MUTATE node/data",
            },
        ).status_code
        == 403
    )
    result = admin.post(endpoint, json=payload)
    assert result.status_code == 202, result.get_json()
    job = result.get_json()["job"]
    assert job["requested_by"] == operator["user_id"]
    assert job["operation"] == "mutation.delete_episodes"
    assert job["options"]["episodes"] == [1, 2]
    assert job["options"]["requested_by"]["deletion_request_id"] == row["request_id"]
    assert client.get(f"/api/control/jobs/{job['job_id']}").status_code == 200
    assert admin.post(endpoint, json=payload).status_code == 409
    assert len(store.list_jobs()) == 1
    claimed = admin.post("/api/agents/jobs/claim", headers={"Authorization": f"Bearer {token}"}, json={})
    assert claimed.get_json()["job"]["job_id"] == job["job_id"]


@pytest.mark.parametrize("problem", ["disabled", "agent_disabled", "changed", "confirmation", "queue_full"])
def test_approval_failure_keeps_request_pending_without_job(tmp_path, problem):
    store, admin, client, _, node, location, _ = setup_requests(tmp_path, enabled=problem != "disabled")
    row = submit(client, location)
    if problem == "changed":
        store.sync_locations(
            node["node_id"],
            [
                {
                    "dataset_key": "node/data",
                    "root": "/datasets/data",
                    "metadata": {"total_episodes": 5},
                }
            ],
        )
    if problem == "agent_disabled":
        from lerobot.data_platform.control_plane import ControlPlaneNode

        with store.sessions.begin() as session:
            session.get(ControlPlaneNode, node["node_id"]).capabilities = {}
    if problem == "queue_full":
        store.job_manager.queue_limit = 0
    response = admin.post(
        f"/api/control/episode-deletion-requests/{row['request_id']}/review",
        json={
            "decision": "approve",
            "reason": "Reviewed",
            "confirmation": "wrong" if problem == "confirmation" else f"DELETE {row['request_id']}",
        },
    )
    assert response.status_code in {403, 409}, response.get_json()
    assert store.list_jobs() == []
    assert (
        client.get("/api/control/episode-deletion-requests").get_json()["requests"][0]["status"] == "pending"
    )


def test_rejection_and_request_visibility(tmp_path):
    store, admin, client, _, _, location, _ = setup_requests(tmp_path)
    row = submit(client, location)
    response = admin.post(
        f"/api/control/episode-deletion-requests/{row['request_id']}/review",
        json={
            "decision": "reject",
            "reason": "Keep these demonstrations",
        },
    )
    assert response.status_code == 200
    assert store.list_jobs() == []
    store.register_user(username="other-operator", password="password-123", role="operator")
    client.post("/api/auth/login", json={"username": "other-operator", "password": "password-123"})
    assert client.get("/api/control/episode-deletion-requests").get_json()["requests"] == []
    assert (
        admin.get("/api/control/episode-deletion-requests").get_json()["requests"][0]["status"] == "rejected"
    )


@pytest.mark.parametrize("v3", [False, True])
def test_approved_deletion_executes_with_backup_and_invalidates_viewer(tmp_path, v3):
    import json

    from lerobot.data_platform.control_plane import ControlPlaneNode
    from tests.datasets.test_dataset_merge_alignment import _snapshot
    from tests.datasets.test_remote_dataset_merge import _HttpClient, _remote_setup

    store, _, agent, locations, roots = _remote_setup(tmp_path, v3=v3)
    agent.allow_source_mutations = True
    with store.sessions.begin() as session:
        node = session.get(ControlPlaneNode, agent.state.node_id)
        node.capabilities = {**node.capabilities, "source_mutations_enabled": True}
    admin = _app(tmp_path, store, legacy_mutations_enabled=True).test_client()
    admin.post("/api/auth/login", json={"username": "platform-admin", "password": "strong-admin-password"})
    store.register_user(username="delete-operator", password="password-123", role="operator")
    operator = admin.application.test_client()
    operator.post("/api/auth/login", json={"username": "delete-operator", "password": "password-123"})
    location = locations[-1]
    row = operator.post(
        f"/api/control/locations/{location['location_id']}/episode-deletion-requests",
        json={
            "episodes": [1],
            "reason": "Reviewed bad demonstration",
        },
    ).get_json()["request"]
    result = admin.post(
        f"/api/control/episode-deletion-requests/{row['request_id']}/review",
        json={
            "decision": "approve",
            "reason": "Verified episode",
            "confirmation": f"DELETE {row['request_id']}",
        },
    )
    assert result.status_code == 202
    agent.client = _HttpClient(admin, agent.state.node_token)
    job = admin.post("/api/agents/jobs/claim", headers=agent.client.headers, json={}).get_json()["job"]
    before_other = _snapshot(roots[0])
    output = agent.execute_job(job)
    assert json.loads((roots[-1] / "meta/info.json").read_text())["total_episodes"] == 1
    assert _snapshot(roots[0]) == before_other
    backups = list(
        (roots[-1].parent / ".data-platform-backups" / roots[-1].name).glob("*/dataset/meta/info.json")
    )
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["total_episodes"] == 2
    complete = admin.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        headers=agent.client.headers,
        json={"status": "done", "result": output},
    )
    assert complete.status_code == 200
    assert store.get_location(location["location_id"])["metadata"]["viewer_ready"] is False


def test_forged_approval_does_not_allow_operator_mutation_claim(tmp_path):
    store, admin, _, operator, _, location, token = setup_requests(tmp_path)
    store.create_job(
        location_id=location["location_id"],
        requested_by=operator["user_id"],
        operation="mutation.delete_episodes",
        options={
            "episodes": [1],
            "reason": "forged",
            "requested_by": {"approved_by": "admin", "deletion_request_id": "fake"},
        },
    )
    claimed = admin.post("/api/agents/jobs/claim", headers={"Authorization": f"Bearer {token}"}, json={})
    assert claimed.get_json()["job"] is None

"""Source mutation authorization covers UI scopes, direct submission and execution."""

import pytest
from sqlalchemy import select

from lerobot.data_platform.control_plane import ControlPlaneStore, DataMutationGrant, DatasetLocation
from lerobot.data_platform.management_storage import AuditOutbox
from tests.datasets.test_episode_deletion_requests import setup_requests


def setup_manager(tmp_path, *, enabled=True):
    store, admin, client, user, node, location, token = setup_requests(tmp_path, enabled=enabled)
    response = admin.patch(f"/api/auth/users/{user['user_id']}", json={"role": "data_manager"})
    assert response.status_code == 200
    endpoint = f"/api/auth/users/{user['user_id']}/data-scopes"
    return store, admin, client, user, node, location, token, endpoint


def mutate(client, location, *, op="delete_episodes", options=None):
    return client.post(
        f"/api/control/locations/{location['location_id']}/mutation-jobs",
        json={
            "op": op,
            "options": options if options is not None else {"episodes": [1], "reason": "Incorrect sample"},
            "confirmation": f"MUTATE {location['dataset_key']}",
        },
    )


def test_manager_direct_mutation_needs_scope_and_no_approval(tmp_path):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    assert admin.put(endpoint, json={"location_ids": []}).status_code == 200
    assert mutate(client, location).status_code == 403
    assert store.list_jobs() == []
    assert client.get(endpoint).get_json() == {"location_ids": [], "all_locations": False}
    result = admin.put(endpoint, json={"location_ids": [location["location_id"]]})
    assert result.status_code == 200
    assert client.get(endpoint).get_json() == result.get_json()
    locations = client.get("/api/control/locations").get_json()["locations"]
    assert locations[0]["source_mutation_allowed"] is True
    result = mutate(client, location)
    assert result.status_code == 202, result.get_json()
    job = result.get_json()["job"]
    assert job["requested_by"] == user["user_id"]
    assert job["operation"] == "mutation.delete_episodes"
    assert client.get("/api/control/episode-deletion-requests").get_json()["requests"] == []
    claimed = client.post("/api/agents/jobs/claim", headers={"Authorization": f"Bearer {token}"}, json={})
    assert claimed.get_json()["job"]["job_id"] == job["job_id"]
    with store.sessions() as session:
        assert session.scalars(select(DataMutationGrant)).one().location_id == location["location_id"]
        assert any(
            row.payload.get("operation") == "account.data_scopes.updated"
            for row in session.scalars(select(AuditOutbox))
        )
    reopened = ControlPlaneStore(str(store.engine.url))
    assert reopened.mutation_location_ids(user["user_id"]) == [location["location_id"]]


def test_manager_value_edit_and_operator_capabilities(tmp_path):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    admin.put(endpoint, json={"location_ids": [location["location_id"]]})
    response = mutate(client, location, op="value_edit", options={"edits": [], "reason": "Correct values"})
    assert response.status_code == 202
    assert response.get_json()["job"]["operation"] == "mutation.value_edit"
    assert (
        client.post(f"/api/control/locations/{location['location_id']}/viewer-jobs", json={}).status_code
        == 202
    )
    assert client.get("/api/auth/users").status_code == 403
    assert client.patch(f"/api/auth/users/{user['user_id']}", json={"role": "admin"}).status_code == 403
    assert client.put(endpoint, json={"location_ids": []}).status_code == 403
    assert client.get("/api/admin/database/tables").status_code == 403


@pytest.mark.parametrize("change", ["revoke", "disable", "demote", "root"])
def test_queued_mutation_rechecks_current_authorization(tmp_path, change):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    admin.put(endpoint, json={"location_ids": [location["location_id"]]})
    assert mutate(client, location).status_code == 202
    if change == "revoke":
        assert admin.put(endpoint, json={"location_ids": []}).status_code == 200
    elif change == "disable":
        assert admin.patch(f"/api/auth/users/{user['user_id']}", json={"active": False}).status_code == 200
    elif change == "demote":
        assert admin.patch(f"/api/auth/users/{user['user_id']}", json={"role": "operator"}).status_code == 200
        assert (
            admin.patch(f"/api/auth/users/{user['user_id']}", json={"role": "data_manager"}).status_code
            == 200
        )
        assert client.get(endpoint).get_json()["location_ids"] == []
    else:
        with store.sessions.begin() as session:
            session.get(DatasetLocation, location["location_id"]).root = "/datasets/replaced"
    assert store.claim_job(node["node_id"]) is None


def test_scopes_are_specific_and_only_admin_can_change_them(tmp_path):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    assert admin.put(endpoint, json={"location_ids": [location["location_id"]]}).status_code == 200
    other = store.sync_locations(node["node_id"], [{"dataset_key": "node/other", "root": "/datasets/other"}])[
        0
    ]
    assert mutate(client, other).status_code == 403
    with pytest.raises(PermissionError):
        store.create_job(
            location_id=other["location_id"], requested_by=user["user_id"], operation="mutation.value_edit"
        )
    for invalid in (None, "all", ["missing"], [1], [location["location_id"], "missing"]):
        assert admin.put(endpoint, json={"location_ids": invalid}).status_code == 400
        assert client.get(endpoint).get_json()["location_ids"] == [location["location_id"]]
    assert admin.put(endpoint, json={}).status_code == 400
    other_user = store.register_user(username="other-manager", password="password-123", role="data_manager")
    assert client.get(f"/api/auth/users/{other_user['user_id']}/data-scopes").status_code == 403
    assert set(store.mutation_location_ids(other_user["user_id"])) == {
        location["location_id"],
        other["location_id"],
    }
    assert mutate(client, location, op="delete_dataset").status_code == 400


@pytest.mark.parametrize("episodes", [[], "all", [-1], [True], ["1"], list(range(10001))])
def test_direct_deletion_requires_explicit_valid_episode_indices(tmp_path, episodes):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    admin.put(endpoint, json={"location_ids": [location["location_id"]]})
    response = mutate(client, location, options={"episodes": episodes, "reason": "Correct data"})
    assert response.status_code == 400
    assert store.list_jobs() == []


def test_scoped_mutations_keep_server_agent_and_confirmation_guards(tmp_path):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path, enabled=False)
    admin.put(endpoint, json={"location_ids": [location["location_id"]]})
    assert mutate(client, location).status_code == 403


def test_inactive_account_scopes_remain_editable_but_not_effective(tmp_path):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    admin.patch(f"/api/auth/users/{user['user_id']}", json={"active": False})
    assert admin.put(endpoint, json={"location_ids": [location["location_id"]]}).status_code == 200
    assert admin.get(endpoint).get_json()["location_ids"] == [location["location_id"]]
    assert store.mutation_location_ids(user["user_id"]) == []


def test_schema_upgrade_creates_grants_without_changing_existing_users(tmp_path):
    from sqlalchemy import delete, inspect

    from lerobot.data_platform.management_storage import SchemaMigration

    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    before = store.list_users()
    DataMutationGrant.__table__.drop(store.engine)
    with store.sessions.begin() as session:
        session.execute(delete(SchemaMigration).where(SchemaMigration.version == 3))
    reopened = ControlPlaneStore(str(store.engine.url), initialize_schema=True)
    assert "dp_data_mutation_grants" in inspect(reopened.engine).get_table_names()
    assert reopened.list_users() == before
    with reopened.sessions() as session:
        assert session.get(SchemaMigration, 3) is not None
    assert reopened.mutation_location_ids(user["user_id"]) == [location["location_id"]]


@pytest.mark.parametrize("guard", ["agent", "confirmation", "reason"])
def test_direct_mutation_preserves_execution_guards(tmp_path, guard):
    from lerobot.data_platform.control_plane import ControlPlaneNode

    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    admin.put(endpoint, json={"location_ids": [location["location_id"]]})
    payload = {
        "op": "delete_episodes",
        "options": {"episodes": [1], "reason": "Incorrect sample"},
        "confirmation": f"MUTATE {location['dataset_key']}",
    }
    if guard == "agent":
        with store.sessions.begin() as session:
            session.get(ControlPlaneNode, node["node_id"]).capabilities = {}
    elif guard == "confirmation":
        payload["confirmation"] = ""
    else:
        payload["options"]["reason"] = ""
    response = client.post(f"/api/control/locations/{location['location_id']}/mutation-jobs", json=payload)
    assert response.status_code == (400 if guard == "reason" else 409)
    assert store.list_jobs() == []


def test_manager_keeps_owner_only_job_controls_and_cannot_approve_deletion(tmp_path):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    own = client.post(f"/api/control/locations/{location['location_id']}/viewer-jobs", json={}).get_json()[
        "job"
    ]
    other = admin.post(
        f"/api/control/locations/{location['location_id']}/mutation-jobs",
        json={
            "op": "delete_episodes",
            "options": {"episodes": [1], "reason": "Reviewed"},
            "confirmation": f"MUTATE {location['dataset_key']}",
        },
    ).get_json()["job"]
    own = client.get(f"/api/control/jobs/{own['job_id']}").get_json()["job"]
    other = admin.get(f"/api/control/jobs/{other['job_id']}").get_json()["job"]
    assert client.get(f"/api/control/jobs/{other['job_id']}").status_code == 404
    for action, method in (("priority", "patch"), ("terminate", "post")):
        response = getattr(client, method)(
            f"/api/control/jobs/{own['job_id']}/{action}",
            json={
                "revision": own["revision"],
                "idempotency_key": action,
                "priority": 2,
            },
        )
        assert response.status_code == 403
    response = client.post(
        f"/api/jobs/{other['job_id']}/cancel",
        json={
            "revision": other["revision"],
            "idempotency_key": "other-cancel",
        },
    )
    assert response.status_code == 403
    response = client.post(
        f"/api/jobs/{own['job_id']}/cancel",
        json={
            "revision": own["revision"],
            "idempotency_key": "own-cancel",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["job"]["status"] == "cancelled"
    assert (
        client.post(
            "/api/control/episode-deletion-requests/anything/review",
            json={
                "decision": "approve",
                "reason": "reviewed",
            },
        ).status_code
        == 403
    )


def test_default_all_scope_includes_new_locations_and_can_be_restricted(tmp_path):
    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    assert client.get(endpoint).get_json() == {"all_locations": True, "location_ids": []}
    assert mutate(client, location).status_code == 202
    other = store.sync_locations(node["node_id"], [{"dataset_key": "node/new", "root": "/datasets/new"}])[0]
    assert mutate(client, other).status_code == 202
    assert admin.put(endpoint, json={"all_locations": False, "location_ids": []}).status_code == 200
    assert store.claim_job(node["node_id"]) is None
    assert mutate(client, other).status_code == 403
    assert admin.put(endpoint, json={"all_locations": True, "location_ids": []}).status_code == 200
    assert store.claim_job(node["node_id"]) is not None
    assert client.put(endpoint, json={"all_locations": True, "location_ids": []}).status_code == 403
    assert admin.put(endpoint, json={"all_locations": "false", "location_ids": []}).status_code == 400


def test_existing_grants_stay_restricted_when_scope_table_is_added(tmp_path):
    from sqlalchemy import delete

    from lerobot.data_platform.control_plane import DataMutationScope
    from lerobot.data_platform.management_storage import SchemaMigration

    store, admin, client, user, node, location, token, endpoint = setup_manager(tmp_path)
    admin.put(endpoint, json={"location_ids": [location["location_id"]]})
    DataMutationScope.__table__.drop(store.engine)
    with store.sessions.begin() as session:
        session.execute(delete(SchemaMigration).where(SchemaMigration.version == 4))
    reopened = ControlPlaneStore(str(store.engine.url), initialize_schema=True)
    assert reopened.mutation_scope(user["user_id"]) == {
        "all_locations": False,
        "location_ids": [location["location_id"]],
    }
    other = store.sync_locations(node["node_id"], [{"dataset_key": "node/new", "root": "/datasets/new"}])[0]
    assert other["location_id"] not in reopened.mutation_location_ids(user["user_id"])


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_viewer_operator_dataset_scopes(tmp_path, role):
    store, admin, client, user, node, location, token = setup_requests(tmp_path)
    store.update_user(user["user_id"], role=role)
    endpoint = f"/api/auth/users/{user['user_id']}/data-scopes"
    assert client.get(endpoint).json["all_locations"] is True
    assert len(client.get("/api/control/locations").json["locations"]) == 1
    assert admin.put(endpoint, json={"location_ids": []}).status_code == 200
    assert client.get("/api/control/locations").json["locations"] == []
    assert client.put(endpoint, json={"location_ids": [], "all_locations": True}).status_code == 403
    url = f"/api/control/locations/{location['location_id']}/viewer-jobs"
    assert client.post(url, json={}).status_code == 403
    assert admin.put(endpoint, json={"location_ids": [location["location_id"]]}).status_code == 200
    assert client.get("/api/control/locations").json["locations"][0]["source_mutation_allowed"] is False
    assert client.post(url, json={}).status_code == (202 if role == "operator" else 403)
    if role == "operator":
        assert admin.put(endpoint, json={"location_ids": []}).status_code == 200
        assert store.claim_job(node["node_id"]) is None
        with pytest.raises(PermissionError):
            store.create_job(
                location_id=location["location_id"], requested_by=user["user_id"], operation="viewer.prepare"
            )
    assert admin.put(endpoint, json={"location_ids": [], "all_locations": True}).status_code == 200
    assert len(client.get("/api/control/locations").json["locations"]) == 1


def test_operator_scope_checks_all_merge_sources_and_current_roots(tmp_path):
    store, admin, client, user, node, location, token = setup_requests(tmp_path)
    other = store.sync_locations(node["node_id"], [{"dataset_key": "node/other", "root": "/datasets/other"}])[
        0
    ]
    endpoint = f"/api/auth/users/{user['user_id']}/data-scopes"
    # Default all includes future registrations.
    assert len(client.get("/api/control/locations").json["locations"]) == 2
    assert admin.put(endpoint, json={"location_ids": [location["location_id"]]}).status_code == 200
    options = {"source_location_ids": [location["location_id"], other["location_id"]]}
    response = client.post(
        f"/api/control/locations/{location['location_id']}/preprocess-jobs",
        json={"op": "merge", "options": options},
    )
    assert response.status_code == 403
    with pytest.raises(PermissionError):
        store.create_job(
            location_id=location["location_id"],
            requested_by=user["user_id"],
            operation="preprocess.merge",
            options=options,
        )
    assert admin.put(endpoint, json={"location_ids": options["source_location_ids"]}).status_code == 200
    job = store.create_job(
        location_id=location["location_id"],
        requested_by=user["user_id"],
        operation="preprocess.merge",
        options=options,
    )
    assert admin.put(endpoint, json={"location_ids": [location["location_id"]]}).status_code == 200
    assert store.claim_job(node["node_id"]) is None
    assert client.get(f"/api/control/jobs/{job['job_id']}").status_code == 403
    assert client.get("/api/control/jobs").json["jobs"] == []
    with store.sessions.begin() as session:
        session.get(DatasetLocation, location["location_id"]).root = "/datasets/replaced"
    assert client.get("/api/control/locations").json["locations"] == []

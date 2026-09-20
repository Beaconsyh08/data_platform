"""Exercise the actual Agent upload/completion protocol and central Curation draft API."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lerobot.data_platform.curation import OPERATIONS
from lerobot.data_platform.curation_execution import execute_curation_job
from lerobot.data_platform.execution import SpoolingClient
from lerobot.data_platform.lifecycle import LifecycleStore
from lerobot.data_platform.routes.curation import register_curation_routes
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_lifecycle import _make_dataset, _snapshot


@pytest.fixture(params=["v2.1", "v3.0"])
def remote(tmp_path, request):
    control = _store(tmp_path)
    app = _app(tmp_path, control)
    ledger = LifecycleStore(tmp_path / "lifecycle")
    register_curation_routes(app, SimpleNamespace(control_plane_store=control, lifecycle_store=ledger))
    client = app.test_client()
    _bootstrap(client)
    source = tmp_path / "source"
    _make_dataset(source)
    if request.param == "v3.0":
        from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3

        converted = tmp_path / "v3"
        run_convert_v3(source, converted)
        source = converted
    token, node = control.enroll_node(
        name="curation-node",
        hostname="localhost",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        capabilities={
            "job_protocol": 2,
            "data_profile_protocol": 100,
            "curation_protocol": 1,
            "curation_operations": sorted(OPERATIONS),
        },
        enrollment_token="test",
        expected_token="test",
    )
    location = control.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "node/source",
                "root": str(source),
                "output_dir": str(tmp_path / "cache"),
                "metadata": json.loads((source / "meta/info.json").read_text()),
            }
        ],
    )[0]
    return SimpleNamespace(
        client=client,
        control=control,
        ledger=ledger,
        node=node,
        token=token,
        location=location,
        source=source,
        tmp=tmp_path,
    )


def queue(remote, operation, **extra):
    body = {"target": {"location_id": remote.location["location_id"]}, "operation": operation, **extra}
    response = remote.client.post(
        "/api/curation/jobs", json=body, headers={"Idempotency-Key": str(extra.get("delivery", operation))}
    )
    assert response.status_code == 202, response.get_json()
    repeated = remote.client.post(
        "/api/curation/jobs", json=body, headers={"Idempotency-Key": str(extra.get("delivery", operation))}
    )
    assert repeated.get_json()["job"]["job_id"] == response.get_json()["job"]["job_id"]
    job = remote.control.claim_job(remote.node["node_id"], worker_instance_id="worker")
    assert job is not None
    headers = {
        "Authorization": f"Bearer {remote.token}",
        "X-Job-Attempt": job["execution"]["attempt_id"],
        "X-Job-Credential": job["execution"]["credential"],
    }
    return job, headers


def execute(remote, job, headers):
    from lerobot.data_platform.agent import DataPlatformAgent

    work = remote.tmp / "work" / job["job_id"]
    if job["options"].get("input_files"):
        for name in job["options"]["input_files"]:
            response = remote.client.get(
                f"/api/agents/jobs/{job['job_id']}/curation-inputs/{name}", headers=headers
            )
            assert response.status_code == 200
            path = work / "inputs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.data)
        job["options"]["input_root"] = str(work / "inputs")
    agent = SimpleNamespace(
        client=SpoolingClient("unused", work),
        state=None,
        allowed_roots=[remote.tmp],
        writable_roots=[remote.tmp],
        name="curation-node",
    )
    agent._upload_viewer_artifacts = lambda job, static, derived=False: (
        DataPlatformAgent._upload_viewer_artifacts(agent, job, static, derived=derived)
    )
    location = {**job["location"], "output_dir": str(work / "staging")}
    result = execute_curation_job(agent, job, remote.source, location)
    endpoint = f"/api/agents/jobs/{job['job_id']}"
    for path in (work / "uploads").glob("*.json"):
        upload = json.loads(path.read_text())
        category = "curation-artifacts" if upload.get("curation") else "derived-artifacts"
        response = remote.client.put(
            endpoint + "/" + category + "/" + upload["relative_path"],
            headers=headers,
            data=Path(upload["path"]).read_bytes(),
        )
        assert response.status_code == 200, response.get_json()
    response = remote.client.post(
        endpoint + "/complete", headers=headers, json={"status": "done", "result": result}
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["job"]["status"] == "done"
    return response.get_json()["job"]


def test_snapshot_review_and_publication_round_trip(remote):
    original = _snapshot(remote.source)
    query = {"location_id": remote.location["location_id"]}
    caps = remote.client.get("/api/curation/capabilities", query_string=query).get_json()
    assert caps["operations"]["curation.snapshot"]["can_execute"]
    assert not caps["operations"]["curation.labeling"]["can_execute"]
    job, headers = queue(remote, "curation.snapshot")
    endpoint = f"/api/agents/jobs/{job['job_id']}/curation-artifacts/result.json"
    assert remote.client.put(endpoint, data=b"{}").status_code == 401
    assert (
        remote.client.put(endpoint, data=b"{}", headers={**headers, "X-Job-Credential": "wrong"}).status_code
        == 409
    )
    execute(remote, job, headers)
    context = remote.client.get("/api/curation/context", query_string=query).get_json()
    assert len(context["episodes"]) == 2
    assert context["operations"]["curation.labeling"]["can_execute"]
    response = remote.client.post("/api/curation/drafts", json={"target": query})
    assert response.status_code == 201, response.get_json()
    draft = response.get_json()["workspace"]
    url = f"/api/curation/drafts/{draft['workspace_id']}/episodes/1"
    updated = remote.client.patch(
        url,
        json={
            "expected_revision": draft["revision"],
            "decision": "keep",
            "fields": {"task": "reviewed task"},
        },
    )
    assert updated.status_code == 200, updated.get_json()
    assert (
        remote.client.patch(
            url, json={"expected_revision": draft["revision"], "decision": "exclude"}
        ).status_code
        == 409
    )
    draft = updated.get_json()["workspace"]
    job, headers = queue(
        remote,
        "curation.validate_source",
        workspace_id=draft["workspace_id"],
        expected_revision=draft["revision"],
        publish_workspace=True,
    )
    completed = execute(remote, job, headers)
    manifest = remote.ledger.get_manifest(completed["result"]["manifest_id"])
    assert manifest.status == "published"
    assert manifest.annotation_patches[0]["fields"]["task"] == "reviewed task"
    assert original == _snapshot(remote.source)


def test_viewer_cannot_queue_or_edit(remote):
    remote.control.register_user(username="reader", password="reader-password", display_name="Reader")
    remote.client.post("/api/auth/logout", json={})
    remote.client.post("/api/auth/login", json={"username": "reader", "password": "reader-password"})
    assert remote.client.post("/api/curation/jobs", json={}).status_code == 403
    assert remote.client.post("/api/curation/drafts", json={}).status_code == 403
    caps = remote.client.get(
        "/api/curation/capabilities", query_string={"location_id": remote.location["location_id"]}
    ).get_json()
    assert not caps["can_edit"]


def test_changed_source_rejected_before_operator_runs(remote):
    job, headers = queue(remote, "curation.snapshot")
    execute(remote, job, headers)
    job, headers = queue(remote, "curation.validate_source")
    path = remote.source / "meta/tasks.jsonl"
    path.write_text('{"task_index":0,"task":"changed task"}\n')
    with pytest.raises(ValueError, match="Source changed"):
        execute(remote, job, headers)


def test_remote_materialization_registers_lineage_and_viewer_without_source_changes(remote, monkeypatch):
    from lerobot.data_platform import cli

    def prepare(*, output_dir, root, **kwargs):
        static = Path(output_dir) / "static"
        static.mkdir(parents=True, exist_ok=True)
        (static / "viewer_manifest.json").write_text(json.dumps({"episodes": [{"episode_index": 0}]}))
        return {}

    monkeypatch.setattr(cli, "run_precompute", prepare)
    before = _snapshot(remote.source)
    job, headers = queue(remote, "curation.snapshot")
    execute(remote, job, headers)
    version = remote.ledger.list_versions()[0]
    ref = {"dataset_version_id": version.version_id, "episode_uid": version.uid_by_index()[1]}
    workspace = remote.ledger.create_workspace(
        version.version_id,
        owner="operator",
        decisions=[{"episode_ref": ref, "decision": "keep", "reason": "selected"}],
    )
    manifest = remote.ledger.publish_workspace(
        workspace.workspace_id,
        expected_revision=workspace.revision,
        reviewer="operator",
        source_fingerprint=version.fingerprint,
    )
    output = remote.tmp / "curated"
    job, headers = queue(
        remote,
        "curation.materialize",
        manifest_id=manifest.manifest_id,
        out_root=str(output),
        parameters={"workers": 1},
    )
    finished = execute(remote, job, headers)
    result = finished["result"]
    assert result["viewer_url"].endswith("/episode_0")
    assert remote.control.get_location(result["output_location_id"])["metadata"]["viewer_ready"]
    derived = remote.ledger.get_version(result["dataset_version_id"])
    assert derived.parent_version_ids == [version.version_id]
    assert derived.uid_by_index() == {0: version.uid_by_index()[1]}
    assert remote.ledger.get_materialization(result["materialization_id"]).status == "committed"
    repeated = remote.client.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        headers=headers,
        json={"status": "done", "result": result},
    )
    assert repeated.status_code == 200
    assert before == _snapshot(remote.source)


def test_operator_can_publish_and_late_edits_prevent_publication(remote):
    user = remote.control.register_user(
        username="curator", password="operator-password", display_name="Curator"
    )
    remote.control.update_user(user["user_id"], role="operator")
    remote.client.post("/api/auth/logout", json={})
    remote.client.post("/api/auth/login", json={"username": "curator", "password": "operator-password"})
    job, headers = queue(remote, "curation.snapshot")
    execute(remote, job, headers)
    query = {"location_id": remote.location["location_id"]}
    draft = remote.client.post("/api/curation/drafts", json={"target": query}).get_json()["workspace"]
    assert draft["owner"] == "curator"
    job, headers = queue(
        remote,
        "curation.validate_source",
        workspace_id=draft["workspace_id"],
        expected_revision=draft["revision"],
        publish_workspace=True,
    )
    response = remote.client.patch(
        f"/api/curation/drafts/{draft['workspace_id']}/episodes/0",
        json={"expected_revision": draft["revision"], "decision": "exclude"},
    )
    assert response.status_code == 200
    with pytest.raises(AssertionError, match="revision"):
        execute(remote, job, headers)
    assert remote.ledger.get_workspace(draft["workspace_id"]).published_manifest_id is None


def test_source_refresh_and_clearing_review(remote):
    job, headers = queue(remote, "curation.snapshot")
    execute(remote, job, headers)
    query = {"location_id": remote.location["location_id"]}
    version = remote.ledger.list_versions()[0]
    draft = remote.client.post("/api/curation/drafts", json={"target": query}).get_json()["workspace"]
    url = f"/api/curation/drafts/{draft['workspace_id']}/episodes/0"
    draft = remote.client.patch(
        url, json={"expected_revision": draft["revision"], "fields": {"task": "changed"}}
    ).get_json()["workspace"]
    draft = remote.client.patch(
        url, json={"expected_revision": draft["revision"], "clear_fields": ["task"]}
    ).get_json()["workspace"]
    assert draft["annotation_patches"] == []
    info = remote.source / "meta/info.json"
    value = json.loads(info.read_text())
    value["description"] = "new source revision"
    info.write_text(json.dumps(value))
    job, headers = queue(remote, "curation.snapshot", delivery="refresh-source")
    execute(remote, job, headers)
    fresh = remote.client.get("/api/curation/context", query_string=query).get_json()
    assert fresh["target"]["dataset_version_id"] != version.version_id
    assert remote.ledger.get_workspace(draft["workspace_id"]).base_dataset_version_id == version.version_id


def test_compare_summary_executes_on_node(remote):
    job, headers = queue(remote, "curation.snapshot")
    execute(remote, job, headers)
    job, headers = queue(remote, "curation.compare_summary")
    execute(remote, job, headers)
    response = remote.client.post(
        "/api/curation/compare", json={"run_a": job["job_id"], "run_b": job["job_id"]}
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["summary"]["a"]["metadata"]["total_episodes"] == 2


def test_local_target_uses_same_durable_worker_protocol(tmp_path):
    control = _store(tmp_path)
    app = _app(tmp_path, control)
    app.static_folder = str(tmp_path / "console" / "static")
    ledger = LifecycleStore(tmp_path / "ledger")
    source = tmp_path / "source"
    _make_dataset(source)
    cache = tmp_path / "cache"
    key = "local/source"
    ctx = SimpleNamespace(
        control_plane_store=control,
        lifecycle_store=ledger,
        repo_key=lambda value: value,
        repo_id_from_key=lambda value: value,
        ensure_dataset_loaded=lambda value: (SimpleNamespace(root=source), cache / "static"),
        datasets_index={key: {"root": source, "output_dir": cache}},
    )
    register_curation_routes(app, ctx)
    client = app.test_client()
    _bootstrap(client)
    body = {"target": {"dataset_key": key}, "operation": "curation.snapshot"}
    response = client.post("/api/curation/jobs", json=body, headers={"Idempotency-Key": "local-snapshot"})
    assert response.status_code == 202, response.get_json()
    duplicate = client.post("/api/curation/jobs", json=body, headers={"Idempotency-Key": "local-snapshot"})
    assert duplicate.get_json()["job"]["job_id"] == response.get_json()["job"]["job_id"]
    state = json.loads((tmp_path / "console/management/local-agent.json").read_text())
    job = control.claim_job(state["node_id"], worker_instance_id="local-worker")
    assert job["operation"] == "curation.snapshot"
    headers = {
        "Authorization": f"Bearer {state['node_token']}",
        "X-Job-Attempt": job["execution"]["attempt_id"],
        "X-Job-Credential": job["execution"]["credential"],
    }
    fixture = SimpleNamespace(tmp=tmp_path, source=source, client=client)
    execute(fixture, job, headers)
    context = client.get("/api/curation/context", query_string={"dataset_key": key}).get_json()
    assert context["target"]["dataset_version_id"]
    assert context["target"]["location_id"] is None
    assert len(context["episodes"]) == 2
    assert client.post("/api/curation/drafts", json={"target": context["target"]}).status_code == 201


def test_label_review_input_and_construction_preserve_source(remote, monkeypatch):
    from lerobot.data_platform import cli
    from lerobot.data_platform.precompute import labeling

    def label(root, meta, episodes, static, **kwargs):
        path = Path(static) / f"labeling/labels_{kwargs['output_variant']}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "episode_index": 0,
                    "task": "pick cube",
                    "selected": {"bbox": {"left": 1, "top": 1, "right": 3, "bottom": 3}},
                }
            )
            + "\n"
        )
        return {"processed": 1}

    def prepare(*, output_dir, **kwargs):
        path = Path(output_dir) / "static/viewer_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"episodes": [{"episode_index": 0}]}))
        return {}

    monkeypatch.setattr(labeling, "run_labeling", label)
    monkeypatch.setattr(cli, "run_precompute", prepare)
    remote.control.heartbeat(
        remote.node["node_id"],
        capabilities={
            **remote.node["capabilities"],
            "curation_backends": {"labeling": {"backends": {"grounding_dino": {"available": True}}}},
        },
    )
    before = _snapshot(remote.source)
    job, headers = queue(remote, "curation.snapshot")
    execute(remote, job, headers)
    job, headers = queue(
        remote, "curation.labeling", parameters={"backend": "grounding_dino", "output_variant": "trial"}
    )
    execute(remote, job, headers)
    labels_run = job["job_id"]
    query = {"location_id": remote.location["location_id"]}
    draft = remote.client.post("/api/curation/drafts", json={"target": query}).get_json()["workspace"]
    response = remote.client.post(
        f"/api/curation/drafts/{draft['workspace_id']}/accept/{labels_run}/0",
        json={"expected_revision": draft["revision"]},
    )
    assert response.status_code == 200, response.get_json()
    draft = response.get_json()["workspace"]
    assert draft["annotation_patches"][0]["fields"]["first_frame_bbox"]["selected"]["bbox"]["left"] == 1
    job, headers = queue(
        remote,
        "curation.construction",
        input_runs=[labels_run],
        workspace_id=draft["workspace_id"],
        expected_revision=draft["revision"],
        parameters={"include_positives": True, "per_scenario_counts": {}},
        out_root=str(remote.tmp / "constructed"),
    )
    done = execute(remote, job, headers)
    derived = remote.ledger.get_version(done["result"]["dataset_version_id"])
    assert (
        derived.dataset_format_version
        == json.loads((remote.source / "meta/info.json").read_text())["codebase_version"]
    )
    assert len(derived.episode_refs) == 2
    assert before == _snapshot(remote.source)
    assert remote.ledger.list_reconciliations(dataset_version_id=derived.version_id)


@pytest.mark.parametrize(
    "operation,parameters",
    [
        ("curation.stage", {"prepare_workers": 1}),
        ("curation.quality", {"workers": 1}),
        ("curation.tagging", {"selected_tags": ["arm"], "workers": 1}),
    ],
)
def test_signal_operations_execute_without_mutating_source(remote, operation, parameters):
    if operation == "curation.stage":
        path = remote.source / "meta/info.json"
        info = json.loads(path.read_text())
        info["data_profile"] = {
            "schema_version": 1,
            "robot_profile": "generic",
            "stage_profile": "time_equal_v1",
            "signal_schema": "unknown",
        }
        path.write_text(json.dumps(info))
    before = _snapshot(remote.source)
    job, headers = queue(remote, "curation.snapshot")
    execute(remote, job, headers)
    job, headers = queue(remote, operation, parameters=parameters)
    execute(remote, job, headers)
    response = remote.client.get(f"/api/curation/runs/{job['job_id']}")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["artifacts"]
    assert before == _snapshot(remote.source)
    if operation == "curation.stage":
        draft = remote.client.post(
            "/api/curation/drafts", json={"target": {"location_id": remote.location["location_id"]}}
        ).get_json()["workspace"]
        response = remote.client.post(
            f"/api/curation/drafts/{draft['workspace_id']}/accept/{job['job_id']}/0",
            json={"expected_revision": draft["revision"]},
        )
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["workspace"]["annotation_patches"][0]["fields"]["subtask_transitions"]


@pytest.mark.parametrize("page", ["explore", "quality", "annotation", "dataset_build"])
def test_curation_bookmark_redirects_into_console(remote, page):
    from urllib.parse import parse_qs, urlsplit

    response = remote.client.get(
        "/curation", query_string={"location_id": remote.location["location_id"], "page": page}
    )
    assert response.status_code == 302
    url = urlsplit(response.headers["Location"])
    assert url.path == "/"
    assert parse_qs(url.query) == {"select": [f"remote:{remote.location['location_id']}"], "page": [page]}

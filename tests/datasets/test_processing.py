"""Persistent sequential plans reuse acknowledged jobs and enforce current authorization."""

import copy
import json
from pathlib import Path

import pytest
from flask import Flask, g

from lerobot.data_platform.control_plane import ControlPlaneStore
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.operators.registry import operation_ids, worker_manifest
from lerobot.data_platform.processing.service import PipelineManager
from lerobot.data_platform.routes.processing import register_processing_routes
from tests.datasets.test_quality_eef import make_eef


@pytest.fixture
def setup(tmp_path):
    root = make_eef(tmp_path / "source")
    store = ControlPlaneStore(f"sqlite:///{tmp_path / 'control.db'}")
    actor = store.bootstrap_admin(
        username="admin", password="strong-admin-password", bootstrap_token="test", expected_token="test"
    )
    _, node = store.enroll_node(
        name="node",
        hostname="node",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        enrollment_token="test",
        expected_token="test",
        capabilities={
            **worker_manifest(),
            "operations": sorted(operation_ids(remote=True)),
            "job_protocol": 2,
            "data_profile_protocol": 100,
        },
    )
    location = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "node/source",
                "root": str(root),
                "output_dir": str(tmp_path / "cache"),
                "metadata": json.loads((root / "meta/info.json").read_text()),
            }
        ],
    )[0]
    return store, PipelineManager(store), actor, node, location


def definition(review=False):
    return {
        "name": "Subset twice",
        "steps": [
            {"step_id": "first", "operator_id": "preprocess.split"},
            {
                "step_id": "second",
                "operator_id": "preprocess.split",
                "input": "first",
                "review_before": review,
            },
        ],
    }


def start(setup, review=False):
    store, manager, actor, node, location = setup
    saved = manager.save(actor["user_id"], definition(review))
    run = manager.start(actor["user_id"], saved["pipeline_id"], 1, location["location_id"], "delivery")
    return saved, run


def acknowledge(setup, job_id, output=True):
    store, manager, actor, node, location = setup
    job = store.claim_job(node["node_id"], worker_instance_id="test-worker")
    assert job["job_id"] == job_id
    result = {}
    if output:
        derived = store.sync_locations(
            node["node_id"],
            [
                {
                    "dataset_key": "node/" + job_id,
                    "root": job["options"]["out_root"],
                    "output_dir": job["options"]["out_root"] + "-cache",
                    "metadata": location["metadata"],
                }
            ],
        )[0]
        result["output_location_id"] = derived["location_id"]
    execution = job["execution"]
    store.job_manager.checkpoint(
        job_id, node["node_id"], execution["attempt_id"], execution["credential"], phase="finalizing"
    )
    store.complete_job(
        job_id,
        node_id=node["node_id"],
        status="done",
        result=result,
        attempt_id=execution["attempt_id"],
        credential=execution["credential"],
    )


def test_persistent_coordinator_advances_only_acknowledged_steps(setup):
    store, manager, actor, node, location = setup
    saved, run = start(setup)
    repeated = manager.start(actor["user_id"], saved["pipeline_id"], 1, location["location_id"], "delivery")
    assert repeated["run_id"] == run["run_id"]
    manager.tick(run["run_id"])
    current = manager.run(actor["user_id"], run["run_id"])
    first = current["steps"][0]["job_id"]
    manager.tick(run["run_id"])
    assert len(store.list_jobs()) == 1
    assert current["steps"][1]["job_id"] is None
    acknowledge(setup, first)
    restarted = PipelineManager(store)
    restarted.tick(run["run_id"])
    current = restarted.run(actor["user_id"], run["run_id"])
    second = current["steps"][1]["job_id"]
    assert store.get_job(second)["location_id"] == current["steps"][0]["output_location_id"]
    acknowledge(setup, second)
    restarted.tick(run["run_id"])
    assert restarted.run(actor["user_id"], run["run_id"])["status"] == "done"


def test_review_waits_without_creating_a_compute_job_and_rejects_stale_answer(setup):
    store, manager, actor, node, location = setup
    _, run = start(setup, review=True)
    manager.tick(run["run_id"])
    acknowledge(setup, manager.run(actor["user_id"], run["run_id"])["steps"][0]["job_id"])
    manager.tick(run["run_id"])
    current = manager.run(actor["user_id"], run["run_id"])
    assert current["status"] == "waiting_review"
    assert len(store.list_jobs()) == 1
    body = {"step_id": "second", "decision": "approve", "expected_revision": current["revision"]}
    with pytest.raises(JobConflictError):
        manager.decide(actor["user_id"], run["run_id"], {**body, "expected_revision": -1})
    manager.decide(actor["user_id"], run["run_id"], body)
    manager.tick(run["run_id"])
    assert len(store.list_jobs()) == 2


def test_saved_versions_are_immutable_and_require_revision(setup):
    store, manager, actor, node, location = setup
    saved = manager.save(actor["user_id"], definition())
    with pytest.raises(JobConflictError):
        manager.save(actor["user_id"], definition(), pipeline_id=saved["pipeline_id"], expected_revision=0)
    revised = definition(True)
    manager.save(actor["user_id"], revised, pipeline_id=saved["pipeline_id"], expected_revision=1)
    assert not manager.get(actor["user_id"], saved["pipeline_id"], 1)["definition"]["steps"][1][
        "review_before"
    ]


def test_cancel_queued_pipeline_does_not_create_jobs(setup):
    store, manager, actor, node, location = setup
    _, run = start(setup)
    cancelled = manager.action(
        actor["user_id"], run["run_id"], "cancel", {"expected_revision": run["revision"]}
    )
    assert cancelled["status"] == "cancelled"
    assert not store.list_jobs()


def test_processing_routes_auth_and_viewer_role(setup):
    from types import SimpleNamespace

    store, manager, actor, node, location = setup
    app = Flask(__name__)
    current = {"actor": None}
    app.before_request(lambda: setattr(g, "control_plane_user", current["actor"]))
    register_processing_routes(app, SimpleNamespace(control_plane_store=store, lifecycle_store=None))
    client = app.test_client()
    assert client.get("/api/operators").status_code == 401
    current["actor"] = {**actor, "role": "viewer"}
    assert client.post("/api/pipelines", json={"definition": definition()}).status_code == 403
    assert client.get("/api/operators").status_code == 200
    current["actor"] = actor
    assert client.post("/api/pipelines", json={"definition": definition()}).status_code == 201


def test_changed_input_blocks_execution(setup):
    from lerobot.data_platform.control_plane import DatasetLocation

    store, manager, actor, node, location = setup
    _, run = start(setup)
    with store.sessions.begin() as session:
        row = session.get(DatasetLocation, location["location_id"])
        row.details = {**row.details, "total_frames": 42}
    manager.tick(run["run_id"])
    current = manager.run(actor["user_id"], run["run_id"])
    assert current["status"] == "error"
    assert "input changed" in current["error"]
    assert not store.list_jobs()


@pytest.mark.parametrize("publication", ["direct", "supervised", "legacy-staged"])
def test_preprocess_output_registers_version_and_preserves_episode_identity(
    setup, tmp_path, monkeypatch, publication
):
    import shutil
    from types import SimpleNamespace

    from lerobot.data_platform.curation import register_snapshot, snapshot_from_version
    from lerobot.data_platform.lifecycle import LifecycleStore
    from tests.datasets.test_data_platform_agent import _agent

    store, _, actor, node, location = setup
    ledger = LifecycleStore(tmp_path / "lifecycle")
    source = tmp_path / "source"
    base = ledger.ingest(source, location["dataset_key"], stage="raw")
    register_snapshot(ledger, snapshot_from_version(ledger, base, source), location)
    manager = PipelineManager(store, ledger)
    saved = manager.save(
        actor["user_id"],
        {
            "name": "Subset",
            "steps": [
                {
                    "step_id": "subset",
                    "operator_id": "preprocess.split",
                    "parameters": {"episode_range": "0:1"},
                }
            ],
        },
    )
    run = manager.start(actor["user_id"], saved["pipeline_id"], 1, location["location_id"], "versioned")
    manager.tick(run["run_id"])
    job = store.claim_job(node["node_id"], worker_instance_id="test-worker")
    agent = _agent(tmp_path, source, tmp_path)
    attempt = job["execution"]["attempt_id"]

    def upload(_state, _job, name, path, **kwargs):
        assert kwargs["curation"]
        destination = ledger.root / "curation/.jobs" / job["job_id"] / attempt / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)

    final = Path(job["options"]["out_root"])
    computed_job = copy.deepcopy(job)
    if publication != "direct":
        from lerobot.data_platform.execution import SpoolingClient

        work = tmp_path / "supervisor" / attempt
        staging = final.parent / f".dp-{job['job_id']}-{attempt}"
        staging.mkdir()
        computed_job["options"]["out_root"] = str(staging / final.name)
        agent.client = SpoolingClient("http://127.0.0.1:1", work)
    else:
        agent.client.upload_artifact = upload
    from lerobot.data_platform import agent as agent_module
    from lerobot.data_platform.operators import dataset_versions

    with monkeypatch.context() as failure:
        failure.setattr(
            dataset_versions,
            "hydrate_builder",
            lambda *args: (_ for _ in ()).throw(RuntimeError("metadata failure")),
        )
        with pytest.raises(RuntimeError, match="metadata failure"):
            agent.execute_job(computed_job)
    with monkeypatch.context() as retry:
        retry.setattr(
            agent_module,
            "run_preprocess_op",
            lambda *args, **kwargs: pytest.fail("Completed computation must not rerun"),
        )
        result = agent.execute_job(computed_job)
    if publication != "direct":
        from lerobot.data_platform import execution as execution_module
        from lerobot.data_platform.curation import validate_bundle
        from tests.datasets.test_execution_supervisor import Client, fake_agent

        supervisor = execution_module.ExecutionSupervisor(fake_agent(tmp_path, Client()))
        marker = {"job": job, "work": str(work), "staging": str(staging), "final": str(final)}
        if publication == "legacy-staged":
            # A retained old executor can finish with the earlier staging-root bundle.
            with monkeypatch.context() as legacy:
                legacy.setattr(execution_module, "rebase_curation_bundle", lambda *args: None)
                result = supervisor.publish(marker, {"status": "done", "result": result})["result"]
        else:
            result = supervisor.publish(marker, {"status": "done", "result": result})["result"]
        bundle = work / "curation-artifacts"
        validate_bundle(bundle, job["operation"], job["options"]["target"])
        if publication == "supervised":
            assert json.loads((bundle / "snapshot.json").read_text())["version"]["root"] == str(final)
            assert json.loads((bundle / "result.json").read_text())["dataset_location"]["root"] == str(final)
        for record in (work / "uploads").glob("*.json"):
            descriptor = json.loads(record.read_text())
            upload(None, job["job_id"], descriptor["relative_path"], Path(descriptor["path"]), curation=True)
    app = Flask(__name__)
    register_processing_routes(app, SimpleNamespace(control_plane_store=store, lifecycle_store=ledger))
    with app.test_request_context(headers={"X-Job-Attempt": attempt}):
        if publication == "legacy-staged":
            with pytest.raises(ValueError, match="does not confirm its planned output"):
                app.extensions["data_platform_complete_operator"](
                    job,
                    {
                        **result,
                        "dataset_location": {**result["dataset_location"], "root": str(tmp_path / "other")},
                    },
                )
        result = app.extensions["data_platform_complete_operator"](job, result)
    execution = job["execution"]
    store.job_manager.checkpoint(
        job["job_id"], node["node_id"], attempt, execution["credential"], phase="finalizing"
    )
    store.complete_job(
        job["job_id"],
        node_id=node["node_id"],
        status="done",
        result=result,
        attempt_id=attempt,
        credential=execution["credential"],
    )
    manager.tick(run["run_id"])
    completed = manager.run(actor["user_id"], run["run_id"])
    assert completed["status"] == "done"
    head = ledger._get_record("curation_locations", completed["steps"][0]["output_location_id"])
    derived = ledger.get_version(head["dataset_version_id"])
    assert derived.parent_version_ids == [base.version_id]
    assert derived.uid_by_index()[0] == base.uid_by_index()[0]


def test_cancel_running_waits_for_executor_exit_and_survives_coordinator_restart(setup, monkeypatch):
    store, manager, actor, node, location = setup
    _, run = start(setup)
    manager.tick(run["run_id"])
    job = store.claim_job(node["node_id"], worker_instance_id="worker")
    current = manager.run(actor["user_id"], run["run_id"])
    command = store.job_manager.command
    monkeypatch.setattr(
        store.job_manager, "command", lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
    )
    with pytest.raises(OSError):
        manager.action(actor["user_id"], run["run_id"], "cancel", {"expected_revision": current["revision"]})
    monkeypatch.setattr(store.job_manager, "command", command)
    restarted = PipelineManager(store)
    restarted.tick(run["run_id"])
    assert store.get_job(job["job_id"])["status"] == "cancel_requested"
    assert restarted.run(actor["user_id"], run["run_id"])["status"] == "cancel_requested"
    store.complete_job(
        job["job_id"],
        node_id=node["node_id"],
        status="cancelled",
        attempt_id=job["execution"]["attempt_id"],
        credential=job["execution"]["credential"],
    )
    restarted.tick(run["run_id"])
    assert restarted.run(actor["user_id"], run["run_id"])["status"] == "cancelled"
    assert len(store.list_jobs()) == 1


def test_retry_keeps_acknowledged_output_and_job_identity(setup):
    store, manager, actor, node, location = setup
    _, run = start(setup)
    manager.tick(run["run_id"])
    first = manager.run(actor["user_id"], run["run_id"])["steps"][0]["job_id"]
    acknowledge(setup, first)
    manager.tick(run["run_id"])
    current = manager.run(actor["user_id"], run["run_id"])
    output = current["steps"][0]["output_location_id"]
    job = store.claim_job(node["node_id"], worker_instance_id="worker")
    store.complete_job(
        job["job_id"],
        node_id=node["node_id"],
        status="error",
        error="injected",
        attempt_id=job["execution"]["attempt_id"],
        credential=job["execution"]["credential"],
    )
    manager.tick(run["run_id"])
    current = manager.run(actor["user_id"], run["run_id"])
    retried = manager.action(
        actor["user_id"], run["run_id"], "retry", {"expected_revision": current["revision"]}
    )
    assert retried["steps"][0]["output_location_id"] == output
    assert retried["steps"][1]["job_id"] == job["job_id"]
    acknowledge(setup, job["job_id"])
    manager.tick(run["run_id"])
    assert manager.run(actor["user_id"], run["run_id"])["status"] == "done"


def test_maintenance_pauses_coordination_and_concurrent_ticks_submit_once(setup):
    from concurrent.futures import ThreadPoolExecutor

    from lerobot.data_platform.maintenance import set_maintenance

    store, manager, actor, node, location = setup
    _, run = start(setup)
    set_maintenance(store, True)
    manager.tick(run["run_id"])
    assert not store.list_jobs()
    assert manager.run(actor["user_id"], run["run_id"])["status"] == "queued"
    set_maintenance(store, False)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: PipelineManager(store).tick(run["run_id"]), range(4)))
    assert len(store.list_jobs()) == 1


def test_pipeline_tables_compile_for_mysql():
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    from lerobot.data_platform.processing.models import (
        Pipeline,
        PipelineRun,
        PipelineStepRun,
        PipelineVersion,
    )

    for model in (Pipeline, PipelineVersion, PipelineRun, PipelineStepRun):
        assert "CREATE TABLE" in str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))


def test_run_delivery_retry_does_not_repreview_changed_source(setup):
    from lerobot.data_platform.control_plane import DatasetLocation

    store, manager, actor, node, location = setup
    saved, run = start(setup)
    with store.sessions.begin() as session:
        row = session.get(DatasetLocation, location["location_id"])
        row.details = {**row.details, "total_frames": 999}
    repeated = manager.start(actor["user_id"], saved["pipeline_id"], 1, location["location_id"], "delivery")
    assert repeated["run_id"] == run["run_id"]
    manager.tick(run["run_id"])
    assert manager.run(actor["user_id"], run["run_id"])["status"] == "error"


def test_legacy_local_metadata_stub_uses_snapshot_metadata(setup, tmp_path):
    from lerobot.data_platform.control_plane import ControlPlaneNode, DatasetLocation
    from lerobot.data_platform.curation import register_snapshot, snapshot_from_version
    from lerobot.data_platform.lifecycle import LifecycleStore
    from lerobot.data_platform.processing.service import location_identity

    store, _, actor, node, location = setup
    with store.sessions.begin() as session:
        session.get(DatasetLocation, location["location_id"]).details = {"local_execution": True}
        row = session.get(ControlPlaneNode, node["node_id"])
        row.capabilities = {**row.capabilities, "quality_checks": ["s3"]}
    location = store.get_location(location["location_id"])
    identity = location_identity(location)
    ledger = LifecycleStore(tmp_path / "legacy-ledger")
    manager = PipelineManager(store, ledger)
    plan = {
        "name": "Sync and inspect",
        "steps": [
            {"step_id": "snapshot", "operator_id": "curation.snapshot"},
            {"step_id": "quality", "operator_id": "curation.quality", "parameters": {"checks": ["s3"]}},
        ],
    }
    assert manager.preview(actor["user_id"], location["location_id"], plan)["can_execute"]
    version = ledger.ingest(tmp_path / "source", location["dataset_key"])
    register_snapshot(ledger, snapshot_from_version(ledger, version, tmp_path / "source"), location)
    assert manager._info(location)["features"]
    assert location_identity(store.get_location(location["location_id"])) == identity
    assert manager.preview(actor["user_id"], location["location_id"], plan)["can_execute"]


def test_coordinator_opens_only_shared_durable_state(setup, tmp_path, monkeypatch):
    from lerobot.data_platform.processing.coordinator import configured_manager

    store, _, actor, node, location = setup
    monkeypatch.setenv("DATA_PLATFORM_DATABASE_URL", str(store.engine.url))
    monkeypatch.setenv("DATA_PLATFORM_OUTPUT_DIR", str(tmp_path / "console"))
    monkeypatch.setattr("lerobot.data_platform.environment.check_server_environment", lambda: None)
    manager = configured_manager()
    assert manager.lifecycle.root == tmp_path / "console/static/lifecycle"
    assert manager.store.get_location(location["location_id"])["root"] == location["root"]


def test_pose_review_binds_existing_curation_username_owner_and_revision(setup, tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace

    from lerobot.data_platform.curation import register_snapshot, snapshot_from_version
    from lerobot.data_platform.lifecycle import EpisodeRef, LifecycleStore
    from lerobot.data_platform.processing.service import location_identity
    from tests.datasets.test_pose_interpolate import repairs

    store, _, actor, node, location = setup
    ledger = LifecycleStore(tmp_path / "review-ledger")
    base = ledger.ingest(tmp_path / "source", location["dataset_key"])
    register_snapshot(ledger, snapshot_from_version(ledger, base, tmp_path / "source"), location)
    workspace = ledger.create_workspace(
        base.version_id,
        owner=actor["username"],
        repair_recipes=[
            {
                "op": "pose_interpolate",
                "episode_refs": [asdict(EpisodeRef(base.version_id, base.uid_by_index()[0]))],
                "params": {"repairs": repairs()},
            }
        ],
    )
    manager = PipelineManager(store, ledger)
    step = SimpleNamespace(parameters={"repairs": repairs()})
    evidence = [SimpleNamespace(input_identity=location_identity(location))]
    answer = {"workspace_id": workspace.workspace_id, "workspace_revision": workspace.revision}
    assert manager._review_workspace(step, actor["user_id"], location, evidence, answer) == answer
    with pytest.raises(JobConflictError):
        manager._review_workspace(
            step,
            actor["user_id"],
            location,
            evidence,
            {**answer, "workspace_revision": workspace.revision + 1},
        )


def test_preview_rejects_remote_derived_outputs_outside_configured_read_roots(setup):
    from lerobot.data_platform.control_plane import ControlPlaneNode

    store, manager, actor, node, location = setup
    with store.sessions.begin() as session:
        session.get(ControlPlaneNode, node["node_id"]).allowed_roots = [location["root"]]
    preview = manager.preview(actor["user_id"], location["location_id"], definition())
    assert preview["can_execute"] is False
    assert any("configured read roots" in item["reason"] for item in preview["blocked"])
    assert store.list_jobs() == []


def test_preprocess_execution_uses_selected_replica_without_changing_logical_version(setup, tmp_path):
    import shutil

    from lerobot.data_platform.curation import register_snapshot, snapshot_from_version
    from lerobot.data_platform.lifecycle import LifecycleStore
    from tests.datasets.test_data_platform_agent import _agent

    store, _, actor, node, original = setup
    ledger = LifecycleStore(tmp_path / "replica-ledger")
    source = Path(original["root"])
    version = ledger.ingest(source, original["dataset_key"], stage="raw")
    canonical = snapshot_from_version(ledger, version, source)
    register_snapshot(ledger, canonical, original)
    replica = tmp_path / "replica"
    shutil.copytree(source, replica)
    location = store.sync_locations(
        node["node_id"],
        [
            {
                **original,
                "root": str(replica),
                "dataset_key": "node/replica",
                "output_dir": str(tmp_path / "replica-cache"),
            }
        ],
    )[0]
    register_snapshot(ledger, canonical, location)
    manager = PipelineManager(store, ledger)
    saved = manager.save(
        actor["user_id"],
        {
            "name": "Use selected replica",
            "steps": [
                {
                    "step_id": "split",
                    "operator_id": "preprocess.split",
                    "parameters": {"episode_range": "0:1"},
                }
            ],
        },
    )
    run = manager.start(actor["user_id"], saved["pipeline_id"], 1, location["location_id"], "replica")
    manager.tick(run["run_id"])
    job = store.claim_job(node["node_id"], worker_instance_id="replica-worker")
    snapshot = job["options"]["version_inputs"][0]
    assert snapshot["version"]["root"] == str(replica)
    assert snapshot["version"]["version_id"] == version.version_id
    assert ledger.curation_snapshot(version.version_id)["version"]["root"] == str(source)
    agent = _agent(tmp_path, tmp_path, tmp_path)
    agent.client.upload_artifact = lambda *args, **kwargs: None
    result = agent.execute_job(job)
    assert result["dataset_version_id"]
    assert Path(result["dataset_location"]["root"]).is_dir()


def test_pipeline_repair_review_preserves_curation_scope_guard(setup, monkeypatch):
    store, manager, actor, node, location = setup
    monkeypatch.setattr(store, "restricted_dataset_keys", lambda owner: {location["dataset_key"]})
    with pytest.raises(PermissionError, match="unrestricted"):
        manager._review_workspace(None, actor["user_id"], location, [], {})


def test_deleted_source_keeps_pipeline_history_and_cancel_but_blocks_new_work(setup):
    from lerobot.data_platform.control_plane import DatasetLocation

    store, manager, actor, node, location = setup
    _, run = start(setup)
    with store.sessions.begin() as session:
        session.get(DatasetLocation, location["location_id"]).state = "deleted"
    assert manager.run(actor["user_id"], run["run_id"])["run_id"] == run["run_id"]
    with pytest.raises(JobConflictError, match="deleted"):
        manager.preview(actor["user_id"], location["location_id"], definition())
    cancelled = manager.action(
        actor["user_id"], run["run_id"], "cancel", {"expected_revision": run["revision"]}
    )
    assert cancelled["status"] == "cancelled"

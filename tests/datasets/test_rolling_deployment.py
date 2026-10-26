"""Rolling upgrades preserve old attempts while new executors share the same node limits."""

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lerobot.data_platform.rolling_deployment import (
    ExecutorGeneration,
    compatible_contracts,
    execution_belongs_here,
    release_in_use,
    rolling_ready,
)
from tests.datasets import test_job_management


@pytest.fixture
def setup(tmp_path):
    return test_job_management.setup.__wrapped__(tmp_path)


def contract(**changes):
    return {"rolling_deploy_protocol": 1, "schema_min": 5, "schema_max": 5, "job_protocol": 2, **changes}


@pytest.mark.parametrize("changes", [{"rolling_deploy_protocol": 2}, {"schema_max": 6}, {"job_protocol": 3}])
def test_live_upgrade_rejects_incompatible_contracts(changes):
    assert not compatible_contracts(contract(), contract(**changes), schema=5)


def test_bootstrap_requires_idle_and_live_upgrade_requires_unchanged_schema():
    assert compatible_contracts(contract(), contract(), schema=5)
    assert not compatible_contracts({}, contract(), schema=5)
    assert not compatible_contracts(contract(), contract(), schema=4)


def test_generation_keeps_immutable_release_until_drain_completes(tmp_path, monkeypatch):
    previous, current = tmp_path / "old", tmp_path / "new"
    previous.mkdir()
    current.mkdir()
    (previous / ".executor.lock").touch()
    pointer = tmp_path / "current"
    pointer.symlink_to(previous)
    monkeypatch.setenv("DATA_PLATFORM_EXECUTOR_RELEASE", str(previous))
    monkeypatch.setenv("DATA_PLATFORM_EXECUTOR_CURRENT", str(pointer))
    with ExecutorGeneration.from_env() as generation:
        assert not generation.draining
        assert release_in_use(previous)
        pointer.unlink()
        pointer.symlink_to(current)
        assert generation.draining
    assert not release_in_use(previous)


def test_generation_only_reconciles_its_own_attempts(monkeypatch):
    monkeypatch.setenv("DATA_PLATFORM_EXECUTOR_RELEASE", "/installed/releases/new")
    monkeypatch.setenv("DATA_PLATFORM_RELEASE", "new")
    assert execution_belongs_here({"execution": {"release": "new"}})
    assert not execution_belongs_here({"execution": {"release": "old"}})
    # Historical markers can only be adopted during the idle bootstrap.
    assert execution_belongs_here({"execution": {}})


def test_same_release_supervision_lock_is_exclusive_and_recoverable(tmp_path):
    from lerobot.data_platform.execution import ExecutionSupervisor
    from tests.datasets.test_execution_supervisor import Client, fake_agent

    first = ExecutionSupervisor(fake_agent(tmp_path, Client()))
    second = ExecutionSupervisor(fake_agent(tmp_path, Client()))
    lock = first.acquire_supervision("attempt")
    assert lock is not None
    try:
        assert second.acquire_supervision("attempt") is None
    finally:
        lock.close()
    recovered = second.acquire_supervision("attempt")
    assert recovered is not None
    recovered.close()


def test_pruning_preserves_all_running_executor_releases(tmp_path, monkeypatch):
    from lerobot.data_platform.deployment import Deployment
    from lerobot.data_platform.releases import prune_deployment_history

    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path))
    parent = tmp_path / "releases"
    for number in range(1, 6):
        root = parent / f"V0.0.6.{number}"
        root.mkdir(parents=True)
        (root / "manifest.sha256").write_text("verified")
        (root / ".executor.lock").touch()
    (tmp_path / "current").symlink_to(parent / "V0.0.6.5")
    with (parent / "V0.0.6.1" / ".executor.lock").open("rb") as running:
        fcntl.flock(running, fcntl.LOCK_SH)
        removed = prune_deployment_history(Deployment("dev"))
        assert (parent / "V0.0.6.1").is_dir()
        assert str(parent / "V0.0.6.2") in removed


@pytest.mark.parametrize("release", [None, "old", "new"])
def test_old_generation_cannot_claim_new_tasks_but_can_finish_its_attempt(setup, monkeypatch, release):
    from lerobot.data_platform.control_plane import ControlPlaneNode

    store, _, owner, node, _, create = setup
    old_caps = {**node["capabilities"], "release": "old", "rolling_deploy_protocol": 1}
    monkeypatch.setenv("DATA_PLATFORM_RELEASE", "old")
    store.heartbeat(node["node_id"], capabilities=old_caps)
    old_job = create()
    old = store.claim_job(node["node_id"], worker_instance_id="old-worker", release="old")
    assert old["execution"]["release"] == "old"
    monkeypatch.setenv("DATA_PLATFORM_RELEASE", "new")
    new_caps = {**old_caps, "release": "new"}
    store.heartbeat(node["node_id"], capabilities=new_caps)
    store.heartbeat(node["node_id"], capabilities=old_caps)
    with store.sessions() as session:
        assert session.get(ControlPlaneNode, node["node_id"]).capabilities["release"] == "new"
    execution = old["execution"]
    credentials = {"attempt_id": execution["attempt_id"], "credential": execution["credential"]}
    assert store.job_manager.heartbeat(old_job["job_id"], node["node_id"], **credentials)["renewed"]
    store.job_manager.checkpoint(old_job["job_id"], node["node_id"], **credentials, phase="finalizing")
    store.complete_job(old_job["job_id"], node_id=node["node_id"], status="done", **credentials)
    create(owner)
    claimed = store.claim_job(node["node_id"], worker_instance_id="next-worker", release=release)
    assert bool(claimed) == (release == "new")


def test_new_release_does_not_reconcile_old_live_markers(tmp_path, monkeypatch):
    from lerobot.data_platform.execution import ExecutionSupervisor, atomic_json
    from tests.datasets.test_execution_supervisor import Client, fake_agent

    monkeypatch.setenv("DATA_PLATFORM_EXECUTOR_RELEASE", "/releases/new")
    monkeypatch.setenv("DATA_PLATFORM_RELEASE", "new")
    client = Client()
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, client))
    job = {"job_id": "old-job", "execution": {"attempt_id": "old-attempt", "release": "old"}}
    work = supervisor.root / "old-attempt"
    atomic_json(work / "marker.json", {"job": job, "acknowledged": False})
    atomic_json(work / "reservation.json", {"job": job, "job_id": job["job_id"]})
    monkeypatch.setattr(supervisor, "monitor", lambda *_: pytest.fail("Old attempt cannot be adopted"))
    assert not supervisor.reconcile()
    assert not supervisor.pending_here()
    assert (work / "reservation.json").exists()
    assert not client.completions


def test_node_slots_are_shared_by_old_and_new_generations(setup, monkeypatch):
    store, _, owner, node, _, _ = setup
    store.job_manager.node_limit = store.job_manager.user_limit = 4
    caps = {**node["capabilities"], "release": "old", "rolling_deploy_protocol": 1, "job_concurrent_limit": 4}
    monkeypatch.setenv("DATA_PLATFORM_RELEASE", "old")
    store.heartbeat(node["node_id"], capabilities=caps)
    locations = store.sync_locations(
        node["node_id"],
        [
            {"dataset_key": f"source-{i}", "root": f"/data/source-{i}", "output_dir": f"/data/cache-{i}"}
            for i in range(5)
        ],
    )
    for location in locations:
        store.create_job(
            location_id=location["location_id"], requested_by=owner["user_id"], operation="viewer.prepare"
        )
    first = [store.claim_job(node["node_id"], release="old", worker_instance_id=f"old-{i}") for i in range(2)]
    monkeypatch.setenv("DATA_PLATFORM_RELEASE", "new")
    store.heartbeat(node["node_id"], capabilities={**caps, "release": "new"})
    second = [
        store.claim_job(node["node_id"], release="new", worker_instance_id=f"new-{i}") for i in range(2)
    ]
    assert all(first + second)
    assert {job["execution"]["release"] for job in first + second} == {"old", "new"}
    assert store.claim_job(node["node_id"], release="new", worker_instance_id="fifth") is None


def test_persistent_pending_attempt_protects_release_during_executor_restart(tmp_path, monkeypatch):
    from lerobot.data_platform.deployment import Deployment
    from lerobot.data_platform.execution import atomic_json
    from lerobot.data_platform.releases import prune_deployment_history

    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / "installed"))
    monkeypatch.setenv("DATA_PLATFORM_OUTPUT_DIR", str(tmp_path / "output"))
    parent = Deployment("dev").root / "releases"
    for number in range(1, 6):
        root = parent / f"V0.0.6.{number}"
        root.mkdir(parents=True)
        (root / "manifest.sha256").touch()
    (Deployment("dev").root / "current").symlink_to(parent / "V0.0.6.5")
    atomic_json(
        tmp_path / "output/management/executions/attempt/marker.json",
        {
            "acknowledged": False,
            "job": {"execution": {"release": "V0.0.6.1"}},
        },
    )
    prune_deployment_history(Deployment("dev"))
    assert (parent / "V0.0.6.1").exists()
    assert not (parent / "V0.0.6.2").exists()


@pytest.mark.parametrize("bad", [None, "stale", "capability", "contract", "retained-contract", "interrupted"])
def test_rolling_readiness_checks_live_agents_and_every_retained_generation(
    tmp_path, monkeypatch, setup, bad
):
    from lerobot.data_platform.control_plane import RemoteJob
    from lerobot.data_platform.deployment import Deployment
    from lerobot.data_platform.execution import atomic_json

    store, _, _, _, _, create = setup
    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path))
    monkeypatch.setenv("DATA_PLATFORM_INSTANCE_ID", "instance")
    previous = tmp_path / "releases" / "old"
    previous.mkdir(parents=True)
    (tmp_path / "current").symlink_to(previous)
    (previous / ".executor.lock").touch()
    atomic_json(
        previous / "deployment-contract.json", contract(job_protocol=3) if bad == "contract" else contract()
    )
    retained = tmp_path / "releases" / "older"
    retained.mkdir()
    (retained / ".executor.lock").touch()
    atomic_json(
        retained / "deployment-contract.json",
        contract(schema_max=6) if bad == "retained-contract" else contract(),
    )
    node = {
        "name": "remote",
        "last_seen_at": (
            datetime.now(timezone.utc) - timedelta(seconds=120 if bad == "stale" else 0)
        ).isoformat(),
        "capabilities": {
            "environment": "dev",
            "instance_id": "instance",
            "release": "old",
            "rolling_deploy_protocol": 0 if bad == "capability" else 1,
        },
    }
    monkeypatch.setattr(store, "list_nodes", lambda: [node])
    if bad == "interrupted":
        job = create()
        with store.sessions.begin() as session:
            session.get(RemoteJob, job["job_id"]).status = "interrupted"
    with (previous / ".executor.lock").open("rb") as old, (retained / ".executor.lock").open("rb") as older:
        fcntl.flock(old, fcntl.LOCK_SH)
        fcntl.flock(older, fcntl.LOCK_SH)
        assert rolling_ready(store, Deployment("dev"), contract(), schema=5, required_names=["remote"]) == (
            bad is None
        )


@pytest.mark.parametrize("failure", [False, True])
def test_running_compute_survives_live_server_upgrade_and_health_failure(
    tmp_path, monkeypatch, setup, failure
):
    from lerobot.data_platform import deployment_backup, environment, maintenance, release_cli, releases
    from lerobot.data_platform.deployment import AgentTarget, Deployment
    from lerobot.data_platform.execution import atomic_json

    store, _, _, _, _, _ = setup
    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / "installed"))
    monkeypatch.setattr(Deployment, "maintenance_file", property(lambda self: tmp_path / "maintenance"))
    monkeypatch.setattr(release_cli, "load_environment", lambda _: {})
    monkeypatch.setattr(environment, "check_server_environment", lambda: None)
    monkeypatch.setattr(release_cli, "_store", lambda: store)
    monkeypatch.setattr(release_cli, "agent_targets", lambda: [AgentTarget("host", "key", ("remote",))])
    monkeypatch.setattr(release_cli, "agent_preflight", lambda _: None)
    monkeypatch.setattr(releases, "verify_release", lambda *_args, **_kwargs: (tmp_path, contract()))
    monkeypatch.setattr(
        "lerobot.data_platform.rolling_deployment.rolling_ready", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        maintenance, "wait_until_idle", lambda *_: pytest.fail("Live upgrade must not wait for compute")
    )
    deployment = Deployment("dev")
    previous = deployment.root / "releases" / "old"
    previous.mkdir(parents=True)
    (deployment.root / "current").symlink_to(previous)
    candidate = deployment.root / "releases" / "new"
    candidate.mkdir()
    monkeypatch.setattr(releases, "prepare_server", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(releases, "install_server_units", lambda *_: None)
    monkeypatch.setattr(releases, "prune_deployment_history", lambda *_: [])
    calls = []
    monkeypatch.setattr(releases, "run", lambda args, **_: calls.append(list(map(str, args))))
    monkeypatch.setattr(
        deployment_backup,
        "backup",
        lambda _, root, **kwargs: atomic_json(root / "backup.json", {"complete": True, **kwargs}),
    )

    def healthy(*_):
        if failure:
            raise RuntimeError("Health check failed")

    monkeypatch.setattr(releases, "wait_healthy", healthy)
    monkeypatch.setattr(releases, "check_nodes", lambda *_args, **_kwargs: None)

    def install(*_args, **kwargs):
        assert kwargs["rolling"] is True
        return None

    monkeypatch.setattr(release_cli, "install_agent", install)
    compute = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        if failure:
            with pytest.raises(RuntimeError, match="Health check failed"):
                release_cli.deploy(deployment, "new")
        else:
            release_cli.deploy(deployment, "new")
        assert compute.poll() is None
        assert ["systemctl", "stop", "data-platform-web@dev.service"] in calls
        assert not any("local-worker@dev.service" in call for call in calls)
        assert not any("init" in call for call in calls)
        state = json.loads((deployment.root / "deployment.json").read_text())
        assert state["rolling"] is True
        assert json.loads((Path(state["backup"]) / "backup.json").read_text())["quiescent"] is False
        with store.sessions() as session:
            assert maintenance.is_maintenance(session) == failure
    finally:
        compute.terminate()
        compute.wait(timeout=5)


def test_rolling_backup_cannot_restore_mid_attempt_state(tmp_path, monkeypatch):
    from lerobot.data_platform.deployment import Deployment
    from lerobot.data_platform.deployment_backup import validate_backup

    monkeypatch.setenv("DATA_PLATFORM_INSTANCE_ID", "instance")
    (tmp_path / "backup.json").write_text(
        json.dumps(
            {
                "complete": True,
                "environment": "dev",
                "instance_id": os.environ.get("DATA_PLATFORM_INSTANCE_ID"),
                "quiescent": False,
            }
        )
    )
    with pytest.raises(RuntimeError, match="not an idle restore backup"):
        validate_backup(Deployment("dev"), tmp_path)

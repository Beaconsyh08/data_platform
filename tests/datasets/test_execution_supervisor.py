"""Real process tests for cancellation and durable artifact publication."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lerobot.data_platform.execution import (
    ExecutionSupervisor,
    atomic_json,
    group_members,
    input_fingerprint,
    process_identity,
)
from lerobot.data_platform.job_concurrency import footprint
from tests.datasets.test_preprocess_ops import _add_v21_stat_counts, _make_dataset


class Client:
    server_url = "http://127.0.0.1:1"

    def __init__(self, mode=None):
        self.mode = mode
        self.completions = []
        self.events = []
        self.checkpoints = []

    def activate(self, job):
        pass

    def checkpoint(self, state, job_id, phase, fingerprint=None):
        self.checkpoints.append((phase, fingerprint))

    def control_heartbeat(self, state, job_id, lease_seconds):
        return {"renewed": True, "stop_mode": self.mode}

    def event(self, state, job_id, message, payload=None):
        self.events.append(message)

    def upload_artifact(self, *args, **kwargs):
        pass

    def complete(self, state, job_id, **payload):
        self.completions.append(payload)


def fake_agent(tmp_path, client):
    state_path = tmp_path / "state" / "agent.json"
    atomic_json(
        state_path,
        {
            "node_id": "node",
            "node_token": "test-token",
            "name": "local-test",
            "server_url": client.server_url,
        },
    )
    return SimpleNamespace(
        state_path=state_path,
        client=client,
        state={},
        name="local-test",
        allowed_roots=[tmp_path],
        writable_roots=[tmp_path],
        allow_source_mutations=False,
        lease_seconds=60,
    )


@pytest.mark.parametrize("operation", ["import.mcap.inspect", "import.mcap.convert"])
def test_raw_mcap_execution_does_not_require_viewer_output_dir(tmp_path, monkeypatch, operation):
    source = tmp_path / "raw" / "00001"
    source.mkdir(parents=True)
    (source / "sample.mcap").write_bytes(b"sample")
    final = tmp_path / "converted" / "00001" if operation.endswith("convert") else None
    job = {
        "job_id": "mcap-job",
        "operation": operation,
        "location": {"root": str(source), "output_dir": "", "dataset_key": "raw/00001"},
        "options": {},
        "execution": {"attempt_id": "attempt", "final_output": str(final) if final else None},
    }
    paths = footprint(
        operation, str(source), "", {}, job["job_id"], final_output=str(final) if final else None
    )
    assert paths.sources == (str(source),)
    assert paths.outputs == ((str(final),) if final else ())
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, Client()))

    def reserved(_job, _work, _roots, output_dir, requested_final):
        assert output_dir == ""
        assert requested_final == final
        raise RuntimeError("reached reservation")

    monkeypatch.setattr(supervisor, "_reserve", reserved)
    with pytest.raises(RuntimeError, match="reached reservation"):
        supervisor.start(job)


def test_completed_viewer_cache_retry_replays_uploads_without_compute(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "meta.json").write_text("stable")
    target = tmp_path / "vis" / "local_vis_source"
    static = target / "static"
    static.mkdir(parents=True)
    atomic_json(
        static / "viewer_manifest.json",
        {"root": str(source), "repo_id": "node/source", "task_config": {"digest": "task-digest"}},
    )
    (static / "video.bin").write_bytes(b"existing cached video")
    client = Client()
    uploaded = []
    client.upload_artifact = lambda _state, _job_id, relative, _path, **_flags: uploaded.append(str(relative))
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, client))
    old = supervisor.root / "old-attempt"
    old.mkdir()
    fingerprint = input_fingerprint([source])
    atomic_json(old / "config.json", {"job": {"job_id": "viewer-job"}, "fingerprint": fingerprint})
    atomic_json(old / "published.json", {"status": "done", "result": {"uploaded_files": 2}})
    atomic_json(
        target / ".viewer-publication.json",
        {
            "staging": str(target.parent / ".dp-viewer-job-old-attempt" / "cache"),
            "target": str(target),
            "source": str(source),
        },
    )
    for name in ("video.bin", "viewer_manifest.json"):
        atomic_json(
            old / "uploads" / f"{name}.json",
            {
                "relative_path": name,
                "path": str(static / name),
                "derived": False,
            },
        )
    current = supervisor.root / "new-attempt"
    reservation = current / "reservation.json"
    job = {
        "job_id": "viewer-job",
        "operation": "viewer.prepare",
        "location": {"root": str(source), "output_dir": str(target), "dataset_key": "node/source"},
        "options": {"task_config": {"digest": "task-digest"}},
        "execution": {"attempt_id": "new-attempt"},
    }
    with pytest.raises(ValueError, match="Viewer source changed"):
        supervisor._reuse_published_viewer(job, current, "changed-source", reservation)
    assert not uploaded
    from lerobot.data_platform import execution

    def real_process_group_only(pid):
        assert pid is not None and pid > 0
        return []

    monkeypatch.setattr(execution, "group_members", real_process_group_only)
    supervisor.start(job)
    assert uploaded == ["video.bin", "viewer_manifest.json"]
    assert client.completions[-1]["status"] == "done"
    assert client.checkpoints[-1][0] == "finalizing"
    assert json.loads((current / "marker.json").read_text())["acknowledged"] is True
    assert not reservation.exists()


def test_spooling_client_provides_only_inherited_identity(tmp_path):
    from lerobot.data_platform.execution import SpoolingClient

    identity = {"environment": "dev", "instance_id": "development-instance"}
    client = SpoolingClient("https://unreachable.invalid", tmp_path, server_identity=identity)
    assert client._request("GET", "/healthz") == identity
    with pytest.raises(RuntimeError, match="verified server identity"):
        client._request("POST", "/api/agents/jobs/claim")
    with pytest.raises(RuntimeError, match="verified server identity"):
        SpoolingClient("https://unreachable.invalid", tmp_path)._request("GET", "/healthz")


def test_force_stop_waits_for_child_processes(tmp_path):
    client = Client(mode="force")
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, client))
    work = supervisor.root / "attempt"
    work.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    marker = {
        "job": {"job_id": "job"},
        "work": str(work),
        "staging": str(staging),
        "final": None,
        "pid": process.pid,
        "process_identity": process_identity(process.pid),
        "publishing": False,
    }
    deadline = time.monotonic() + 5
    try:
        while len(group_members(process.pid)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(group_members(process.pid)) == 2
        supervisor.monitor(marker, process)
        assert client.completions[-1]["status"] == "cancelled"
        assert not group_members(process.pid)
        assert not staging.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait()


def test_first_viewer_job_creates_missing_cache_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PLATFORM_REQUIRE_CGROUP", "0")
    source = tmp_path / "dataset"
    _make_dataset(source)
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, Client()))
    cache = tmp_path / "vis" / "dataset"
    job = {
        "job_id": "first-viewer",
        "operation": "viewer.prepare",
        "location": {"root": str(source), "output_dir": str(cache)},
        "options": {},
        "execution": {"attempt_id": "attempt", "final_output": None},
    }
    from lerobot.data_platform import execution

    monkeypatch.setattr(execution.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=123))
    monkeypatch.setattr(execution, "process_identity", lambda pid: "test-process")
    markers = []
    monkeypatch.setattr(supervisor, "monitor", lambda marker, process: markers.append(marker))
    assert not cache.parent.exists()
    supervisor.start(job)
    assert Path(markers[0]["staging"]).is_dir()
    assert Path(markers[0]["staging"]).parent == cache.parent
    assert not cache.exists()  # Only staging is created before the worker publishes its result.


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
@pytest.mark.parametrize("named_environment", [False, True])
def test_real_worker_preserves_source_and_publishes_drop_field(
    tmp_path, monkeypatch, version, named_environment
):
    monkeypatch.setenv("DATA_PLATFORM_REQUIRE_CGROUP", "0")
    source = tmp_path / "source"
    _make_dataset(source)
    if version == "v3.0":
        from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3

        _add_v21_stat_counts(source)
        converted = tmp_path / "source_v3"
        run_convert_v3(source, out_root=converted, workers=1)
        source = converted
    before = {
        str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }
    client = Client()
    agent = fake_agent(tmp_path, client)
    if named_environment:
        from lerobot.data_platform.environment import EnvironmentIdentity, verify_directory

        monkeypatch.setenv("DATA_PLATFORM_ENV", "dev")
        monkeypatch.setenv("DATA_PLATFORM_INSTANCE_ID", "11111111-1111-4111-8111-111111111111")
        monkeypatch.setenv("DATA_PLATFORM_STATE_ROOT", str(tmp_path))
        agent.environment_identity = EnvironmentIdentity.from_env()
        verify_directory(agent.state_path.parent, "agent", initialize=True)
        state = json.loads(agent.state_path.read_text())
        state.update(environment="dev", instance_id=agent.environment_identity.instance_id)
        atomic_json(agent.state_path, state)
    supervisor = ExecutionSupervisor(agent)
    final = tmp_path / "new-output-parent" / "final-output"
    job = {
        "job_id": "job",
        "operation": "preprocess.drop_field",
        "location_id": "location",
        "location": {
            "root": str(source),
            "dataset_key": "local/source",
            "output_dir": str(tmp_path / "cache"),
        },
        "options": {"field_name": "old_field"},
        "execution": {
            "attempt_id": "attempt",
            "credential": "test-credential",
            "worker_instance_id": "worker",
            "protocol": 2,
            "final_output": str(final),
        },
    }
    supervisor.start(job)
    result = client.completions[-1]
    assert result["status"] == "done", result
    info = json.loads((final / "meta" / "info.json").read_text())
    assert "old_field" not in info["features"]
    assert result["result"]["dataset_location"]["root"] == str(final)
    from lerobot.data_platform.agent import _dataset_payload

    assert (
        result["result"]["dataset_location"]["dataset_key"]
        == _dataset_payload(final, node_name=agent.name)["dataset_key"]
    )
    assert {
        str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()
    } == before
    assert [phase for phase, _ in client.checkpoints] == ["executing", "finalizing"]


def test_completion_outage_replays_result_without_recomputing(tmp_path, monkeypatch):
    client = Client()
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, client))
    work = supervisor.root / "attempt"
    work.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    payload = {"status": "done", "result": {"verified": True}}
    atomic_json(work / "published.json", payload)
    marker = {
        "job": {"job_id": "job"},
        "work": str(work),
        "staging": str(staging),
        "final": None,
        "pid": 99999999,
        "process_identity": "never",
        "publishing": True,
        "acknowledged": False,
        "reservation": str(work / "reservation.json"),
    }
    atomic_json(work / "reservation.json", {"job_id": "job"})
    atomic_json(work / "marker.json", marker)
    real_complete = client.complete
    monkeypatch.setattr(client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        supervisor.reconcile()
    assert not json.loads((work / "marker.json").read_text())["acknowledged"]
    assert (work / "reservation.json").exists()
    monkeypatch.setattr(client, "complete", real_complete)
    assert supervisor.reconcile()
    assert client.completions == [payload | {"error": None}]
    assert json.loads((work / "marker.json").read_text())["acknowledged"]
    assert not (work / "reservation.json").exists()
    assert not supervisor.reconcile()


def test_agent_path_reservation_waits_and_renews_until_release(tmp_path):
    client = Client()
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, client))
    source = str(tmp_path / "source")
    output = str(tmp_path / "cache")
    first_work = supervisor.root / "one"
    second_work = supervisor.root / "two"
    first_work.mkdir()
    second_work.mkdir()
    first = supervisor._reserve(
        {"job_id": "one", "operation": "viewer.prepare", "options": {}},
        first_work,
        [source],
        output,
        None,
    )
    finished = threading.Event()
    reservations = []

    def wait_for_reservation():
        reservations.append(
            supervisor._reserve(
                {"job_id": "two", "operation": "viewer.prepare", "options": {}},
                second_work,
                [source],
                output,
                None,
            )
        )
        finished.set()

    thread = threading.Thread(target=wait_for_reservation)
    thread.start()
    try:
        assert not finished.wait(0.2)
        assert "Waiting for another job's data paths" in client.events
        supervisor._release(first)
        assert finished.wait(3)
        assert reservations[0].exists()
    finally:
        supervisor._release(first)
        for path in reservations:
            supervisor._release(path)
        thread.join(timeout=3)


def test_local_validated_command_is_queued_and_executed(tmp_path, monkeypatch):
    from lerobot.data_platform import viewer
    from lerobot.data_platform.cli import get_default_output_dir
    from lerobot.data_platform.execution import input_fingerprint

    monkeypatch.setenv("DATA_PLATFORM_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}")
    monkeypatch.setenv("DATA_PLATFORM_INTERNAL_EXECUTION", "")
    monkeypatch.setenv("DATA_PLATFORM_OUTPUT_DIR", str(tmp_path / "console"))
    monkeypatch.setenv("DATA_PLATFORM_CONSOLE_MODE", "full")
    source = tmp_path / "source"
    _make_dataset(source)
    _add_v21_stat_counts(source)
    static = tmp_path / "console" / "static"
    static.mkdir(parents=True)
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
        static_folder=static,
        template_folder=Path(viewer.__file__).parent / "templates",
        start_server=False,
        database_url=os.environ["DATA_PLATFORM_DATABASE_URL"],
    )
    ctx = app.extensions["data_platform_route_context"]
    store = ctx.control_plane_store
    store.bootstrap_admin(
        username="admin-user",
        password="long-test-password",
        display_name="Admin",
        bootstrap_token="test",
        expected_token="test",
    )
    with app.test_request_context("/"):
        ctx.register_dataset(
            ctx.meta_only_dataset_cls("local/source", root=source), get_default_output_dir(source)
        )
    client = app.test_client()
    assert (
        client.post(
            "/api/auth/login", json={"username": "admin-user", "password": "long-test-password"}
        ).status_code
        == 200
    )
    output = tmp_path / "output"
    created = client.post(
        "/api/preprocess/drop_field/start",
        json={"dataset_key": "local/source", "options": {"field_name": "old_field", "out_root": str(output)}},
    )
    assert created.status_code == 200, created.get_json()
    job_id = created.get_json()["job"]["id"]
    listed = client.get(f"/api/jobs/{job_id}").get_json()["job"]
    assert listed["control_job_id"] == job_id
    assert listed["requested_by_username"] == "admin-user"
    assert listed["available_actions"] == ["cancel"]
    assert listed["revision"] == 0
    store.register_user(
        username="other-operator", password="long-test-password", display_name="Other", role="operator"
    )
    other_client = app.test_client()
    other_client.post(
        "/api/auth/login", json={"username": "other-operator", "password": "long-test-password"}
    )
    assert other_client.get("/api/jobs").get_json()["jobs"] == []
    assert other_client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.get("/api/jobs").get_json()["jobs"][0]["id"] == job_id
    assert not output.exists()
    persistent = store.get_job(job_id)
    assert persistent["status"] == "queued"
    assert persistent["operation"] == "preprocess.drop_field"
    assert "request" not in persistent["options"]
    claimed = store.claim_job(persistent["node_id"], worker_instance_id="test-worker")
    work = tmp_path / "worker"
    config = {
        "job": claimed,
        "work": str(work),
        "input_roots": [str(source)],
        "fingerprint": input_fingerprint([source]),
    }
    atomic_json(work / "config.json", config)
    from lerobot.data_platform.processing.local_executor import execute_local_operator
    from tests.datasets.test_data_platform_agent import _agent

    agent = _agent(tmp_path, tmp_path, tmp_path)
    agent.name = "local-test"
    result = execute_local_operator(agent, claimed)
    assert result["local_job"]["status"] == "done"
    assert "old_field" not in json.loads((output / "meta" / "info.json").read_text())["features"]
    assert "old_field" in json.loads((source / "meta" / "info.json").read_text())["features"]


def test_agent_ca_bundle_keeps_explicit_tls_options(monkeypatch):
    from lerobot.data_platform.agent import AgentClient

    monkeypatch.setenv("DATA_PLATFORM_AGENT_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")
    assert AgentClient("https://example.invalid").verify == "/etc/ssl/certs/ca-certificates.crt"
    assert AgentClient("https://example.invalid", verify=False).verify is False
    assert AgentClient("https://example.invalid", verify="/custom/ca.pem").verify == "/custom/ca.pem"


def test_curation_worker_retains_checksummed_bundle_through_publication(tmp_path, monkeypatch):
    import shutil

    from lerobot.data_platform.curation import scan_snapshot, validate_bundle
    from tests.datasets.test_lifecycle import _make_dataset as make_source
    from tests.datasets.test_lifecycle import _snapshot

    monkeypatch.setenv("DATA_PLATFORM_REQUIRE_CGROUP", "0")
    source = tmp_path / "source"
    make_source(source)
    before = _snapshot(source)
    snapshot = scan_snapshot(source, "local/source", "ds_test")
    target = {
        "dataset_key": "local/source",
        "location_id": "location",
        "dataset_version_id": snapshot.version["version_id"],
    }
    client = Client()
    received = tmp_path / "received"

    def upload(state, job_id, relative, path, **flags):
        assert flags.get("curation")
        destination = received / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)

    client.upload_artifact = upload
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, client))
    job = {
        "job_id": "job",
        "operation": "curation.stage",
        "location_id": "location",
        "location": {
            "root": str(source),
            "dataset_key": "local/source",
            "output_dir": str(tmp_path / "cache"),
        },
        "options": {
            "target": target,
            "snapshot": snapshot.to_dict(),
            "task_config": snapshot.task_config,
            "parameters": {"prepare_workers": 1},
        },
        "execution": {
            "attempt_id": "attempt",
            "credential": "test-credential",
            "worker_instance_id": "worker",
            "protocol": 2,
            "final_output": None,
        },
    }
    supervisor.start(job)
    assert client.completions[-1]["status"] == "done", client.completions
    validate_bundle(received, "curation.stage", target)
    assert (received / "static/viewer_manifest.json").is_file()
    assert before == _snapshot(source)

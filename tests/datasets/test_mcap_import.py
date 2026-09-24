"""Raw import contracts, authorization, publication and timestamp segmentation."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from lerobot.data_platform import mcap_contract, mcap_import
from lerobot.data_platform.agent import discover_datasets
from lerobot.data_platform.execution import input_fingerprint
from lerobot.data_platform.mcap_contract import McapImportConfig
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_data_platform_agent import _agent


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcap_contract", mcap_contract)
    path = Path(mcap_import.__file__).with_name("mcap_worker.py")
    spec = importlib.util.spec_from_file_location("mcap_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "options",
    [
        {"max_files": -1},
        {"max_files": True},
        {"short_side": 255},
        {"adapter": "generic"},
        {"fps": 60},
        {"unknown": 3},
    ],
)
def test_invalid_config(options):
    with pytest.raises(ValueError):
        McapImportConfig.from_dict(options)


def test_convert_button_requires_a_successful_current_inspection():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the MCAP page behavior check")
    template = (Path(mcap_import.__file__).parent / "templates" / "mcap_import.html").read_text()
    script = template.split("<script>", 1)[1].split("</script>", 1)[0]
    checks = """
const assert = require('node:assert/strict');
const app = mcapImportApp({user_id: 'owner', role: 'admin'}, () => {});
app.selected = ['source']; app.outputParent = '/datasets/output';
app.jobs = [{job_id: 'failed', location_id: 'source', operation: 'import.mcap.inspect',
    status: 'error', requested_by: 'owner', error: 'invalid raw output path',
    options: {config: {short_side: 256, max_files: 3}}}];
assert.equal(app.readyToConvert(), false);
assert.match(app.inspectionLabel('source'), /invalid raw output path/);
app.jobs.unshift({job_id: 'valid', location_id: 'source', operation: 'import.mcap.inspect',
    status: 'done', requested_by: 'owner', result: {mcap_import: {
        config: {short_side: 256, max_files: 3}, files: [{file: 'sample.mcap'}],
        total_episodes: 1, total_frames: 30, rejected: []}}});
assert.equal(app.readyToConvert(), true);
app.maxFiles = 0;
assert.equal(app.readyToConvert(), false);
"""
    result = subprocess.run([node, "-e", script + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_raw_discovery_fingerprint_and_source_preservation(tmp_path):
    root = tmp_path / "task" / "00001"
    root.mkdir(parents=True)
    source = root / "00001.mcap"
    source.write_bytes(b"original")
    before = input_fingerprint([root])
    found = discover_datasets([tmp_path], node_name="test")
    assert len(found) == 1
    assert found[0]["metadata"]["source_format"] == "mcap"
    assert found[0]["metadata"]["mcap_count"] == 1
    assert list(root.iterdir()) == [source]
    assert source.read_bytes() == b"original"
    source.write_bytes(b"changed-longer")
    assert input_fingerprint([root]) != before
    (root / "link.mcap").symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        input_fingerprint([root])


def test_valid_segments_trim_missing_pose_and_split_gaps(worker, monkeypatch, tmp_path):
    source = tmp_path / "sample.mcap"
    source.write_bytes(b"fixture")
    start = 1_700_000_000_000_000_000
    ticks = start + np.arange(180, dtype=np.int64) * 1_000_000_000 // 30

    def messages(path, topics):
        for key, topic in worker.TOPICS.items():
            for index, timestamp in enumerate(ticks):
                if key == "left_pose" and (index >= 150 or 60 <= index < 75):
                    continue
                msg = SimpleNamespace(
                    header=SimpleNamespace(timestamp=int(timestamp)),
                    format="h264",
                    frame_id="world",
                    value=0.05,
                    pose=SimpleNamespace(
                        position=SimpleNamespace(x=index, y=0, z=0),
                        orientation=SimpleNamespace(x=0, y=0, z=0, w=1),
                    ),
                )
                yield topic, None, msg

    monkeypatch.setattr(worker, "messages", messages)
    monkeypatch.setattr(
        worker,
        "image_frames",
        lambda *args: (item for item in [(start, SimpleNamespace(width=1600, height=1300))]),
    )
    summary, _, grid, segments, _, _ = worker.inspect_file(source, McapImportConfig())
    assert len(segments) == 2
    assert summary["trimmed"]
    assert summary["output_shapes"]["left"] == [316, 256]
    assert grid[segments[-1][1] - 1] <= ticks[149]
    assert all(not (ticks[60] <= grid[a] <= ticks[74]) for a, _ in segments)
    assert summary["shared_frame_verified"] is False
    features = worker.feature_schema(summary)
    assert not any("head" in key or "action" in key for key in features)


def _setup(tmp_path):
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    admin = _bootstrap(client).get_json()["user"]
    token, node = store.enroll_node(
        name="import-node",
        hostname="test",
        allowed_roots=["/datasets"],
        writable_roots=["/datasets"],
        capabilities={
            "job_protocol": 2,
            "mcap_import_protocol": 1,
            "mcap_import": {"ready": True},
            "operations": sorted(mcap_contract.OPERATIONS),
        },
        enrollment_token="test",
        expected_token="test",
    )
    location = store.sync_locations(
        node["node_id"],
        [
            {
                "root": "/datasets/raw/task/00001",
                "dataset_key": "raw",
                "output_dir": "",
                "metadata": {"source_format": "mcap", "mcap_count": 3},
            }
        ],
    )[0]
    return store, client, admin, node, location, {"Authorization": f"Bearer {token}"}


def _inspect(client, location, headers):
    endpoint = f"/api/control/locations/{location['location_id']}/mcap-jobs"
    response = client.post(endpoint, json={"operation": "inspect"})
    assert response.status_code == 202, response.json
    job = response.json["job"]
    claimed = client.post(
        "/api/agents/jobs/claim", headers=headers, json={"worker_instance_id": "test-worker"}
    ).json["job"]
    assert claimed["job_id"] == job["job_id"]
    headers.update(
        {
            "X-Job-Attempt": claimed["execution"]["attempt_id"],
            "X-Job-Credential": claimed["execution"]["credential"],
        }
    )
    report = {
        "config_digest": McapImportConfig().digest,
        "files": [{"file": "sample.mcap"}],
        "input_fingerprint": "source-snapshot",
    }
    completed = client.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        headers=headers,
        json={"status": "done", "result": {"mcap_import": report}},
    )
    assert completed.status_code == 200, completed.json
    return job["job_id"]


def test_inspect_convert_publish_and_scope_inheritance(tmp_path):
    store, client, admin, node, location, headers = _setup(tmp_path)
    viewer = store.register_user(username="scoped", password="valid-password", role="viewer")
    excluded = store.register_user(username="excluded", password="valid-password", role="viewer")
    store.set_mutation_locations(viewer["user_id"], [location["location_id"]], actor=admin)
    store.set_mutation_locations(excluded["user_id"], [], actor=admin)
    assert client.get("/api/control/locations").json["locations"] == []
    assert len(client.get("/api/control/locations?format=mcap").json["locations"]) == 1
    assert (
        client.post(f"/api/control/locations/{location['location_id']}/viewer-jobs", json={}).status_code
        == 400
    )
    endpoint = f"/api/control/locations/{location['location_id']}/mcap-jobs"
    probe = _inspect(client, location, headers)
    body = {"operation": "convert", "inspection_job_id": probe, "out_root": "/datasets/converted/00001"}
    assert client.post(endpoint, json={**body, "config": {"max_files": 2}}).status_code == 409
    assert client.post(endpoint, json={**body, "out_root": location["root"]}).status_code == 400
    response = client.post(endpoint, json=body)
    assert response.status_code == 202, response.json
    job = response.json["job"]
    claimed = client.post(
        "/api/agents/jobs/claim", headers=headers, json={"worker_instance_id": "test-worker"}
    ).json["job"]
    assert claimed["execution"]["final_output"] == body["out_root"]
    headers.update(
        {
            "X-Job-Attempt": claimed["execution"]["attempt_id"],
            "X-Job-Credential": claimed["execution"]["credential"],
        }
    )
    completed = client.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        headers=headers,
        json={
            "status": "done",
            "result": {
                "dataset_location": {
                    "dataset_key": "converted",
                    "root": body["out_root"],
                    "metadata": {"source_format": "lerobot"},
                }
            },
        },
    )
    assert completed.status_code == 200, completed.json
    output = completed.json["job"]["result"]["synced_location"]
    assert output["location_id"] in {
        row["location_id"] for row in store.accessible_locations(viewer["user_id"])
    }
    assert store.accessible_locations(excluded["user_id"]) == []
    assert len(client.get("/api/control/locations").json["locations"]) == 1
    assert len(client.get("/api/control/locations?format=all").json["locations"]) == 2


@pytest.mark.parametrize("role,allowed", [("viewer", False), ("operator", True), ("data_manager", True)])
def test_import_roles_and_dataset_scope(tmp_path, role, allowed):
    store, client, admin, _, location, _ = _setup(tmp_path)
    user = store.register_user(username="worker", password="valid-password", role=role)
    store.set_mutation_locations(user["user_id"], [location["location_id"]], actor=admin)
    client.post("/api/auth/logout", json={})
    assert (
        client.post("/api/auth/login", json={"username": "worker", "password": "valid-password"}).status_code
        == 200
    )
    endpoint = f"/api/control/locations/{location['location_id']}/mcap-jobs"
    assert client.post(endpoint, json={}).status_code == (202 if allowed else 403)
    store.set_mutation_locations(user["user_id"], [], actor=admin)
    if role in {"viewer", "operator"}:
        assert client.get("/api/control/locations?format=all").json["locations"] == []
    assert client.post(endpoint, json={}).status_code == (202 if role == "data_manager" else 403)


def test_changed_source_rejected_before_starting_worker(tmp_path, monkeypatch):
    source = tmp_path / "raw"
    source.mkdir()
    (source / "sample.mcap").write_bytes(b"sample")
    agent = _agent(tmp_path, tmp_path, tmp_path)
    monkeypatch.setattr(mcap_import, "runtime_status", lambda: {"ready": True})
    job = {
        "job_id": "test",
        "operation": "import.mcap.convert",
        "location": {"metadata": {"source_format": "mcap"}},
        "options": {"input_fingerprint": "stale"},
    }
    with pytest.raises(ValueError, match="changed after inspection"):
        mcap_import.execute_import(agent, job, source)
    assert not (tmp_path / "executions").exists()


def test_cancelled_worker_stops_before_io(worker, tmp_path, monkeypatch):
    stop = tmp_path / "stop"
    stop.touch()
    monkeypatch.setenv("DATA_PLATFORM_EXECUTION_STOP_FILE", str(stop))
    with pytest.raises(InterruptedError):
        worker.stop_check()


def test_checkpoint_reuse_requires_matching_content(worker, tmp_path):
    root = tmp_path / "checkpoint"
    root.mkdir()
    data = root / "data.parquet"
    data.write_bytes(b"complete")
    receipt = tmp_path / "receipt.json"
    lineage = [{"episode_index": 0}]
    worker.atomic_json(receipt, {"lineage": lineage, "manifest": worker.checkpoint_manifest(root)})
    assert worker.load_checkpoint(root, receipt) == lineage
    data.write_bytes(b"tampered")
    assert worker.load_checkpoint(root, receipt) is None

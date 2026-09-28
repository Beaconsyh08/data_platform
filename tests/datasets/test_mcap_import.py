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
from lerobot.data_platform.mcap_contract import McapImportConfig, default_output_root
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


@pytest.mark.parametrize(
    "source,roots,expected",
    [
        (
            "/storage/raw/collection/task/00002",
            ["/other/output", "/storage/raw_lerobot"],
            "/storage/raw_lerobot/collection/task/00002",
        ),
        ("/storage/raw/task/00002", ["/storage", "/storage/raw_lerobot"], "/storage/raw_lerobot/task/00002"),
        ("/storage/raw/task/00002", ["/storage"], "/storage/raw/task/00002_lerobot"),
        ("/storage/raw/task/00002", ["/other/output"], None),
        ("/storage/raw/task/00002", ["/storage/raw/task/00002"], None),
        ("/storage/raw/task/00002", [], None),
        ("/", ["/"], None),
        ("relative/source", ["/storage"], None),
        ("/storage/raw/../source", ["/storage"], None),
    ],
)
def test_output_follows_source_and_writable_roots(source, roots, expected):
    assert default_output_root(source, roots) == expected


def test_convert_page_selection_output_and_job_handoff():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the MCAP page behavior check")
    template = (Path(mcap_import.__file__).parent / "templates" / "mcap_import.html").read_text()
    script = template.split("<script>", 1)[1].split("</script>", 1)[0]
    checks = """
const assert = require('node:assert/strict');
(async () => {
    const submitted = [];
    const app = mcapImportApp({user_id: 'owner', role: 'admin'}, jobs => submitted.push(...jobs));
    app.nodeId = 'node';
    app.nodes = [{node_id: 'node', status: 'online', capabilities: {mcap_import: {ready: true}}}];
    app.sources = Array.from({length: 19}, (_, i) => ({location_id: `source-${i}`, node_id: 'node',
        root: `/storage/raw/task/${String(i).padStart(5, '0')}`, metadata: {mcap_count: 12},
        default_import_output: `/storage/raw_lerobot/task/${String(i).padStart(5, '0')}`}));
    assert.equal(app.pageSources().length, 8);
    app.selectVisible(); app.page = 2; app.selectVisible();
    assert.equal(app.selected.length, 16);
    app.search = '00018'; app.page = 1;
    assert.equal(app.filteredSources().length, 1);
    assert.equal(app.selected.length, 16); // Filtering preserves selected folders.
    app.selected = ['source-18'];
    assert.equal(app.readyToConvert(), true); // No inspection job is needed.
    assert.equal(app.outputFor(app.sources[18]), '/storage/raw_lerobot/task/00018');
    assert.equal(app.selectedFileCount(), 3);
    app.maxFiles = 0;
    assert.equal(app.selectedFileCount(), 12);
    app.maxFiles = -1;
    assert.equal(app.readyToConvert(), false);
    app.maxFiles = 3;
    app.outputParent = '/storage/custom';
    assert.equal(app.outputFor(app.sources[18]), '/storage/custom/00018');
    app.json = async (url, body) => {
        assert.equal(body.operation, 'convert');
        assert.equal(body.inspection_job_id, undefined);
        assert.equal(body.out_root, '/storage/custom/00018');
        return {job: {job_id: 'new-job', location_id: 'source-18'}};
    };
    await app.submit('convert');
    assert.equal(submitted[0].job_id, 'new-job');
    assert.equal(app.selected.length, 0);
    assert.match(app.notice, /Jobs/);
    app.selected = ['source-18']; app.user.role = 'viewer';
    await app.submit('convert');
    assert.equal(submitted.length, 1);
    app.changeNode();
    assert.equal(app.selected.length, 0);
    assert.equal(app.outputParent, '');
})().catch(error => { console.error(error); process.exitCode = 1; });
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


def test_direct_convert_with_suggested_output_and_safety_checks(tmp_path):
    store, client, _, _, location, _ = _setup(tmp_path)
    endpoint = f"/api/control/locations/{location['location_id']}/mcap-jobs"
    source = client.get("/api/control/locations?format=mcap").json["locations"][0]
    expected = "/datasets/raw/task/00001_lerobot"
    assert source["default_import_output"] == expected
    for path, status in [(location["root"], 400), ("/elsewhere/output", 403)]:
        response = client.post(endpoint, json={"operation": "convert", "out_root": path})
        assert response.status_code == status, response.json
    response = client.post(endpoint, json={"operation": "convert"})
    assert response.status_code == 202, response.json
    job = response.json["job"]
    assert job["options"]["out_root"] == expected
    assert job["options"]["source_root"] == location["root"]
    assert "inspection_job_id" not in job["options"]
    assert "input_fingerprint" not in job["options"]
    assert [row["operation"] for row in store.list_jobs()] == ["import.mcap.convert"]


def test_direct_conversion_inspects_before_writing(worker, tmp_path, monkeypatch):
    source = tmp_path / "raw"
    source.mkdir()
    (source / "bad.mcap").write_bytes(b"invalid")
    events = []
    monkeypatch.setattr(worker, "emit", lambda phase, **kwargs: events.append({"phase": phase, **kwargs}))

    def invalid_file(*args):
        raise ValueError("Missing required signals")

    monkeypatch.setattr(worker, "inspect_file", invalid_file)
    monkeypatch.setattr(worker, "write_file", lambda *args: pytest.fail("Invalid input must not be written"))
    with pytest.raises(ValueError, match="No valid MCAP files"):
        worker.run(
            {
                "source": str(source),
                "operation": "convert",
                "config": {},
                "input_fingerprint": "test",
                "output": str(tmp_path / "output"),
                "checkpoint_root": str(tmp_path / "checkpoints"),
                "result": str(tmp_path / "report.json"),
            }
        )
    assert not (tmp_path / "output").exists()
    rejected = [event for event in events if event["phase"] == "rejected"]
    assert "bad.mcap: Missing required signals" in rejected[-1]["message"]


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
@pytest.mark.parametrize("operation", ["inspect", "convert"])
def test_import_roles_and_dataset_scope(tmp_path, role, allowed, operation):
    store, client, admin, _, location, _ = _setup(tmp_path)
    user = store.register_user(username="worker", password="valid-password", role=role)
    store.set_mutation_locations(user["user_id"], [location["location_id"]], actor=admin)
    client.post("/api/auth/logout", json={})
    assert (
        client.post("/api/auth/login", json={"username": "worker", "password": "valid-password"}).status_code
        == 200
    )
    endpoint = f"/api/control/locations/{location['location_id']}/mcap-jobs"
    assert client.post(endpoint, json={"operation": operation}).status_code == (202 if allowed else 403)
    store.set_mutation_locations(user["user_id"], [], actor=admin)
    if role in {"viewer", "operator"}:
        assert client.get("/api/control/locations?format=all").json["locations"] == []
    assert client.post(endpoint, json={"operation": operation}).status_code == (
        202 if role == "data_manager" else 403
    )


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


@pytest.mark.parametrize(
    "cpu,memory_gib,node_workers,inherited_workers,expected",
    [
        (112, 1200, 4, 4, 4),
        (112, 1200, 8, 8, 8),
        (112, 1200, 16, 16, 8),
        (112, 1200, 8, 4, 4),
        (16, 30, 8, 8, 4),
        (8, 16, 4, 4, 2),
        (16, 3, 4, 4, 1),
        (16, 16, 4, 2, 2),
        (1, 1, 4, 4, 1),
    ],
)
def test_conversion_worker_budget(monkeypatch, cpu, memory_gib, node_workers, inherited_workers, expected):
    from lerobot.data_platform.execution_resources import ExecutionResources

    budget = ExecutionResources(cpu, memory_gib * 1024**3, node_workers, 4, 4)
    monkeypatch.setattr(ExecutionResources, "resolve", lambda: budget)
    monkeypatch.setenv("DATA_PLATFORM_EXECUTION_WORKERS", str(inherited_workers))
    assert mcap_import.conversion_workers() == expected
    assert mcap_import.worker_environment()["OMP_NUM_THREADS"] == "1"


def test_file_checkpoint_reused_without_encoding(worker, monkeypatch, tmp_path):
    import hashlib
    import json

    source = tmp_path / "sample.mcap"
    source.write_bytes(b"source")
    config = McapImportConfig()
    sha = worker.file_hash(source)
    key = hashlib.sha256(f"{source.name}:{sha}:{config.digest}".encode()).hexdigest()
    destination = tmp_path / key
    destination.mkdir()
    (destination / "data").write_bytes(b"converted")
    lineage = [{"episode_index": 0}]
    (tmp_path / f"{key}.json").write_text(
        json.dumps(
            {
                "lineage": lineage,
                "manifest": worker.checkpoint_manifest(destination),
            }
        )
    )
    summary = {
        "output_shapes": {},
        "source_duration_s": 1,
        "valid_duration_s": 1,
        "segments": [{}],
        "frames": 30,
    }
    monkeypatch.setattr(worker, "inspect_file", lambda *args: (summary,))
    monkeypatch.setattr(worker, "write_file", lambda *args: pytest.fail("Checkpoint should skip encoding"))
    result = worker.process_file(source, config, True, tmp_path, 0, 1)
    assert result["root"] == destination
    assert result["lineage"] == lineage
    assert result["reused"] is True
    (destination / "data").write_bytes(b"corrupt")
    assert worker.load_checkpoint(destination, tmp_path / f"{key}.json") is None


def test_parallel_completion_keeps_source_order(worker, monkeypatch, tmp_path):
    import json

    source = tmp_path / "source"
    source.mkdir()
    for name in ("00001.mcap", "00002.mcap", "00003.mcap"):
        (source / name).touch()
    monkeypatch.setattr(
        worker,
        "file_results",
        lambda files, *args: iter(
            [
                (
                    2,
                    {"summary": {"file": files[2].name, "output_shapes": {}, "frames": 60, "segments": [{}]}},
                ),
                (
                    0,
                    {"summary": {"file": files[0].name, "output_shapes": {}, "frames": 30, "segments": [{}]}},
                ),
                (1, {"rejected": {"file": files[1].name, "reason": "Invalid signal"}}),
            ]
        ),
    )
    report = tmp_path / "report.json"
    worker.run(
        {
            "operation": "inspect",
            "source": str(source),
            "config": {},
            "checkpoint_root": str(tmp_path / "cache"),
            "result": str(report),
            "input_fingerprint": "test",
        }
    )
    result = json.loads(report.read_text())
    assert [row["file"] for row in result["files"]] == ["00001.mcap", "00003.mcap"]
    assert result["total_frames"] == 90
    assert result["total_episodes"] == 2
    assert result["rejected"][0]["file"] == "00002.mcap"


def test_file_pool_stop_is_bounded_and_keeps_completed_work(tmp_path):
    script = tmp_path / "pool_check.py"
    script.write_text("""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import mcap_worker as worker

def fake_file(path, config, convert, cache, i, total):
    if i == 0:
        (cache / "completed").write_text("checkpoint")
        return {"ok": True}
    while True:
        worker.emit("converting", file=str(path))
        time.sleep(0.01)

if __name__ == "__main__":
    worker.process_file = fake_file
    worker.emit = lambda *args, **kwargs: None
    results = worker.file_results(list(range(10)), None, True, Path(sys.argv[2]), 2)
    assert next(results) == (0, {"ok": True})
    results.close()
    assert (Path(sys.argv[2]) / "completed").read_text() == "checkpoint"
""")
    subprocess.run(
        [sys.executable, str(script), str(Path(mcap_import.__file__).parent), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_camera_reads_overlap_without_reordering(worker):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def camera(side):
        for stamp in [101, 102, 107]:
            barrier.wait(timeout=5)
            yield side, stamp

    images = {side: camera(side) for side in worker.SIDES}
    with ThreadPoolExecutor(max_workers=2) as readers:
        for stamp in [101, 102, 107]:
            assert worker.next_camera_images(images, readers) == {
                side: (side, stamp) for side in worker.SIDES
            }


def test_camera_read_failure_waits_for_other_reader_before_cleanup(worker):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    other_started = Event()

    def broken():
        assert other_started.wait(timeout=5)
        raise ValueError("Missing decoded frame")
        yield  # make this an iterator

    def other():
        other_started.set()
        yield "frame"

    images = {"left": broken(), "right": other()}
    with (
        pytest.raises(ValueError, match="Missing decoded frame"),
        ThreadPoolExecutor(max_workers=2) as readers,
    ):
        worker.next_camera_images(images, readers)
    # The healthy stream is no longer executing and can safely be closed by write_file's finally.
    for iterator in images.values():
        iterator.close()

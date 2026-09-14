"""UMI data keeps its native signals and embedded images throughout the console."""

import csv
import io
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

from lerobot.data_platform.cli import load_platform_metadata
from lerobot.data_platform.precompute.annotation import write_episode_csv
from lerobot.data_platform.precompute.data_profile import resolve_data_profile


@pytest.fixture
def umi_root(tmp_path):
    root = tmp_path / "umi"
    (root / "meta/episodes/chunk-000").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    features = {}
    arrays = {}
    for key in ("head_image", "left_arm_image", "right_arm_image"):
        features[key] = {"dtype": "image", "shape": [4, 4, 3], "names": ["height", "width", "channels"]}
        values = []
        for i in range(6):
            buffer = io.BytesIO()
            Image.new("RGB", (4, 4), (i, 2, 3)).save(buffer, format="PNG")
            values.append({"bytes": buffer.getvalue(), "path": f"frame-{i:06d}.png"})
        arrays[key] = pa.array(values)
    for prefix in ("head", "left_arm", "right_arm"):
        for suffix, names in (
            ("pose", ["x", "y", "z", "roll", "pitch", "yaw"]),
            ("quaternion_pose", ["x", "y", "z", "qw", "qx", "qy", "qz"]),
        ):
            key = f"{prefix}_{suffix}"
            features[key] = {"dtype": "float32", "shape": [len(names)], "names": names}
            arrays[key] = pa.array(
                [[float(i)] * len(names) for i in range(6)], type=pa.list_(pa.float32(), len(names))
            )
    for key in ("left_gripper_pos", "right_gripper_pos"):
        features[key] = {"dtype": "float32", "shape": [1], "names": [key]}
        arrays[key] = pa.array([6.72, 80, 91, 10, 20, 30], type=pa.float32())
    for key, values in {
        "timestamp": [0, 1 / 30, 2 / 30] * 2,
        "frame_index": [0, 1, 2] * 2,
        "episode_index": [0] * 3 + [1] * 3,
        "index": list(range(6)),
        "task_index": [0] * 6,
    }.items():
        dtype = "float32" if key == "timestamp" else "int64"
        features[key] = {"dtype": dtype, "shape": [1], "names": None}
        arrays[key] = pa.array(values, type=pa.float32() if key == "timestamp" else pa.int64())
    pq.write_table(pa.table(arrays), root / "data/chunk-000/file-000.parquet")
    info = {
        "codebase_version": "v3.0",
        "robot_type": "UMI",
        "fps": 30,
        "total_episodes": 2,
        "total_frames": 6,
        "total_tasks": 1,
        "chunks_size": 1000,
        "splits": {"train": "0:2"},
        "features": features,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": None,
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/stats.json").write_text("{}")
    pq.write_table(
        pa.table({"task_index": [0], "task": ["Pick up the object."]}), root / "meta/tasks.parquet"
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "episode_index": i,
                    "length": 3,
                    "tasks": ["Pick up the object."],
                    "dataset_from_index": i * 3,
                    "dataset_to_index": i * 3 + 3,
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                    "meta/episodes/chunk_index": 0,
                    "meta/episodes/file_index": 0,
                }
                for i in range(2)
            ]
        ),
        root / "meta/episodes/chunk-000/file-000.parquet",
    )
    (root / "export_manifest.json").write_text(json.dumps({"source": "rosbag2_mcap", "episodes": [0, 1]}))
    return root


def test_umi_profile_does_not_infer_dvt(umi_root):
    profile = resolve_data_profile(umi_root)
    assert profile.robot_profile == "umi"
    assert profile.legacy_data_version is None
    assert profile.stage_profile == "time_equal_v1"


def test_processing_default_is_dvt2_without_rewriting_source_profile(umi_root):
    from lerobot.data_platform.precompute.data_profile import (
        dataset_semantics,
        profile_from_data_version,
        resolve_processing_profile,
        write_data_profile,
    )

    info_path = umi_root / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["robot_type"] = "h10w"
    info_path.write_text(json.dumps(info))
    original = profile_from_data_version(
        "DVT1", info["features"], resolution_source="explicit", confirmed=True
    )
    profile_path = write_data_profile(umi_root, original)
    before = profile_path.read_bytes()
    assert resolve_data_profile(umi_root).legacy_data_version == "DVT1"
    selected = resolve_processing_profile(umi_root)
    assert selected.legacy_data_version == "DVT2"
    assert selected.stage_profile == "h10w_dvt2_stage_v1"
    assert dataset_semantics(info, original)["default_processing_profile"]["legacy_data_version"] == "DVT2"
    assert resolve_processing_profile(umi_root, data_version_override="DVT1").legacy_data_version == "DVT1"
    assert profile_path.read_bytes() == before


def test_umi_equal_time_count_and_existing_annotations(umi_root, tmp_path):
    path = umi_root / "data/chunk-000/file-000.parquet"
    source = pq.read_table(path).append_column("subtask_state", pa.array([0, 1, 1] * 2, type=pa.int64()))
    pq.write_table(source, path)
    meta = load_platform_metadata(umi_root, "local/umi")
    output = tmp_path / "signals.csv"
    _, boundaries, _ = write_episode_csv(umi_root, meta, 0, output, None, None, True)
    assert boundaries is None
    with output.open() as handle:
        assert [float(row["stage"]) for row in csv.DictReader(handle)] == [0, 1, 1]
    _, boundaries, issues = write_episode_csv(
        umi_root, meta, 0, output, None, None, True, force_recompute_stage=True, fallback_stage_count=3
    )
    assert boundaries["equal_time"] and boundaries["num_stages"] == 3 and not issues
    with output.open() as handle:
        rows = list(csv.DictReader(handle))
    assert [float(row["stage"]) for row in rows] == [0, 0.5, 1]
    np.testing.assert_allclose([float(row["left_gripper_pos"]) for row in rows], [6.72, 80, 91])
    assert pq.read_table(path).equals(source)


def test_umi_csv_has_unique_named_raw_signals(umi_root, tmp_path):
    output = tmp_path / "episode.csv"
    meta = load_platform_metadata(umi_root, "local/umi")
    _, boundaries, issues = write_episode_csv(umi_root, meta, 0, output, None, None, False)
    with output.open() as handle:
        reader = csv.DictReader(handle)
        assert len(reader.fieldnames) == len(set(reader.fieldnames))
        rows = list(reader)
    assert "head_pose.x" in rows[0]
    assert "left_arm_quaternion_pose.qw" in rows[0]
    np.testing.assert_allclose(float(rows[0]["left_gripper_pos"]), 6.72)
    assert [float(row["stage"]) for row in rows] == [0, 0.5, 1]
    assert boundaries["equal_time"] and boundaries["num_stages"] == 5
    assert not issues


def test_native_split_merge_preserves_bytes_types_and_indices(umi_root, tmp_path):
    from lerobot.data_platform.precompute.dataset_io import read_episode_table
    from lerobot.data_platform.precompute.preprocess.dataset_merge import run_merge
    from lerobot.data_platform.precompute.preprocess.dataset_split import run_split

    source = pq.read_table(umi_root / "data/chunk-000/file-000.parquet")
    first = run_split(umi_root, tmp_path / "first", episode_range="0:1")
    second = run_split(umi_root, tmp_path / "second", episode_range="1:2")
    merged = run_merge([first.out_root, second.out_root], tmp_path / "merged")
    meta = load_platform_metadata(merged.out_root, merged.repo_id)
    combined = pa.concat_tables([read_episode_table(merged.out_root, meta, i) for i in range(2)])
    assert combined.equals(source)
    assert meta.info["video_path"] is None
    assert meta.info["features"]["head_image"]["dtype"] == "image"
    assert meta.stats["index"]["max"] == [5]
    assert meta.total_frames == 6
    assert len(merged.episode_lineage) == 2
    assert (first.out_root / "provenance/source_000/export_manifest.json").is_file()
    assert not (first.out_root / "export_manifest.json").exists()
    assert pq.read_table(umi_root / "data/chunk-000/file-000.parquet").equals(source)


def test_native_dry_run_and_conflict(umi_root, tmp_path):
    from lerobot.data_platform.precompute.preprocess.dataset_split import run_split

    before = sorted(tmp_path.rglob("*"))
    result = run_split(umi_root, tmp_path / "output", episode_range="1:2", dry_run=True)
    assert result.total_frames == 3
    assert sorted(tmp_path.rglob("*")) == before
    with pytest.raises(FileExistsError):
        run_split(umi_root, umi_root, dry_run=True)


def test_native_merge_checks_arrow_schema_and_episode_selection(umi_root, tmp_path):
    import shutil

    from lerobot.data_platform.precompute.preprocess.dataset_merge import run_merge, validate_merge_sources

    other = tmp_path / "other"
    shutil.copytree(umi_root, other)
    with pytest.raises(ValueError, match="episodes not found"):
        run_merge([umi_root, other], tmp_path / "output", exclude_episodes=[[9], []], dry_run=True)
    path = other / "data/chunk-000/file-000.parquet"
    table = pq.read_table(path)
    index = table.schema.get_field_index("left_gripper_pos")
    table = table.set_column(index, "left_gripper_pos", table["left_gripper_pos"].cast(pa.float64()))
    pq.write_table(table, path)
    with pytest.raises(ValueError, match="Arrow schema"):
        validate_merge_sources([umi_root, other])
    with pytest.raises(ValueError, match="Arrow schema"):
        run_merge([umi_root, other], tmp_path / "output", dry_run=True)
    assert not (tmp_path / "output").exists()


def test_umi_cache_manifest_failure_is_not_success(umi_root, tmp_path, monkeypatch):
    from lerobot.data_platform import cli

    def fail(**kwargs):
        raise OSError("injected manifest failure")

    monkeypatch.setattr(cli, "write_viewer_manifest", fail)
    with pytest.raises(OSError, match="viewer manifest"):
        cli.run_precompute(
            root=umi_root,
            output_dir=tmp_path / "cache",
            visualize_only=True,
            prepare_videos=False,
            show_progress=False,
        )


def test_native_failure_cleans_staging(umi_root, tmp_path, monkeypatch):
    from lerobot.data_platform.precompute.preprocess import v3_native
    from lerobot.data_platform.precompute.preprocess.dataset_split import run_split

    def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(v3_native, "_episode_stats", fail)
    with pytest.raises(RuntimeError, match="injected"):
        run_split(umi_root, tmp_path / "output")
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".output.staging-*"))


def test_umi_viewer_refreshes_old_csv_only(umi_root, tmp_path):
    import av

    from lerobot.data_platform.cli import run_precompute

    output = tmp_path / "cache"
    before = {p: p.read_bytes() for p in umi_root.rglob("*") if p.is_file()}
    run_precompute(root=umi_root, output_dir=output, visualize_only=True, show_progress=False)
    static = output / "static"
    manifest = json.loads((static / "viewer_manifest.json").read_text())
    assert manifest["data_version"] is None
    assert manifest["robot_type"] == "UMI"
    assert not manifest["operation_capabilities"]["standardize"]["available"]
    videos = list((static / "videos").rglob("*.mp4"))
    assert len(videos) == 6
    mtimes = {p: p.stat().st_mtime_ns for p in videos}
    for path in videos:
        with av.open(str(path)) as container:
            assert len(list(container.decode(video=0))) == 3
    manifest.pop("signal_columns_version")
    (static / "viewer_manifest.json").write_text(json.dumps(manifest))
    csv_path = static / "csv/episode_000000_ds1.csv"
    csv_path.write_text("timestamp,x,x\n0,1,2\n")
    run_precompute(root=umi_root, output_dir=output, visualize_only=True, show_progress=False)
    assert "head_pose.x" in csv_path.read_text()
    assert {p: p.stat().st_mtime_ns for p in videos} == mtimes
    manifest = json.loads((static / "viewer_manifest.json").read_text())
    manifest["data_profile"]["gripper_encoding"] = "legacy"
    (static / "viewer_manifest.json").write_text(json.dumps(manifest))
    csv_path.write_text("timestamp,left_gripper_pos\n0,0.0672\n")
    run_precompute(root=umi_root, output_dir=output, visualize_only=True, show_progress=False)
    assert "head_pose.x" in csv_path.read_text()
    csv_mtime = csv_path.stat().st_mtime_ns
    run_precompute(root=umi_root, output_dir=output, visualize_only=True, show_progress=False)
    assert csv_path.stat().st_mtime_ns == csv_mtime
    assert {p: p.stat().st_mtime_ns for p in videos} == mtimes
    run_precompute(
        root=umi_root, output_dir=output, visualize_only=True, show_progress=False, fallback_stage_count=3
    )
    assert json.loads(csv_path.with_suffix(".stages.json").read_text())["stage_count"] == 3
    from lerobot.data_platform.task_text import cached_subtask_names

    max_stage, names = cached_subtask_names("Pick up the object.", static, 0)
    assert max_stage == 2 and names[0] == "Stage 1/3" and names[2] == "Stage 3/3"
    assert {p: p.stat().st_mtime_ns for p in videos} == mtimes
    assert {p: p.read_bytes() for p in umi_root.rglob("*") if p.is_file()} == before


def test_umi_local_routes_and_raw_analysis(umi_root, tmp_path, monkeypatch):
    import time

    from flask import Flask

    from lerobot.data_platform import viewer

    captured = {}
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: captured.update(app=self))
    static = tmp_path / "console/static"
    static.mkdir(parents=True)
    viewer.run_server(
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
        datasets_root=tmp_path,
    )
    client = captured["app"].test_client()
    registered = client.post("/api/datasets/register", json={"root": str(umi_root)})
    assert registered.status_code == 200
    dataset = registered.get_json()["dataset"]
    assert dataset["robot_type"] == "UMI"
    assert dataset["data_version"] is None
    for op in ("standardize", "convert_action", "smooth_action", "quality_flags", "value_edit"):
        response = client.post(
            f"/api/preprocess/{op}/start", json={"dataset_key": "local/umi", "options": {}}
        )
        assert response.status_code == 400, response.get_json()
    response = client.post(
        "/api/precompute/start", json={"dataset_key": "local/umi", "options": {"data_version": "DVT2"}}
    )
    assert response.status_code == 400
    assert not client.get("/api/jobs").get_json()["jobs"]
    response = client.post(
        "/api/precompute/start",
        json={
            "dataset_key": "local/umi",
            "options": {"prepare_workers": 1, "force_recompute_stage": True, "fallback_stage_count": 3},
        },
    )
    assert response.status_code == 200, response.get_json()
    job_id = response.get_json()["job"]["id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] in {"done", "error"}:
            break
        time.sleep(0.02)
    assert job["status"] == "done", job.get("error")
    page = client.get("/local/umi/episode_0?direct=1")
    assert page.status_code == 200
    assert "dataVersion: null" in page.get_data(as_text=True)
    csv_text = client.get("/local/umi/episode_0/data.csv").get_data(as_text=True)
    assert "head_pose.x" in csv_text
    rows = list(csv.DictReader(csv_text.splitlines()))
    assert float(rows[0]["left_gripper_pos"]) == pytest.approx(0.0672, rel=1e-6)
    stages = json.loads(
        (umi_root.parent / "vis/local_vis_umi/static/csv/episode_000000_ds1.stages.json").read_text()
    )
    assert stages["stage_count"] == 3
    assert stages["stage_profile"] == "time_equal_v1"


def test_umi_agent_and_remote_protocol(umi_root, tmp_path):
    from dataclasses import replace

    from lerobot.data_platform.agent import _dataset_payload
    from tests.datasets.test_control_plane import _app, _bootstrap, _store
    from tests.datasets.test_data_platform_agent import _agent
    from tests.datasets.test_remote_dataset_merge import _HttpClient

    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    assert _bootstrap(client).status_code == 200
    agent = _agent(tmp_path, tmp_path, tmp_path)
    capabilities = agent.capabilities()
    token, node = store.enroll_node(
        name="umi-test",
        hostname="umi-test",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        capabilities={**capabilities, "data_profile_protocol": 1},
        enrollment_token="enroll",
        expected_token="enroll",
    )
    agent.state = replace(agent.state, node_id=node["node_id"], node_token=token)
    location = store.sync_locations(node["node_id"], [_dataset_payload(umi_root, node_name="umi-test")])[0]
    url = f"/api/control/locations/{location['location_id']}"
    assert client.post(url + "/viewer-jobs", json={}).status_code == 409
    old_payload = _dataset_payload(umi_root, node_name="umi-test")
    old_payload["metadata"] = {
        key: value
        for key, value in old_payload["metadata"].items()
        if key in {"features", "fps", "total_episodes", "total_frames", "codebase_version"}
    }
    store.sync_locations(node["node_id"], [old_payload])
    assert client.post(url + "/viewer-jobs", json={}).status_code == 409
    store.sync_locations(node["node_id"], [_dataset_payload(umi_root, node_name="umi-test")])
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/api/agents/heartbeat", headers=headers, json={"capabilities": capabilities})
    assert response.status_code == 200
    assert client.post(url + "/preprocess-jobs", json={"op": "standardize"}).status_code == 400
    with pytest.raises(ValueError, match="DVT"):
        agent.execute_job({"operation": "preprocess.standardize", "location": location})
    response = client.post(
        url + "/preprocess-jobs",
        json={"op": "split", "options": {"episode_range": "1:2", "out_root": str(tmp_path / "split_output")}},
    )
    assert response.status_code == 202, response.get_json()
    agent.client = _HttpClient(client, token)
    client.post(
        "/api/agents/heartbeat",
        headers=headers,
        json={"capabilities": {**capabilities, "data_profile_protocol": 1}},
    )
    assert client.post("/api/agents/jobs/claim", headers=headers, json={}).get_json()["job"] is None
    client.post("/api/agents/heartbeat", headers=headers, json={"capabilities": capabilities})
    job = client.post("/api/agents/jobs/claim", headers=headers, json={}).get_json()["job"]
    result = agent.execute_job(job)
    assert result["dataset_location"]["metadata"]["data_version"] is None
    assert result["viewer_cache"]["uploaded_files"] > 0
    response = client.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        headers=headers,
        json={"status": "done", "result": result},
    )
    assert response.status_code == 200, response.get_json()


def test_umi_remote_stage_preview_forwards_count_and_force(umi_root, tmp_path):
    from dataclasses import replace

    from lerobot.data_platform.agent import _dataset_payload
    from tests.datasets.test_control_plane import _app, _bootstrap, _store
    from tests.datasets.test_data_platform_agent import _agent
    from tests.datasets.test_remote_dataset_merge import _HttpClient

    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    _bootstrap(client)
    agent = _agent(tmp_path, tmp_path, tmp_path)
    token, node = store.enroll_node(
        name="umi-preview",
        hostname="umi-preview",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        capabilities=agent.capabilities(),
        enrollment_token="enroll",
        expected_token="enroll",
    )
    agent.state = replace(agent.state, node_id=node["node_id"], node_token=token)
    agent.client = _HttpClient(client, token)
    location = store.sync_locations(node["node_id"], [_dataset_payload(umi_root, node_name="umi-preview")])[0]
    url = f"/api/control/locations/{location['location_id']}/viewer-jobs"
    assert client.post(url, json={"fallback_stage_count": 1}).status_code == 400
    response = client.post(
        url, json={"force_recompute_stage": True, "fallback_stage_count": 3, "prepare_videos": False}
    )
    assert response.status_code == 202, response.get_json()
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post("/api/agents/jobs/claim", headers=headers, json={}).get_json()["job"]
    assert job["options"]["fallback_stage_count"] == 3 and job["options"]["force_recompute_stage"]
    result = agent.execute_job(job)
    static = Path(result["output_dir"]) / "static"
    assert json.loads((static / "csv/episode_000000_ds1.stages.json").read_text())["stage_count"] == 3
    assert json.loads((static / "viewer_manifest.json").read_text())["stage_profile"] == "time_equal_v1"


def test_unknown_robot_and_legacy_profile_compatibility(umi_root):
    from lerobot.data_platform.precompute.data_profile import (
        profile_from_data_version,
        profile_from_info,
        write_data_profile,
    )

    info = json.loads((umi_root / "meta/info.json").read_text())
    info["robot_type"] = "new_robot"
    profile = profile_from_info(info)
    assert profile.robot_profile == "unknown" and profile.legacy_data_version is None
    info["robot_type"] = "h10w"
    (umi_root / "meta/info.json").write_text(json.dumps(info))
    legacy = profile_from_data_version("DVT2", {}, resolution_source="explicit", confirmed=True)
    write_data_profile(umi_root, legacy)
    assert resolve_data_profile(umi_root).legacy_data_version == "DVT2"
    payload = legacy.to_dict()
    payload["schema_version"] = 1
    (umi_root / "meta/data_profile.json").write_text(json.dumps(payload))
    assert resolve_data_profile(umi_root).legacy_data_version == "DVT2"


def test_umi_rejects_semantic_merge_mismatch_and_dvt_override(umi_root, tmp_path):
    import shutil

    from lerobot.data_platform.precompute.preprocess.dataset_merge import run_merge, validate_merge_sources
    from lerobot.data_platform.precompute.preprocess.standardize import run_standardize_dataset

    other = tmp_path / "other"
    shutil.copytree(umi_root, other)
    with pytest.raises(ValueError, match="strict"):
        run_merge([umi_root, other], tmp_path / "output", dimension_policy="min", dry_run=True)
    info = json.loads((other / "meta/info.json").read_text())
    info["features"]["left_arm_pose"]["names"][0:2] = ["y", "x"]
    (other / "meta/info.json").write_text(json.dumps(info))
    with pytest.raises(ValueError, match="feature"):
        validate_merge_sources([umi_root, other])
    with pytest.raises(ValueError, match="DVT"):
        run_standardize_dataset(umi_root, tmp_path / "standardized", data_version="DVT2")
    assert not (tmp_path / "standardized").exists()

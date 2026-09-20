"""Single-episode isolation, strict result validation, and authenticated remote review."""

import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

from lerobot.data_platform import viewer
from lerobot.data_platform.routes.temporal_caption import register_temporal_caption_routes
from lerobot.data_platform.temporal_caption import artifact_key, prepare, sample_frames, validate_result
from tests.datasets.test_analysis_metadata import _remote_context
from tests.datasets.test_control_plane import _bootstrap


def result():
    return {
        "summary": "Two observed movements",
        "limitations": ["Sampled observations"],
        "segments": [
            {
                "start_frame": a,
                "end_frame": b,
                "caption": "移动",
                "caption_en": "Move",
                "evidence": "Visible displacement",
                "confidence": "medium",
                "uncertainty": "Contact occluded",
            }
            for a, b in [(0, 6), (6, 12)]
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"start_frame": 1},
        {"end_frame": 7},
        {"end_frame": 0},
        {"end_frame": 6.0},
        {"start_frame": False},
        {"caption": ""},
        {"confidence": "certain"},
    ],
)
def test_reject_invalid_or_gapped_model_output(change):
    value = result()
    value["segments"][0].update(change)
    with pytest.raises(ValueError):
        validate_result(value, 12)


def test_boundary_sampling_includes_endpoints_and_densifies():
    initial = sample_frames(656, 30)
    refined = sample_frames(656, 30, {"segments": [{"start_frame": 0}, {"start_frame": 100}]})
    assert initial[0] == 0 and initial[-1] == 655
    assert set(initial) < set(refined)
    assert 98 in refined and 102 in refined
    assert len(refined) == len(set(refined))


def test_export_reads_only_selected_episode_from_shared_v3_shard(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    root = tmp_path / "source"
    (root / "meta/episodes").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    (root / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "fps": 30,
                "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                "features": {"head_image": {"dtype": "image"}, "left_gripper_pos": {"dtype": "float32"}},
            }
        )
    )
    pq.write_table(pa.Table.from_pylist([{"task_index": 0, "task": "Move"}]), root / "meta/tasks.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "episode_index": ep,
                    "tasks": ["Move"],
                    "length": 2,
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                }
                for ep in [0, 1]
            ]
        ),
        root / "meta/episodes/file-000.parquet",
    )
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "episode_index": ep,
                    "frame_index": frame,
                    "timestamp": frame / 30,
                    "left_gripper_pos": float(ep),
                    "head_image": {"bytes": buf.getvalue(), "path": None},
                }
                for ep in [0, 1]
                for frame in [0, 1]
            ]
        ),
        root / "data/chunk-000/file-000.parquet",
    )
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    output = tmp_path / "export"
    prepare(root, 1, output)
    meta = json.loads((output / "episode.json").read_text())
    assert meta["episode_index"] == 1 and meta["frame_count"] == 2
    assert all(row["left_gripper_pos"] == 1 for row in meta["signals"])
    assert len(list((output / "frames").glob("*.jpg"))) == 2
    assert (output / "video.mp4").stat().st_size > 0
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        prepare(root, 1, output)
    with pytest.raises(ValueError, match="outside"):
        prepare(root, 1, root / "export")


def test_remote_review_without_viewer_requires_auth_and_is_scoped(tmp_path, monkeypatch):
    app, control, location = _remote_context(tmp_path)
    root = tmp_path / "captions"
    monkeypatch.setenv("DATA_PLATFORM_TEMPORAL_CAPTION_ROOT", str(root))
    ctx = SimpleNamespace(control_plane_store=control)
    register_temporal_caption_routes(app, ctx)
    run = root / artifact_key(location["dataset_key"]) / "trial"
    run.mkdir(parents=True)
    artifact = {
        "dataset_key": location["dataset_key"],
        "variant": "refined",
        "result": result(),
        "frame_count": 12,
        "fps": 30,
        "duration_s": 0.4,
        "episode_index": 0,
        "model": "test",
        "created_at": 1,
    }
    (run / "refined.json").write_text(json.dumps(artifact))
    (run / "video.mp4").write_bytes(b"fake-video-bytes")
    client = app.test_client()
    url = f"/api/control/locations/{location['location_id']}/temporal-caption"
    assert client.get(url).status_code == 401
    assert _bootstrap(client).status_code == 200
    control.register_user(username="caption-viewer", password="viewer-password", display_name="Viewer")
    client.post("/api/auth/logout", json={})
    assert (
        client.post(
            "/api/auth/login", json={"username": "caption-viewer", "password": "viewer-password"}
        ).status_code
        == 200
    )
    assert client.get(f"/remote/{location['location_id']}/temporal-caption").status_code == 200
    assert client.get(url).get_json()["runs"][0]["variant"] == "refined"
    assert client.get(url + "/trial/refined").get_json()["result"] == result()
    response = client.get(url + "/trial/refined/video", headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and response.data == b"fake"
    assert client.post(url, json={}).status_code in {403, 405}
    assert client.get(url + "/trial/secrets").status_code == 404
    assert client.get("/api/control/locations/missing/temporal-caption").status_code == 404
    artifact["dataset_key"] = "another/dataset"
    (run / "refined.json").write_text(json.dumps(artifact))
    assert client.get(url + "/trial/refined").status_code == 404
    assert not control.list_jobs()


@pytest.mark.parametrize("mode, status", [("full", 200), ("visualize", 404)])
def test_remote_caption_registration_respects_console_mode(tmp_path, monkeypatch, mode, status):
    _, _, location = _remote_context(tmp_path)
    monkeypatch.setenv("DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-secret")
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
        static_folder=tmp_path / "static",
        template_folder=Path(viewer.__file__).parent / "templates",
        database_url=f"sqlite:///{tmp_path / 'control-plane.db'}",
        start_server=False,
        console_mode=mode,
    )
    client = app.test_client()
    assert _bootstrap(client).status_code == 200
    assert client.get(f"/remote/{location['location_id']}/temporal-caption").status_code == status


def test_player_boundary_rounding_and_loop_use_episode_frames():
    import subprocess

    if not shutil.which("node"):
        pytest.skip("Node required for player check")
    source = (Path(viewer.__file__).parent / "templates/visualize_dataset_temporal_caption.html").read_text()
    function = source.split("function syncPlayback() {", 1)[1].split("$('video').addEventListener", 1)[0]
    script = (
        "const assert = require('node:assert/strict');\n"
        + """
let artifact = {fps:30, frame_count:656, duration_s:656/30, result:{segments:[
    {start_frame:0,end_frame:248}, {start_frame:248,end_frame:360}, {start_frame:360,end_frame:656}]}};
let active=0, selectedLoop=1;
const elements = {video:{currentTime:8.266666,seeking:false}, loop:{checked:false}, clock:{textContent:''}};
const $ = id => elements[id]; const highlight = () => {};
const document = {getElementById: () => null};
"""
        + "function syncPlayback() {"
        + function
        + """
syncPlayback(); assert.equal(active,1);
elements.loop.checked=true; elements.video.currentTime=13;
syncPlayback(); assert.equal(elements.video.currentTime,248/30);
elements.video.seeking=true; elements.video.currentTime=13;
syncPlayback(); assert.equal(elements.video.currentTime,13);
"""
    )
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def demo_result():
    return {
        "episode_caption": {"en": "Move an object", "zh": "移动物体"},
        "scene": {"objects": ["object"]},
        "segments": [
            {
                "start_frame": a,
                "end_frame": b,
                "caption_zh": "移动",
                "caption_en": "Move",
                "evidence": "Visible motion",
                "phase": "transfer",
                "active_arm": "both",
            }
            for a, b in [(0, 5), (6, 11)]
        ],
        "quality": {"instruction_followed": False, "success": False, "confidence": 0.8},
        "_meta": {"episode": 25, "num_frames": 12, "fps": 30, "model": "test"},
    }


def test_demo_normalization_preserves_identity_quality_and_inclusive_coverage():
    from lerobot.data_platform.temporal_caption_demo import normalize_demo, scheme_info

    source = demo_result()
    normalized = normalize_demo(source, {"episode_index": 25, "num_frames": 12, "fps": 30}, "n/d")
    assert normalized["episode_index"] == 25
    assert scheme_info(normalized)["name"] == "Video + Events"
    assert normalized["result"]["segments"][0]["end_frame"] == 6
    assert normalized["result"]["segments"][-1]["end_frame"] == 12
    assert normalized["result"]["segments"][0]["confidence"] == "not_reported"
    assert normalized["result"]["quality"] == source["quality"]
    assert source["segments"][0]["end_frame"] == 5
    with pytest.raises(ValueError, match="disagree"):
        normalize_demo(source, {"episode_index": 0, "num_frames": 12, "fps": 30}, "n/d")
    source["segments"][1]["start_frame"] = 7
    with pytest.raises(ValueError, match="contiguously"):
        normalize_demo(source, {"episode_index": 25, "num_frames": 12, "fps": 30}, "n/d")


def test_review_context_uses_elapsed_frames_and_does_not_expose_source_paths(tmp_path):
    from lerobot.data_platform.temporal_caption_demo import review_context

    (tmp_path / "episode.json").write_text(
        json.dumps(
            {
                "fps": 30,
                "task_hint": ["Move"],
                "source_parquet": "/private/data.parquet",
                "signals": [
                    {"frame": 0, "left_arm_pose": [0, 0, 0], "left_gripper_pos": 0},
                    {"frame": 3, "left_arm_pose": [0.1, 0, 0], "left_gripper_pos": 90},
                ],
            }
        )
    )
    result = review_context(tmp_path)
    assert result["signals"][1]["left_speed"] == pytest.approx(1)
    assert result["signals"][1]["t"] == pytest.approx(0.1)
    assert result["task"] == "Move" and "private" not in json.dumps(result)


def test_video_event_scheme_runs_one_episode_and_uses_temporal_input(tmp_path, monkeypatch):
    from lerobot.data_platform import qwen
    from lerobot.data_platform.temporal_caption_demo import infer_video_events

    bundle = tmp_path / "bundle"
    (bundle / "frames").mkdir(parents=True)
    (bundle / "video.mp4").write_bytes(b"video")
    for i in range(12):
        (bundle / "frames" / f"{i:06d}.jpg").write_bytes(b"image")
    (bundle / "episode.json").write_text(
        json.dumps(
            {
                "fps": 30,
                "frame_count": 12,
                "episode_index": 25,
                "task_hint": ["Move"],
                "signals": [
                    {
                        "frame": i,
                        "left_arm_pose": [i / 30, 0, 0],
                        "right_arm_pose": [0, 0, 0],
                        "left_gripper_pos": 0 if i < 6 else 90,
                        "right_gripper_pos": 0,
                    }
                    for i in range(12)
                ],
            }
        )
    )
    requests = []

    def post(payload):
        requests.append(payload)
        return {"choices": [{"message": {"content": json.dumps(demo_result())}}]}

    monkeypatch.setattr(qwen, "QwenClient", lambda **kwargs: SimpleNamespace(post_chat_completion=post))
    output = tmp_path / "result"
    infer_video_events(bundle, output, "n/d", "test-model")
    assert len(requests) == 1
    visual = requests[0]["messages"][0]["content"][0]
    assert visual["type"] == "video" and len(visual["video"]) == 12
    value = json.loads((output / "caption.json").read_text())
    assert value["scheme_id"] == "video_events" and value["episode_index"] == 25
    assert value["result"]["segments"][-1]["end_frame"] == 12
    context = json.loads((output / "episode.json").read_text())
    assert context["gripper_events"] == [{"frame": 6, "side": "left", "event": "gripper_rise"}]
    with pytest.raises(FileExistsError):
        infer_video_events(bundle, output, "n/d", "test-model")
    assert len(requests) == 1

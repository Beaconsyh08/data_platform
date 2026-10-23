"""FLORA evidence integrity, executor selection, and attempt-scoped publication."""

import copy
import json

import pytest

from lerobot.data_platform.routes.temporal_caption_jobs import caption_scheme_status
from lerobot.data_platform.temporal_caption import prepare
from lerobot.data_platform.temporal_caption_flora import (
    build_artifact,
    camera_roles,
    disagreement_intervals,
    review_partition,
    validate_flora,
)
from lerobot.data_platform.temporal_caption_jobs import publish_run, validate_options, validate_source
from tests.datasets import test_temporal_caption_jobs as job_fixtures


@pytest.fixture
def remote(tmp_path, monkeypatch):
    value = job_fixtures.remote.__wrapped__(tmp_path, monkeypatch)
    client, store, node, token, location, root = value
    location = store.sync_locations(
        node["node_id"],
        [
            {
                **location,
                "metadata": {
                    **location["metadata"],
                    "total_episodes": 1,
                    "features": {name: {"dtype": "image"} for name in ["left_wrist", "scene", "right_wrist"]},
                },
            }
        ],
    )[0]
    return client, store, node, token, location, root


def flora_artifact(key="node/source"):
    native = {
        "segments": [
            {"start_s": 0, "end_s": 0.1, "caption": "Reach", "status": "complete"},
            {"start_s": 0.15, "end_s": 0.3, "caption": "Grasp", "status": "complete"},
            {"start_s": 0.25, "end_s": 0.4, "caption": "Lift", "status": "complete"},
        ]
    }
    windows = [
        {"scores": [1] * 8 + [3] * 4, "output": "Window one: failure then normal"},
        {"scores": [0] * 3 + [2] * 5 + [3] * 4, "output": "Window two: suboptimal then normal"},
    ]
    reward = {
        "terminal": {"score": 4, "rationale": "Completed after retry"},
        "windows": windows,
        "merged_scores": [1] * 8 + [3] * 4,
        "merged_source_window": [0] * 12,
        "disagreement_intervals": disagreement_intervals(windows, 12),
        "human_reviewed": False,
        "alignment": "Test alignment",
    }
    return build_artifact({"episode_index": 0, "frame_count": 12, "fps": 30}, key, native, reward, [])


def write_run(path, key="node/source"):
    path.mkdir()
    (path / "caption.json").write_text(json.dumps(flora_artifact(key)))
    (path / "episode.json").write_text(
        json.dumps(
            {"episode_index": 0, "frame_count": 12, "fps": 30, "include_signals": False, "signals": []}
        )
    )
    (path / "video.mp4").write_bytes(b"review-video")
    return path


def test_native_gap_overlap_and_ceil_are_preserved():
    artifact = flora_artifact()
    segments = artifact["result"]["segments"]
    assert [(s["start_frame"], s["end_frame"]) for s in segments] == [(0, 3), (3, 5), (5, 8), (8, 9), (9, 12)]
    assert segments[1]["caption"] == "Unlabeled interval"
    assert segments[3]["native_segment_indices"] == [1, 2]
    assert artifact["native_segments"][1]["start_s"] == 0.15
    assert all(s["confidence"] == "not_reported" for s in segments)
    assert artifact["reward"]["disagreement_intervals"] == [{"start_frame": 3, "end_frame": 8}]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: a["reward"]["merged_scores"].pop(),
        lambda a: a["reward"]["windows"][0]["scores"].__setitem__(0, True),
        lambda a: a["reward"]["disagreement_intervals"].clear(),
        lambda a: a["reward"]["merged_source_window"].__setitem__(0, 20),
        lambda a: a["reward"]["terminal"].update(score=6),
        lambda a: a["reward"].update(human_reviewed=True),
        lambda a: a.update(include_signals=True),
        lambda a: a["native_segments"][0].update(end_s=float("nan")),
        lambda a: a["result"]["segments"][0].update(caption="Invented action"),
    ],
)
def test_reject_inconsistent_rewards_or_native_captions(mutation):
    artifact = flora_artifact()
    mutation(artifact)
    with pytest.raises(ValueError):
        validate_flora(artifact)


def test_reject_incomplete_native_results():
    native = copy.deepcopy(flora_artifact()["native_segments"])
    native[0]["status"] = "failed"
    with pytest.raises(ValueError, match="incomplete"):
        review_partition(native, 12, 30)


def test_visual_only_options_and_camera_identity():
    assert validate_options({"episode_index": 0, "scheme": "flora"})["include_signals"] is False
    with pytest.raises(ValueError, match="visual only"):
        validate_options({"episode_index": 0, "scheme": "flora", "include_signals": True})
    assert camera_roles(["image_right", "scene", "image_left"])["main"] == "scene"
    assert camera_roles(["right_wrist", "left_wrist"]) == {"left": "left_wrist", "right": "right_wrist"}
    for names in (["a", "b", "c"], ["left", "scene"], ["left_right", "scene", "other"]):
        with pytest.raises(ValueError):
            camera_roles(names)


def test_flora_readiness_does_not_require_dashscope():
    capabilities = {
        "caption_flora_protocol": 1,
        "caption_flora_configured": True,
        "caption_configured": False,
        "caption_schemes": ["flora", "video_events"],
    }
    status = {s["id"]: s for s in caption_scheme_status(capabilities, None, {"role": "operator"})}
    assert status["flora"]["can_submit"]
    assert not status["video_events"]["can_submit"]
    assert not any(s["can_submit"] for s in caption_scheme_status(capabilities, None, {"role": "viewer"}))
    capabilities["caption_flora_protocol"] = 0
    assert not next(
        s for s in caption_scheme_status(capabilities, None, {"role": "operator"}) if s["id"] == "flora"
    )["can_submit"]


def test_flora_queue_publish_read_and_tamper_rejection(remote, tmp_path):
    client, store, node, token, location, root = remote
    current = next(n for n in store.list_nodes() if n["node_id"] == node["node_id"])
    store.heartbeat(
        node["node_id"],
        capabilities={
            **current["capabilities"],
            "caption_configured": False,
            "caption_flora_protocol": 1,
            "caption_flora_configured": True,
            "caption_schemes": ["flora"],
            "caption_signal_options": True,
        },
    )
    api = f"/api/control/locations/{location['location_id']}/temporal-caption"
    response = client.post(
        api + "/jobs", json={"episode_index": 0, "scheme": "flora"}, headers={"Idempotency-Key": "flora"}
    )
    assert response.status_code == 202, response.get_json()
    job = response.get_json()["job"]
    assert job["options"]["include_signals"] is False
    claim = store.claim_job(node["node_id"], worker_instance_id="flora-worker")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Job-Attempt": claim["execution"]["attempt_id"],
        "X-Job-Credential": claim["execution"]["credential"],
    }
    run = write_run(tmp_path / "run")
    endpoint = f"/api/agents/jobs/{job['job_id']}"
    for path in run.iterdir():
        assert (
            client.put(
                endpoint + f"/caption-artifacts/{path.name}", headers=headers, data=path.read_bytes()
            ).status_code
            == 200
        )
    completed = client.post(endpoint + "/complete", headers=headers, json={"status": "done"})
    assert completed.status_code == 200, completed.get_json()
    response = client.get(api + f"/{job['job_id']}/caption")
    assert response.status_code == 200
    assert response.get_json()["reward"]["disagreement_intervals"] == [{"start_frame": 3, "end_frame": 8}]
    saved = next(root.glob("*/*/caption.json"))
    value = json.loads(saved.read_text())
    value["reward"]["disagreement_intervals"] = []
    saved.write_text(json.dumps(value))
    assert client.get(api + f"/{job['job_id']}/caption").status_code == 422


def test_invalid_flora_never_publishes(tmp_path):
    run = write_run(tmp_path / "run")
    value = flora_artifact()
    value["reward"]["merged_scores"] = []
    (run / "caption.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        publish_run(run, tmp_path / "published", "node/source", "run")
    assert not (tmp_path / "published").exists()


def test_export_native_camera_video_keeps_resolution_and_source(tmp_path):
    import av

    from tests.datasets.test_temporal_caption import (
        test_export_reads_only_selected_episode_from_shared_v3_shard,
    )

    test_export_reads_only_selected_episode_from_shared_v3_shard(tmp_path)
    source = tmp_path / "source"
    before = {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    output = tmp_path / "flora-export"
    prepare(source, 1, output, export_camera_videos=True)
    with av.open(str(output / "camera-videos/0.mp4")) as video:
        frames = list(video.decode(video=0))
    assert len(frames) == 2
    assert (frames[0].width, frames[0].height) == (8, 8)
    assert before == {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_browser_flora_review_seek_switch_mobile_and_readonly(remote, tmp_path):
    """Exercise real HTML/JS and authenticated routes, not template substring checks."""
    import shutil
    import subprocess
    import threading

    from werkzeug.serving import make_server

    from tests.datasets.test_temporal_caption_jobs import make_hybrid_run

    playwright = pytest.importorskip("playwright.sync_api")
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    client, store, node, _, location, root = remote
    run = write_run(tmp_path / "browser-run")
    (run / "video.mp4").unlink()
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=192x64:r=30",
            "-frames:v",
            "12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(run / "video.mp4"),
        ],
        check=True,
    )
    publish_run(run, root, "node/source", "flora")
    publish_run(make_hybrid_run(tmp_path / "legacy", key="node/source"), root, "node/source", "legacy")
    store.register_user(username="flora-reader", password="reader-password", display_name="Reader")
    current = next(n for n in store.list_nodes() if n["node_id"] == node["node_id"])
    store.heartbeat(
        node["node_id"],
        capabilities={
            **current["capabilities"],
            "caption_flora_protocol": 1,
            "caption_flora_configured": True,
            "caption_schemes": ["flora"],
            "caption_signal_options": True,
        },
    )
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(
                headless=True, executable_path=shutil.which("google-chrome") or None
            )
            context = browser.new_context(viewport={"width": 1360, "height": 1000})
            assert context.request.post(
                origin + "/api/auth/login", data={"username": "flora-reader", "password": "reader-password"}
            ).ok
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            route = f"/remote/{location['location_id']}/temporal-caption"
            page.goto(origin + route + "/review?scheme=flora")
            page.locator("#reward-review").wait_for(state="visible")
            page.wait_for_function("document.querySelector('#video').readyState >= 1")
            assert page.locator("#reward-tracks canvas").count() == 3
            page.locator("#reward-conflicts button").click()
            page.wait_for_function(
                "document.querySelector('#reward-overlay').textContent.includes('Windows disagree')"
            )
            assert abs(page.locator("#video").evaluate("v=>v.currentTime") - 0.1) < 0.04
            page.locator("#reward-tracks canvas").first.focus()
            page.keyboard.press("ArrowRight")
            page.wait_for_function("document.querySelector('#video').currentTime > .12")
            page.select_option("#scheme", "hybrid_windows")
            page.locator("#reward-review").wait_for(state="hidden")
            assert page.locator("#reward-tracks canvas").count() == 0
            page.select_option("#scheme", "flora")
            page.locator("#reward-review").wait_for(state="visible")
            page.route(
                "**/legacy/caption",
                lambda route: route.fulfill(status=503, json={"error": "Temporarily unavailable"}),
            )
            page.select_option("#scheme", "hybrid_windows")
            page.wait_for_function("document.querySelector('#status').textContent.includes('503')")
            assert page.locator("#reward-review").is_hidden()
            assert page.locator("#reward-tracks canvas").count() == 0
            page.unroute("**/legacy/caption")
            page.goto(origin + route + "/review?scheme=video_events")
            page.wait_for_function(
                "document.querySelector('#status').textContent.includes('No saved results')"
            )
            assert page.locator("#review").is_hidden()
            page.goto(origin + route + "/review?scheme=flora")
            page.locator("#reward-review").wait_for(state="visible")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.goto(origin + route)
            page.locator('[data-caption-id="job-scheme"] option[value="flora"]').wait_for(state="attached")
            page.select_option('[data-caption-id="job-scheme"]', "flora")
            assert page.locator('[data-caption-id="include-signals"]').is_disabled()
            assert not page.locator('[data-caption-id="include-signals"]').is_checked()
            assert page.locator('[data-caption-id="job-submit"]').is_disabled()
            assert "Read-only" in page.locator('[data-caption-id="job-readiness"]').inner_text()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_parent_death_stops_worker_and_its_children(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    pid_file = tmp_path / "pids.json"
    worker = (
        "import json,subprocess,sys,time;from pathlib import Path;"
        "from lerobot.data_platform.temporal_caption_flora_worker import guard_parent;"
        "guard_parent();"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"Path({str(pid_file)!r}).write_text(json.dumps([__import__('os').getpid(),child.pid]));"
        "time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,os,time;"
        f"subprocess.Popen([sys.executable,'-c',{worker!r}],"
        "env={**os.environ,'FLORA_PARENT_PID':str(os.getpid())},start_new_session=True);time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    pids = []

    def alive(pid):
        path = Path(f"/proc/{pid}/stat")
        return path.exists() and path.read_text().split(")", 1)[1].split()[0] != "Z"

    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = json.loads(pid_file.read_text())
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(alive(pid) for pid in pids)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if pids and any(alive(pid) for pid in pids):
            os.killpg(pids[0], signal.SIGKILL)


@pytest.mark.parametrize("video_backed", [False, True])
def test_executor_exports_visual_only_and_publishes_without_api_key(tmp_path, monkeypatch, video_backed):
    import shutil
    from types import SimpleNamespace

    import pyarrow as pa
    import pyarrow.parquet as pq

    from lerobot.data_platform import qwen, temporal_caption_flora
    from lerobot.data_platform.temporal_caption_jobs import execute_caption_job, validate_run
    from tests.datasets.test_temporal_caption import (
        test_export_reads_only_selected_episode_from_shared_v3_shard,
    )

    test_export_reads_only_selected_episode_from_shared_v3_shard(tmp_path)
    source = tmp_path / "source"
    info = json.loads((source / "meta/info.json").read_text())
    for name in ("left_wrist", "right_wrist"):
        info["features"][name] = {"dtype": "image"}
    (source / "meta/info.json").write_text(json.dumps(info))
    parquet = source / "data/chunk-000/file-000.parquet"
    rows = pq.read_table(parquet).to_pylist()
    for row in rows:
        row.update(left_wrist=row["head_image"], right_wrist=row["head_image"])
    pq.write_table(pa.Table.from_pylist(rows), parquet)
    if video_backed:
        import subprocess

        info["video_path"] = "videos/{video_key}.mp4"
        episodes_path = source / "meta/episodes/file-000.parquet"
        episodes = pq.read_table(episodes_path).to_pylist()
        (source / "videos").mkdir()
        cameras = ["head_image", "left_wrist", "right_wrist"]
        for camera in cameras:
            info["features"][camera]["dtype"] = "video"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=16x16:r=30",
                    "-frames:v",
                    "4",
                    "-c:v",
                    "libx264",
                    str(source / "videos" / f"{camera}.mp4"),
                ],
                check=True,
            )
            for ep in episodes:
                ep.update(
                    {
                        f"videos/{camera}/chunk_index": 0,
                        f"videos/{camera}/file_index": 0,
                        f"videos/{camera}/from_timestamp": ep["episode_index"] * 2 / 30,
                        f"videos/{camera}/to_timestamp": (ep["episode_index"] + 1) * 2 / 30,
                    }
                )
        (source / "meta/info.json").write_text(json.dumps(info))
        pq.write_table(pa.Table.from_pylist(episodes), episodes_path)
        pq.write_table(pq.read_table(parquet).drop(cameras), parquet)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(
        qwen, "QwenClient", lambda **kwargs: pytest.fail("FLORA must not construct an API client")
    )
    monkeypatch.setattr(temporal_caption_flora.FloraRuntime, "load", lambda: None)

    def infer(bundle, output, key, model, **kwargs):
        meta = json.loads((bundle / "episode.json").read_text())
        assert meta["include_signals"] is False
        assert all(set(row) == {"frame"} for row in meta["signals"])
        assert len(list((bundle / "camera-videos").glob("*.mp4"))) == 3
        count = meta["frame_count"]
        windows = [{"scores": [3] * count, "output": "All normal"}]
        value = build_artifact(
            meta,
            key,
            {
                "segments": [
                    {"start_s": 0, "end_s": count / meta["fps"], "caption": "Move", "status": "complete"}
                ]
            },
            {
                "terminal": {"score": 4, "rationale": "Completed"},
                "windows": windows,
                "merged_scores": [3] * count,
                "merged_source_window": [0] * count,
                "disagreement_intervals": [],
                "human_reviewed": False,
            },
            [],
        )
        output.mkdir()
        (output / "caption.json").write_text(json.dumps(value))
        shutil.copyfile(bundle / "episode.json", output / "episode.json")
        shutil.copyfile(bundle / "video.mp4", output / "video.mp4")

    monkeypatch.setattr(temporal_caption_flora, "infer_flora", infer)
    uploaded = tmp_path / "uploaded"
    uploaded.mkdir()
    client = SimpleNamespace(
        event=lambda *args: None,
        upload_artifact=lambda state, job, name, path, **kwargs: shutil.copyfile(path, uploaded / name),
    )
    agent = SimpleNamespace(client=client, state=None, state_path=tmp_path / "state.json")
    options = {"episode_index": 1, "scheme": "flora", "include_signals": False}
    result = execute_caption_job(
        agent, {"job_id": "flora", "options": options}, source, {"dataset_key": "node/source"}
    )
    assert result["scheme"] == "flora"
    records = validate_run(uploaded, key="node/source", options=options)
    assert len(records) == 1 and records[0]["source_fingerprint"]


def test_video_shard_export_uses_selected_episode_range(tmp_path):
    import av
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    from tests.datasets.test_temporal_caption import (
        test_export_reads_only_selected_episode_from_shared_v3_shard,
    )

    test_export_reads_only_selected_episode_from_shared_v3_shard(tmp_path)
    root = tmp_path / "source"
    info = json.loads((root / "meta/info.json").read_text())
    info["features"]["head_image"]["dtype"] = "video"
    info["video_path"] = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
    (root / "meta/info.json").write_text(json.dumps(info))
    path = root / "videos/head_image/chunk-000/file-000.mp4"
    path.parent.mkdir(parents=True)
    with av.open(str(path), "w") as out:
        stream = out.add_stream("libx264", rate=30)
        stream.width = stream.height = 16
        stream.pix_fmt = "yuv444p"
        stream.options = {"crf": "0"}
        for color in [(255, 0, 0)] * 2 + [(0, 255, 0)] * 2:
            frame = av.VideoFrame.from_ndarray(np.full((16, 16, 3), color, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    ep_path = root / "meta/episodes/file-000.parquet"
    episodes = pq.read_table(ep_path).to_pylist()
    for ep in episodes:
        ep.update(
            {
                "videos/head_image/chunk_index": 0,
                "videos/head_image/file_index": 0,
                "videos/head_image/from_timestamp": ep["episode_index"] * 2 / 30,
                "videos/head_image/to_timestamp": (ep["episode_index"] + 1) * 2 / 30,
            }
        )
    pq.write_table(pa.Table.from_pylist(episodes), ep_path)
    data = root / "data/chunk-000/file-000.parquet"
    pq.write_table(pq.read_table(data).drop(["head_image"]), data)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    output = tmp_path / "video-export"
    prepare(root, 1, output, export_camera_videos=True)
    with av.open(str(output / "camera-videos/0.mp4")) as video:
        frames = list(video.decode(video=0))
    assert len(frames) == 2
    assert all(f.to_ndarray(format="rgb24")[0, 0, 1] > 240 for f in frames)
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    episodes[1]["videos/head_image/to_timestamp"] = 3 / 30
    pq.write_table(pa.Table.from_pylist(episodes), ep_path)
    with pytest.raises(ValueError, match="range"):
        prepare(root, 1, tmp_path / "invalid-export", export_camera_videos=True)
    assert not (tmp_path / "invalid-export").exists()


def test_two_wrist_long_episode_chunks_keep_frame_count(tmp_path):
    import subprocess

    import av

    from lerobot.data_platform.temporal_caption_flora_worker import caption_inputs

    videos = {}
    for role, color in [("left", "red"), ("right", "blue")]:
        path = tmp_path / f"{role}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=16x16:r=4",
                "-frames:v",
                "485",
                "-c:v",
                "libx264",
                str(path),
            ],
            check=True,
        )
        videos[role] = str(path)
    inputs, instruction = caption_inputs(tmp_path, {"videos": videos, "frame_count": 485, "fps": 4})
    assert "left wrist" in instruction and "No scene camera" in instruction
    assert [offset for offset, _ in inputs] == [0, 242 / 4]
    counts = []
    for _, paths in inputs:
        assert list(paths) == ["main"]
        with av.open(str(paths["main"])) as video:
            frames = list(video.decode(video=0))
            counts.append(len(frames))
            assert frames[0].width == 32 and frames[0].height == 16
            image = frames[0].to_ndarray(format="rgb24")
            assert image[8, 8, 0] > 240 and image[8, 24, 2] > 240
    assert counts == [242, 243]


def test_long_native_partition_can_publish_more_than_forty_actions():
    segments = [{"start_s": i, "end_s": i + 1, "caption": "Move", "status": "complete"} for i in range(100)]
    assert len(review_partition(segments, 3000, 30)["segments"]) == 100


def test_video_dataset_passes_submission_and_export_validation():
    info = {
        "codebase_version": "v3.0",
        "features": {name: {"dtype": "video"} for name in ["left_wrist", "right_wrist"]},
    }
    validate_source(info)
    caps = {"caption_schemes": ["flora"], "caption_flora_protocol": 1, "caption_flora_configured": True}
    status = caption_scheme_status(caps, None, {"role": "operator"}, metadata=info)
    assert next(s for s in status if s["id"] == "flora")["can_submit"]

"""Dataset-local experiment discovery, scoped relay reads, and video byte ranges."""

import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from flask import request

from lerobot.data_platform.caption_reads import MAX_REPLY_BYTES, CaptionReadRelay
from lerobot.data_platform.dataset_captions import read_dataset_caption
from lerobot.data_platform.routes.temporal_caption import register_temporal_caption_routes
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_temporal_caption import result


def dataset(root, *, version="v2.1"):
    (root / "meta").mkdir(parents=True)
    (root / "data").mkdir()
    info = {"codebase_version": version, "fps": 30, "features": {"image": {"dtype": "image"}}}
    (root / "meta/info.json").write_text(json.dumps(info))
    if version == "v2.1":
        (root / "meta/episodes.jsonl").write_text(json.dumps({"episode_index": 0, "length": 12}) + "\n")
    else:
        (root / "meta/episodes").mkdir()
        pq.write_table(
            pa.Table.from_pylist([{"episode_index": 0, "length": 12}]), root / "meta/episodes/part.parquet"
        )
        pq.write_table(pa.Table.from_pylist([{"task_index": 0, "task": "Move"}]), root / "meta/tasks.parquet")
    return root


def episode(root, method="hybrid_window_event", *, with_meta=True, envelope=False):
    path = root / "output" / method / "episode_000000"
    path.mkdir(parents=True)
    value = {
        "dataset_name": root.name,
        "scheme_id": "hybrid_windows",
        "episode_index": 0,
        "frame_count": 12,
        "fps": 30,
        "model": "recorded-model",
        "task_hint": ["Move"],
        "source_digest": "recorded-source",
        "pipeline_version": "experiment-pipeline",
        **result(),
    }
    if envelope:
        value = {key: content for key, content in value.items() if key not in result()}
        value.update(dataset_key="local/source", result=result(), variant="caption")
    (path / "caption.json").write_text(json.dumps(value))
    (path / "video.mp4").write_bytes(b"0123456789abcdefghij")
    if with_meta:
        (path / "episode.json").write_text(
            json.dumps(
                {
                    "episode_index": 0,
                    "frame_count": 12,
                    "fps": 30,
                    "source_digest": "recorded-source",
                    "cameras": ["image"],
                    "task_hint": ["Move"],
                    "signals": [],
                }
            )
        )
    return path


def reader(root, **options):
    worker = SimpleNamespace(allowed_roots=[root.parent], state_path=root.parent / "state/agent.json")
    descriptor = {"root": str(root), "dataset_key": "node/source", "action": "runs", **options}
    return read_dataset_caption(worker, descriptor)


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
@pytest.mark.parametrize("with_meta,envelope", [(False, False), (True, False), (True, True)])
def test_method_name_from_directory_and_exact_result_preservation(tmp_path, version, with_meta, envelope):
    root = dataset(tmp_path / "source", version=version)
    source = episode(root, with_meta=with_meta, envelope=envelope)
    original = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    status, body, binary = reader(root)
    assert status == 200 and not binary
    run = json.loads(body)["runs"][0]
    assert run["scheme"]["id"] == run["scheme"]["name"] == "hybrid_window_event"
    value = json.loads(reader(root, action="result", run=run["run"])[1])
    assert value["result"] == result()
    assert value["source_scheme_id"] == "hybrid_windows"
    assert (
        value["source_caption_sha256"] == hashlib.sha256((source / "caption.json").read_bytes()).hexdigest()
    )
    assert value["dataset_key"] == "node/source"
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == original


def test_fresh_discovery_does_not_depend_on_registry_or_accept_incomplete_episode(tmp_path):
    root = dataset(tmp_path / "source")
    path = episode(root)
    (root / "output/experiment_registry.json").write_text('{"versions": []}')
    assert len(json.loads(reader(root)[1])["runs"]) == 1
    episode(root, "new_method")
    assert {run["scheme"]["name"] for run in json.loads(reader(root)[1])["runs"]} == {
        "hybrid_window_event",
        "new_method",
    }
    (path / "video.mp4").unlink()
    found = json.loads(reader(root)[1])
    assert len(found["runs"]) == 1 and found["warnings"]


def test_root_and_file_escape_are_rejected(tmp_path):
    root = dataset(tmp_path / "allowed/source")
    path = episode(root)
    outside = tmp_path / "outside.json"
    outside.write_text((path / "caption.json").read_text())
    (path / "caption.json").unlink()
    (path / "caption.json").symlink_to(outside)
    assert not json.loads(reader(root)[1])["runs"]
    (root / "output/escaped_method").symlink_to(path, target_is_directory=True)
    assert not json.loads(reader(root)[1])["runs"]
    worker = SimpleNamespace(allowed_roots=[tmp_path / "other"], state_path=tmp_path / "state.json")
    with pytest.raises(PermissionError, match="outside"):
        read_dataset_caption(worker, {"root": str(root), "dataset_key": "node/source", "action": "runs"})


def test_relay_wrong_node_duplicate_oversize_and_timeout():
    relay = CaptionReadRelay(timeout=0.5)
    received = []
    thread = threading.Thread(target=lambda: received.append(relay.request("node-a", {"action": "runs"})))
    thread.start()
    assert relay.claim("node-b") is None
    request = relay.claim("node-a")
    deadline = time.monotonic() + 0.4
    while request is None and time.monotonic() < deadline:
        time.sleep(0.001)
        request = relay.claim("node-a")
    assert request is not None
    with pytest.raises(KeyError):
        relay.complete(request["request_id"], "node-b", 200, b"{}", binary=False)
    with pytest.raises(ValueError):
        relay.complete(request["request_id"], "node-a", 200, b"x" * (MAX_REPLY_BYTES + 1), binary=False)
    relay.complete(request["request_id"], "node-a", 200, b"{}", binary=False)
    with pytest.raises(KeyError):
        relay.complete(request["request_id"], "node-a", 200, b"{}", binary=False)
    thread.join()
    assert received[0].body == b"{}" and not relay.pending
    relay.timeout = 0.001
    with pytest.raises(TimeoutError):
        relay.request("offline", {})
    assert not relay.pending


@pytest.fixture
def live(tmp_path, monkeypatch):
    root = dataset(tmp_path / "source")
    episode(root)
    store = _store(tmp_path)
    app = _app(tmp_path, store)
    ctx = SimpleNamespace(control_plane_store=store)
    published = tmp_path / "published"
    monkeypatch.setenv("DATA_PLATFORM_TEMPORAL_CAPTION_ROOT", str(published))
    register_temporal_caption_routes(app, ctx)

    @app.before_request
    def existing_audit_json_probe():
        # The full Viewer inspects every POST body before feature handlers run.
        if request.path.startswith("/api/agents/caption-reads/"):
            assert request.get_json(silent=True) is None

    token, node = store.enroll_node(
        name="reader-node",
        hostname="localhost",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        capabilities={"caption_read_protocol": 1},
        enrollment_token="test",
        expected_token="test",
    )
    location = store.sync_locations(
        node["node_id"], [{"root": str(root), "dataset_key": "node/source", "metadata": {}}]
    )[0]
    client = app.test_client()
    _bootstrap(client)
    worker = SimpleNamespace(allowed_roots=[tmp_path], state_path=tmp_path / "state/agent.json")
    monkeypatch.setattr("lerobot.data_platform.camera_layout.ordered_review_video", lambda path, *_: path)
    stop = threading.Event()
    failures = []

    def serve():
        transport = app.test_client()
        headers = {"Authorization": f"Bearer {token}"}
        while not stop.is_set():
            read = transport.get("/api/agents/caption-reads", headers=headers).get_json()["read"]
            if read:
                try:
                    status, body, binary = read_dataset_caption(worker, read)
                    response = transport.post(
                        f"/api/agents/caption-reads/{read['request_id']}?status={status}",
                        headers={**headers, "X-Caption-Read-Format": "bytes" if binary else "json"},
                        data=body,
                        content_type="application/octet-stream",
                    )
                    assert response.status_code == 200
                except Exception as exc:
                    failures.append(exc)
                    return
            stop.wait(0.001)

    thread = threading.Thread(target=serve)
    thread.start()
    yield client, store, root, location, app, token, published, ctx
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and not failures


def test_remote_results_ranges_roles_and_no_server_publication(live):
    client, store, root, location, app, _, published, ctx = live
    base = f"/api/control/locations/{location['location_id']}/temporal-caption"
    run = client.get(base).get_json()["runs"][0]["run"]
    value = client.get(f"{base}/{run}/caption").get_json()
    assert value["scheme"]["name"] == "hybrid_window_event" and value["result"] == result()
    video = value["video_url"]
    ranged = client.get(video, headers={"Range": "bytes=3-7"})
    assert ranged.status_code == 206 and ranged.data == b"34567"
    assert ranged.headers["Content-Range"] == "bytes 3-7/20"
    assert client.get(video, headers={"Range": "bytes=-3"}).data == b"hij"
    assert client.get(video, headers={"Range": "bytes=25-"}).status_code == 416
    head = client.head(video)
    assert head.status_code == 200 and head.data == b"" and head.headers["Content-Length"] == "20"
    assert client.get(video).data == b"0123456789abcdefghij"
    assert not published.exists() and not ctx.caption_read_relay.pending
    anonymous = app.test_client()
    assert anonymous.get(base).status_code == 401
    assert anonymous.get("/api/agents/caption-reads").status_code == 401
    user = store.register_user(username="reader", password="reader-password", display_name="Reader")
    assert (
        client.put(f"/api/auth/users/{user['user_id']}/data-scopes", json={"location_ids": []}).status_code
        == 200
    )
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"username": "reader", "password": "reader-password"})
    assert client.get(base).status_code == 403
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"username": "platform-admin", "password": "strong-admin-password"})
    assert (
        client.put(
            f"/api/auth/users/{user['user_id']}/data-scopes", json={"location_ids": [location["location_id"]]}
        ).status_code
        == 200
    )
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"username": "reader", "password": "reader-password"})
    assert client.get(base).status_code == 200
    assert client.post(base + "/jobs", json={}).status_code == 403
    assert client.post(base + "/import").status_code == 403
    assert not published.exists()


def test_video_change_during_read_is_rejected(live):
    client, _, root, location, _, _, _, _ = live
    base = f"/api/control/locations/{location['location_id']}/temporal-caption"
    run = client.get(base).get_json()["runs"][0]["run"]
    info = json.loads(reader(root, action="video-info", run=run)[1])
    (root / "output/hybrid_window_event/episode_000000/video.mp4").write_bytes(b"changed-video")
    status, _, binary = reader(
        root, action="video-chunk", run=run, start=0, length=5, revision=info["revision"]
    )
    assert status == 409 and not binary


def test_offline_source_read_does_not_hide_existing_published_results(live, tmp_path, monkeypatch):
    from lerobot.data_platform.temporal_caption_jobs import publish_run
    from tests.datasets.test_temporal_caption_jobs import make_hybrid_run

    client, store, _, location, _, _, published, _ = live
    source = make_hybrid_run(tmp_path / "published-input", key="node/source")
    publish_run(source, published, "node/source", "old-import")
    nodes = [{**node, "status": "offline"} for node in store.list_nodes()]
    monkeypatch.setattr(store, "list_nodes", lambda: nodes)
    base = f"/api/control/locations/{location['location_id']}/temporal-caption"
    response = client.get(base)
    assert response.status_code == 200
    assert [run["run"] for run in response.get_json()["runs"]] == ["old-import"]
    assert "offline" in response.get_json()["warnings"][0]
    assert client.get(base + "/old-import/caption").status_code == 200


def test_video_larger_than_one_reply_is_streamed_in_bounded_chunks(live):
    from lerobot.data_platform.caption_reads import VIDEO_CHUNK_BYTES

    client, _, root, location, _, _, published, ctx = live
    video = root / "output/hybrid_window_event/episode_000000/video.mp4"
    original = b"z" * (VIDEO_CHUNK_BYTES + 17)
    video.write_bytes(original)
    base = f"/api/control/locations/{location['location_id']}/temporal-caption"
    run = client.get(base).get_json()["runs"][0]["run"]
    response = client.get(f"{base}/{run}/caption/video")
    assert response.status_code == 200 and response.data == original
    assert not published.exists() and not ctx.caption_read_relay.pending

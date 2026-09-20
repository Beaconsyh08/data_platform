"""Remote caption ownership, attempt isolation, durable uploads, and imported result identity."""

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from lerobot.data_platform.execution import SpoolingClient
from lerobot.data_platform.routes.temporal_caption import register_temporal_caption_routes
from lerobot.data_platform.temporal_caption_jobs import import_archive, publish_run
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_temporal_caption import result


def make_run(path, key="trial/source", episode=25):
    path.mkdir(parents=True)
    (path / "episode.json").write_text(json.dumps({"episode_index": episode, "frame_count": 12, "fps": 30}))
    (path / "video.mp4").write_bytes(b"test-video")
    for variant in ("coarse", "refined"):
        (path / f"{variant}.json").write_text(
            json.dumps(
                {
                    "dataset_key": key,
                    "dataset_name": "source",
                    "episode_index": episode,
                    "frame_count": 12,
                    "fps": 30,
                    "duration_s": 0.4,
                    "variant": variant,
                    "scheme_id": "multiview_semantics",
                    "result": result(),
                    "model": "qwen3.8-max",
                    "created_at": 1,
                }
            )
        )
    return path


def archive(run):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for p in run.iterdir():
            output.writestr(f"imported/{p.name}", p.read_bytes())
    buffer.seek(0)
    return buffer


def test_import_rebind_is_idempotent_and_rejects_conflicts(tmp_path):
    source = make_run(tmp_path / "source")
    root = tmp_path / "published"
    assert import_archive(archive(source), root, "node/source", "source") == ["imported"]
    assert import_archive(archive(source), root, "node/source", "source") == ["imported"]
    saved = next(root.glob("*/imported/refined.json"))
    value = json.loads(saved.read_text())
    assert value["dataset_key"] == "node/source"
    assert value["imported_from_dataset_key"] == "trial/source"
    assert json.loads((source / "refined.json").read_text())["dataset_key"] == "trial/source"
    (source / "video.mp4").write_bytes(b"different")
    with pytest.raises(FileExistsError):
        import_archive(archive(source), root, "node/source", "source")
    with pytest.raises(ValueError, match="name"):
        import_archive(archive(source), root, "node/other", "other")
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as output:
        output.writestr("../video.mp4", b"escape")
    bad.seek(0)
    with pytest.raises(ValueError):
        import_archive(bad, root, "node/source", "source")


def test_partial_run_not_published(tmp_path):
    source = make_run(tmp_path / "source")
    (source / "refined.json").unlink()
    with pytest.raises(ValueError, match="Incomplete"):
        publish_run(source, tmp_path / "published", "trial/source", "run")
    assert not (tmp_path / "published").exists()


def test_spool_retains_caption_upload_after_temporary_export_deleted(tmp_path):
    work = tmp_path / "work"
    client = SpoolingClient("http://unused", work)
    original = tmp_path / "video.mp4"
    original.write_bytes(b"durable-video")
    client.upload_artifact(None, "job", Path("video.mp4"), original, caption=True)
    original.unlink()
    upload = json.loads(next((work / "uploads").glob("*.json")).read_text())
    assert upload["caption"] is True
    assert Path(upload["path"]).read_bytes() == b"durable-video"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(tmp_path, store)
    root = tmp_path / "captions"
    monkeypatch.setenv("DATA_PLATFORM_TEMPORAL_CAPTION_ROOT", str(root))
    register_temporal_caption_routes(app, SimpleNamespace(control_plane_store=store))
    client = app.test_client()
    _bootstrap(client)
    token, node = store.enroll_node(
        name="caption-node",
        hostname="localhost",
        allowed_roots=["/data"],
        writable_roots=["/data"],
        capabilities={
            "job_protocol": 2,
            "caption_protocol": 1,
            "caption_configured": True,
            "data_profile_protocol": 100,
            "operations": ["caption.annotate"],
        },
        enrollment_token="test",
        expected_token="test",
    )
    location = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "node/source",
                "root": "/data/source",
                "output_dir": "/data/cache",
                "metadata": {"codebase_version": "v3.0", "features": {"head_image": {"dtype": "image"}}},
            }
        ],
    )[0]
    return client, store, node, token, location, root


def queue(remote):
    client, store, node, token, location, _ = remote
    url = f"/api/control/locations/{location['location_id']}/temporal-caption/jobs"
    body = {"episode_index": 25, "scheme": "multiview_semantics"}
    response = client.post(url, json=body, headers={"Idempotency-Key": "one"})
    assert response.status_code == 202, response.get_json()
    job = response.get_json()["job"]
    repeated = client.post(url, json=body, headers={"Idempotency-Key": "one"})
    assert repeated.get_json()["job"]["job_id"] == job["job_id"]
    claimed = store.claim_job(node["node_id"], worker_instance_id="worker")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Job-Attempt": claimed["execution"]["attempt_id"],
        "X-Job-Credential": claimed["execution"]["credential"],
    }
    return job, headers, url


def test_remote_job_publication_and_attempt_guards(remote, tmp_path):
    client, store, _, _, location, root = remote
    job, headers, url = queue(remote)
    endpoint = f"/api/agents/jobs/{job['job_id']}"
    assert client.put(endpoint + "/caption-artifacts/video.mp4", data=b"x").status_code == 401
    bad_headers = {**headers, "X-Job-Credential": "wrong"}
    assert (
        client.put(endpoint + "/caption-artifacts/video.mp4", headers=bad_headers, data=b"x").status_code
        == 409
    )
    assert client.post(endpoint + "/complete", headers=headers, json={"status": "done"}).status_code == 400
    source = make_run(tmp_path / "source", key="node/source")
    for p in source.iterdir():
        assert (
            client.put(
                endpoint + f"/caption-artifacts/{p.name}", headers=headers, data=p.read_bytes()
            ).status_code
            == 200
        )
    completed = client.post(endpoint + "/complete", headers=headers, json={"status": "done"})
    assert completed.status_code == 200, completed.get_json()
    assert completed.get_json()["job"]["status"] == "done"
    assert client.post(endpoint + "/complete", headers=headers, json={"status": "done"}).status_code == 200
    review_api = url.removesuffix("/jobs")
    assert len(client.get(review_api).get_json()["runs"]) == 2
    assert (
        client.get(review_api + f"/{job['job_id']}/refined/video", headers={"Range": "bytes=0-3"}).status_code
        == 206
    )
    assert not any("key" in key for key in store.get_job(job["job_id"])["options"])


def test_viewer_denied_submit_import_and_owner_jobs_scoped(remote, tmp_path):
    client, store, _, _, location, _ = remote
    _, _, url = queue(remote)
    store.register_user(username="reader", password="reader-password", display_name="Reader")
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"username": "reader", "password": "reader-password"})
    assert client.post(url, json={}).status_code == 403
    assert client.post(url.removesuffix("/jobs") + "/import").status_code == 403
    listed = client.get(url).get_json()
    assert listed["jobs"] == [] and not listed["can_submit"] and not listed["can_import"]


@pytest.mark.parametrize(
    "options",
    [
        None,
        [],
        {"episode_index": True, "scheme": "video_events"},
        {"episode_index": 25, "scheme": []},
        {"episode_index": 25, "scheme": "video_events", "api_key": "never"},
    ],
)
def test_bad_options_do_not_queue(remote, options):
    client, store, _, _, location, _ = remote
    url = f"/api/control/locations/{location['location_id']}/temporal-caption/jobs"
    assert client.post(url, json=options, headers={"Idempotency-Key": "one"}).status_code == 400
    assert store.list_jobs() == []


@pytest.mark.parametrize("scheme", ["multiview_semantics", "video_events"])
def test_real_episode_agent_execution_round_trip(remote, tmp_path, monkeypatch, scheme):
    from lerobot.data_platform import qwen
    from lerobot.data_platform.temporal_caption_jobs import execute_caption_job
    from tests.datasets.test_temporal_caption import (
        test_export_reads_only_selected_episode_from_shared_v3_shard,
    )

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    test_export_reads_only_selected_episode_from_shared_v3_shard(fixture)
    client, store, node, token, location, _ = remote
    url = f"/api/control/locations/{location['location_id']}/temporal-caption/jobs"
    queued = client.post(
        url, json={"episode_index": 1, "scheme": scheme}, headers={"Idempotency-Key": "real"}
    ).get_json()["job"]
    job = store.claim_job(node["node_id"], worker_instance_id="worker")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Job-Attempt": job["execution"]["attempt_id"],
        "X-Job-Credential": job["execution"]["credential"],
    }
    calls = []

    class FakeQwen:
        def __init__(self, **kwargs):
            pass

        def post_chat_completion(self, payload):
            calls.append(payload)
            value = result()
            value["segments"] = [{**value["segments"][0], "end_frame": 2}]
            if scheme == "video_events":
                value = {
                    "episode_caption": {"en": "Move", "zh": "移动"},
                    "segments": [
                        {
                            "start_frame": 0,
                            "end_frame": 1,
                            "caption_zh": "移动",
                            "caption_en": "Move",
                            "evidence": "Visible movement",
                        }
                    ],
                }
            return {"choices": [{"message": {"content": json.dumps(value)}}]}

    class Transport:
        def event(self, state, job_id, message, payload):
            response = client.post(
                f"/api/agents/jobs/{job_id}/events",
                headers=headers,
                json={"message": message, "payload": payload},
            )
            assert response.status_code == 200

        def upload_artifact(self, state, job_id, relative, path, *, caption):
            assert caption
            response = client.put(
                f"/api/agents/jobs/{job_id}/caption-artifacts/{relative}",
                headers=headers,
                data=path.read_bytes(),
            )
            assert response.status_code == 200

    monkeypatch.setattr(qwen, "QwenClient", FakeQwen)
    agent = SimpleNamespace(client=Transport(), state=None, state_path=tmp_path / "agent/state.json")
    output = execute_caption_job(agent, job, fixture / "source", location)
    assert len(calls) == (2 if scheme == "multiview_semantics" else 1)
    assert all(p["model"] == "qwen3.8-max" for p in calls)
    completed = client.post(
        f"/api/agents/jobs/{queued['job_id']}/complete",
        headers=headers,
        json={"status": "done", "result": output},
    )
    assert completed.status_code == 200, completed.get_json()
    variant = "refined" if scheme == "multiview_semantics" else "caption"
    review = client.get(url.removesuffix("/jobs") + f"/{job['job_id']}/{variant}").get_json()
    assert review["episode_index"] == 1 and review["frame_count"] == 2
    assert review["context"]["signals"]
    assert list((tmp_path / "agent/caption-work").iterdir()) == []


def test_cancelled_attempt_cannot_publish(remote, tmp_path):
    from tests.datasets.test_job_management import command

    client, store, _, _, _, root = remote
    job, headers, _ = queue(remote)
    owner = store.verify_session(next(c.value for c in client._cookies.values() if "session" in c.key))
    command(store, job, owner, "cancel")
    response = client.post(
        f"/api/agents/jobs/{job['job_id']}/complete", headers=headers, json={"status": "done"}
    )
    assert response.status_code == 409
    assert not list(root.glob("*/%s" % job["job_id"]))


def test_fusion_requires_agent_scheme_capability(remote):
    client, store, node, _, location, _ = remote
    url = f"/api/control/locations/{location['location_id']}/temporal-caption/jobs"
    options = {"episode_index": 25, "scheme": "fusion_review"}
    assert client.post(url, json=options, headers={"Idempotency-Key": "fusion"}).status_code == 409
    current = next(n for n in store.list_nodes() if n["node_id"] == node["node_id"])
    store.heartbeat(
        node["node_id"], capabilities={**current["capabilities"], "caption_schemes": ["fusion_review"]}
    )
    assert client.post(url, json=options, headers={"Idempotency-Key": "fusion"}).status_code == 202

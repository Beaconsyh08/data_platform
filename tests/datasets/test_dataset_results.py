"""Dataset result ownership, authorization, durability and Agent replication."""

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lerobot.data_platform.dataset_results import (
    ResultSnapshot,
    apply_results,
    mark_edited_csv,
    migrate_cache,
    preserve_results,
    sync_agent_results,
)
from lerobot.data_platform.execution import atomic_json
from tests.datasets.test_control_plane import _app, _bootstrap, _store


def setup_results(tmp_path):
    store = _store(tmp_path)
    token, node = store.enroll_node(
        name="worker",
        hostname="worker",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        capabilities={"result_sync_protocol": 1},
        enrollment_token="test",
        expected_token="test",
    )
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "vis/local_vis_source"
    location = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "remote/source",
                "root": str(source),
                "output_dir": str(output),
            }
        ],
    )[0]
    cache = tmp_path / "remote-cache" / location["location_id"]
    manifest = {
        "root": str(source),
        "total_episodes": 2,
        "total_frames": 10,
        "episodes": [{"episode_index": 0}, {"episode_index": 1}],
    }
    atomic_json(cache / "static/viewer_manifest.json", manifest)
    atomic_json(output / "static/viewer_manifest.json", manifest)
    store.mark_viewer_ready(
        location["location_id"], viewer_url="/remote/source/episode_0", cache_root=str(cache)
    )
    app = _app(tmp_path, store)
    client = app.test_client()
    _bootstrap(client)
    return store, client, token, location, cache, output


class Bridge:
    def __init__(self, client, token):
        self.client, self.headers = client, {"Authorization": f"Bearer {token}"}
        self.fail_ack = False
        self.downloads = 0

    def result_locations(self, state):
        response = self.client.get("/api/agents/dataset-results", headers=self.headers)
        assert response.status_code == 200
        return response.json["locations"]

    def result_bundle(self, state, location_id):
        self.downloads += 1
        response = self.client.get(f"/api/agents/locations/{location_id}/results", headers=self.headers)
        assert response.status_code == 200
        return response.json

    def ack_results(self, state, location_id, revision, *, error=False):
        if self.fail_ack:
            raise OSError("connection lost")
        response = self.client.post(
            f"/api/agents/locations/{location_id}/results/ack",
            headers=self.headers,
            json={"revision": revision, "error": error},
        )
        assert response.status_code == 200
        return response.json


def status(client):
    return client.get("/api/dataset-results/status?dataset_key=remote/source").json


def test_flag_sync_round_trip_and_retry_preserves_unrelated_source(tmp_path):
    _, client, token, location, cache, output = setup_results(tmp_path)
    atomic_json(cache / "static/flagged_episodes.json", {"flagged_episodes": [1]})
    atomic_json(cache / "static/manual_flagged_episodes.json", {"flag_reasons": {"1": ["wrong_prompt"]}})
    source_file = Path(location["root"]) / "source.parquet"
    source_file.write_bytes(b"untouched-source")
    (output / "static/unrelated.txt").write_text("preserve")
    bridge = Bridge(client, token)
    agent = SimpleNamespace(
        client=bridge,
        state=None,
        state_path=tmp_path / "agent/state.json",
        allowed_roots=[tmp_path],
        writable_roots=[tmp_path],
    )
    assert status(client)["state"] == "pending"
    bridge.fail_ack = True
    sync_agent_results(agent)
    assert json.loads((output / "static/flagged_episodes.json").read_text())["flagged_episodes"] == [1]
    assert status(client)["state"] == "pending"  # A lost ACK must not claim remote success.
    bridge.fail_ack = False
    sync_agent_results(agent)
    assert bridge.downloads == 1
    assert status(client)["state"] == "synced"
    atomic_json(cache / "static/flagged_episodes.json", {"flagged_episodes": []})
    (cache / "static/manual_flagged_episodes.json").unlink()
    assert status(client)["state"] == "pending"
    sync_agent_results(agent)
    assert not (output / "static/manual_flagged_episodes.json").exists()
    assert json.loads((output / "static/flagged_episodes.json").read_text())["flagged_episodes"] == []
    assert list((output / ".result-sync-backups").glob("*/manual_flagged_episodes.json"))
    assert source_file.read_bytes() == b"untouched-source"
    assert (output / "static/unrelated.txt").read_text() == "preserve"


def test_result_routes_reject_other_nodes_and_stale_ack(tmp_path):
    store, client, token, location, cache, _ = setup_results(tmp_path)
    other, _ = store.enroll_node(
        name="other",
        hostname="other",
        allowed_roots=[],
        writable_roots=[],
        capabilities={},
        enrollment_token="test",
        expected_token="test",
    )
    assert client.get("/api/agents/dataset-results").status_code == 401
    assert client.get("/api/agents/dataset-results", headers={"Authorization": f"Bearer {other}"}).json == {
        "locations": []
    }
    assert (
        client.get(
            f"/api/agents/locations/{location['location_id']}/results",
            headers={"Authorization": f"Bearer {other}"},
        ).status_code
        == 404
    )
    old = status(client)["revision"]
    atomic_json(cache / "static/flagged_episodes.json", {"flagged_episodes": [0]})
    response = client.post(
        f"/api/agents/locations/{location['location_id']}/results/ack",
        headers={"Authorization": f"Bearer {token}"},
        json={"revision": old},
    )
    assert response.status_code == 409
    assert status(client)["state"] == "pending"


def test_sync_write_failure_is_visible_and_retried(tmp_path):
    _, client, token, location, cache, output = setup_results(tmp_path)
    atomic_json(cache / "static/flagged_episodes.json", {"flagged_episodes": [0]})
    agent = SimpleNamespace(
        client=Bridge(client, token),
        state=None,
        state_path=tmp_path / "agent/state.json",
        allowed_roots=[tmp_path],
        writable_roots=[tmp_path / "not-writable"],
    )
    sync_agent_results(agent)
    assert status(client)["state"] == "error"
    assert not (output / "static/flagged_episodes.json").exists()
    agent.writable_roots = [tmp_path]
    sync_agent_results(agent)
    assert status(client)["state"] == "synced"


@pytest.mark.parametrize("fault", ["checksum", "traversal", "symlink", "dataset"])
def test_invalid_transfer_cannot_replace_results(tmp_path, fault):
    _, client, token, location, cache, output = setup_results(tmp_path)
    atomic_json(cache / "static/flagged_episodes.json", {"flagged_episodes": [1]})
    atomic_json(output / "static/flagged_episodes.json", {"flagged_episodes": [0]})
    bundle = Bridge(client, token).result_bundle(None, location["location_id"])
    if fault == "checksum":
        bundle["contents"]["flagged_episodes.json"] = base64.b64encode(b"corrupt").decode()
    elif fault == "traversal":
        bundle["files"]["../source.json"] = bundle["files"].pop("flagged_episodes.json")
        bundle["revision"] = hashlib.sha256(json.dumps(bundle["files"], sort_keys=True).encode()).hexdigest()
    elif fault == "symlink":
        (output / "static/flagged_episodes.json").unlink()
        (output / "static/flagged_episodes.json").symlink_to(cache / "static/flagged_episodes.json")
    else:
        bundle["dataset"]["root"] = "/different-dataset"
    before = (output / "static/flagged_episodes.json").read_bytes()
    with pytest.raises(ValueError):
        apply_results(output, bundle, tmp_path / "receipt.json")
    assert (output / "static/flagged_episodes.json").read_bytes() == before
    assert not (tmp_path / "receipt.json").exists()


def test_rebuild_preserves_review_results_and_migration_keeps_legacy_reference(tmp_path):
    previous, staged, target = tmp_path / ".jobs/attempt", tmp_path / "staged", tmp_path / "dataset-id"
    atomic_json(previous / "static/viewer_manifest.json", {"root": "/dataset"})
    atomic_json(previous / "static/manual_flagged_episodes.json", {"flagged_episodes": [4]})
    atomic_json(previous / "static/subtask_annotations.json", {"4": [{"state": 1, "time": 2}]})
    atomic_json(staged / "static/subtask_annotations.json", {"4": []})
    preserve_results(previous, staged)
    assert json.loads((staged / "static/subtask_annotations.json").read_text())["4"]
    assert (staged / "static/manual_flagged_episodes.json").is_file()
    migrate_cache(previous, target)
    assert previous.is_symlink() and previous.resolve() == target
    assert migrate_cache(previous, target) == target
    assert ResultSnapshot.read(target / "static").files.keys() == {
        "manual_flagged_episodes.json",
        "subtask_annotations.json",
    }


def test_stage_csv_edits_are_synced_without_copying_the_whole_csv_cache(tmp_path):
    _, client, token, location, cache, output = setup_results(tmp_path)
    (cache / "static/csv").mkdir()
    (cache / "static/csv/episode_000000_ds1.csv").write_text("timestamp,stage\n0,1\n")
    (cache / "static/csv/episode_000001_ds1.csv").write_text("timestamp,stage\n0,0\n")
    atomic_json(cache / "static/csv/episode_000000_ds1.stages.json", {"stage_count": 2})
    mark_edited_csv(cache, 0)
    bundle = Bridge(client, token).result_bundle(None, location["location_id"])
    assert set(bundle["files"]) == {"csv/episode_000000_ds1.csv", "csv/episode_000000_ds1.stages.json"}
    apply_results(output, bundle, tmp_path / "receipt.json")
    assert (output / "static/csv/episode_000000_ds1.csv").read_text() == "timestamp,stage\n0,1\n"
    assert not (output / "static/csv/episode_000001_ds1.csv").exists()


def test_web_restart_migrates_legacy_cache_and_restores_the_same_dataset_key(tmp_path):
    from lerobot.data_platform import viewer

    store, _, _, location, cache, _ = setup_results(tmp_path)
    atomic_json(cache / "static/manual_flagged_episodes.json", {"flagged_episodes": [1]})
    legacy = cache.parent / ".jobs/old-job/attempt"
    legacy.parent.mkdir(parents=True)
    cache.rename(legacy)
    store.mark_viewer_ready(
        location["location_id"], viewer_url="/remote/source/episode_0", cache_root=str(legacy)
    )
    console = tmp_path / "console/static"
    console.mkdir(parents=True)
    for _ in range(2):
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
            static_folder=console,
            template_folder=Path(viewer.__file__).parent / "templates",
            database_url=f"sqlite:///{tmp_path / 'control-plane.db'}",
            remote_cache_root=tmp_path / "remote-cache",
            start_server=False,
        )
        client = app.test_client()
        assert (
            client.post(
                "/api/auth/login", json={"username": "platform-admin", "password": "strong-admin-password"}
            ).status_code
            == 200
        )
        assert client.get("/remote/source/flagged_episodes").status_code == 200
        assert store.get_location(location["location_id"])["metadata"]["cache_root"] == str(cache)
    assert legacy.is_symlink() and legacy.resolve() == cache
    assert json.loads((cache / "static/manual_flagged_episodes.json").read_text())["flagged_episodes"] == [1]


def test_partial_write_retries_and_retains_original_backup(tmp_path, monkeypatch):
    from lerobot.data_platform import dataset_results

    _, client, token, location, cache, output = setup_results(tmp_path)
    for name in ("flagged_episodes.json", "manual_flagged_episodes.json"):
        atomic_json(cache / "static" / name, {"flagged_episodes": [1]})
        atomic_json(output / "static" / name, {"flagged_episodes": [0]})
    bundle = Bridge(client, token).result_bundle(None, location["location_id"])
    original = dataset_results.os.replace
    writes = []

    def interrupted(old, new):
        if Path(new).parent == output / "static":
            writes.append(new)
            if len(writes) == 2:
                raise OSError("disk unavailable")
        return original(old, new)

    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(dataset_results.os, "replace", interrupted)
    with pytest.raises(OSError):
        apply_results(output, bundle, receipt)
    assert not receipt.exists()
    monkeypatch.setattr(dataset_results.os, "replace", original)
    apply_results(output, bundle, receipt)
    for name in bundle["files"]:
        assert json.loads((output / "static" / name).read_text())["flagged_episodes"] == [1]
        saved = output / ".result-sync-backups" / bundle["revision"] / name
        assert json.loads(saved.read_text())["flagged_episodes"] == [0]


def test_viewer_completion_publishes_stable_dataset_cache_and_preserves_flags(tmp_path):
    store, client, token, location, cache, _ = setup_results(tmp_path)
    atomic_json(cache / "static/manual_flagged_episodes.json", {"flagged_episodes": [1]})
    job = client.post(f"/api/control/locations/{location['location_id']}/viewer-jobs", json={}).json["job"]
    headers = {"Authorization": f"Bearer {token}"}
    claimed = client.post("/api/agents/jobs/claim", headers=headers, json={}).json["job"]
    assert claimed["job_id"] == job["job_id"]
    manifest = json.loads((cache / "static/viewer_manifest.json").read_text())
    assert (
        client.put(
            f"/api/agents/jobs/{job['job_id']}/artifacts/viewer_manifest.json",
            headers=headers,
            data=json.dumps(manifest),
        ).status_code
        == 200
    )
    response = client.post(
        f"/api/agents/jobs/{job['job_id']}/complete", headers=headers, json={"status": "done", "result": {}}
    )
    assert response.status_code == 200, response.json
    current = store.get_location(location["location_id"])
    assert current["metadata"]["cache_root"] == str(cache)
    assert (cache / "static/manual_flagged_episodes.json").is_file()
    assert list(cache.parent.glob(f".{cache.name}.backup-*/static/manual_flagged_episodes.json"))
    assert not (cache.parent / ".jobs" / job["job_id"] / "static").exists()
    assert (
        client.post(
            f"/api/agents/jobs/{job['job_id']}/complete",
            headers=headers,
            json={"status": "done", "result": {}},
        ).status_code
        == 200
    )

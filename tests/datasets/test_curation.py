"""Curation snapshots preserve identity without requiring Server A to see Agent files."""

import json
from dataclasses import replace

import pytest

from lerobot.data_platform.curation import (
    CurationSnapshot,
    CurationTarget,
    artifact_path,
    bundle_manifest,
    publish_bundle,
    register_snapshot,
    scan_snapshot,
)
from lerobot.data_platform.curation_execution import validate_parameters
from lerobot.data_platform.lifecycle import LifecycleStore
from tests.datasets.test_lifecycle import _make_dataset, _snapshot


def test_remote_snapshot_has_stable_identity_and_supports_offline_drafts(tmp_path):
    source = tmp_path / "source"
    _make_dataset(source)
    original = _snapshot(source)
    first = scan_snapshot(source, "node/source", "ds_remote")
    again = scan_snapshot(source, "node/source", "ds_remote", previous=first.to_dict())
    assert first.version["version_id"] == again.version["version_id"]
    assert first.version["episode_refs"] == again.version["episode_refs"]
    store = LifecycleStore(tmp_path / "central")
    location = {
        "root": "/unavailable/data/source",
        "location_id": "location-a",
        "node_id": "node-a",
        "dataset_key": "node/source",
    }
    version = register_snapshot(store, first, location)
    assert store.get_version(version.version_id).root == location["root"]
    profile = store.create_dataset_profile(version.version_id)
    assert profile is not None
    cohort = store.resolve_cohort(version.version_id, {"episode_indices": [1]})
    assert cohort["episode_count"] == 1
    workspace = store.create_workspace(version.version_id, owner="operator")
    with pytest.raises(ValueError, match="fresh source validation"):
        store.publish_workspace(
            workspace.workspace_id, expected_revision=workspace.revision, reviewer="operator"
        )
    manifest = store.publish_workspace(
        workspace.workspace_id,
        expected_revision=workspace.revision,
        reviewer="operator",
        source_fingerprint=version.fingerprint,
    )
    assert manifest.status == "published"
    other = {**location, "node_id": "node-b", "location_id": "location-b"}
    register_snapshot(store, first, other)
    assert len(store.list_replicas(dataset_version_id=version.version_id)) == 2
    assert original == _snapshot(source)


def test_snapshot_rejects_mismatched_episode_identity(tmp_path):
    source = tmp_path / "source"
    _make_dataset(source)
    snapshot = scan_snapshot(source, "node/source", "ds_remote")
    value = snapshot.to_dict()
    value["version"]["episode_refs"][0]["episode_uid"] = "another-episode"
    with pytest.raises(ValueError, match="identity mismatch"):
        CurationSnapshot.from_dict(value)


def test_result_publication_is_complete_idempotent_and_attempt_bound(tmp_path):
    staged = tmp_path / "attempt"
    staged.mkdir()
    (staged / "result.json").write_text(json.dumps({"fingerprint": "abc"}))
    target = CurationTarget("node/source", "location", "version").to_dict()
    bundle_manifest(staged, operation="curation.validate_source", target=target)
    output = tmp_path / "results" / "run"
    publish_bundle(staged, output, "curation.validate_source", target)
    publish_bundle(staged, output, "curation.validate_source", target)
    with pytest.raises(ValueError, match="different request"):
        publish_bundle(staged, output, "curation.quality", target)
    (staged / "result.json").write_text("tampered")
    with pytest.raises(ValueError):
        publish_bundle(staged, output, "curation.validate_source", target)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "nested/../../escape", "a\\b"])
def test_artifacts_cannot_escape_root(tmp_path, name):
    with pytest.raises(ValueError):
        artifact_path(tmp_path, name)


def test_client_cannot_override_execution_inputs():
    with pytest.raises(ValueError, match="Unsupported parameters"):
        validate_parameters("curation.labeling", {"root": "/another/dataset"})
    with pytest.raises(ValueError, match="Unsupported parameters"):
        validate_parameters("curation.tagging", {"vlm_token": "not-accepted"})
    with pytest.raises(ValueError, match="variant"):
        validate_parameters("curation.labeling", {"output_variant": "../../escape"})
    assert replace(CurationTarget("local/source"), dataset_version_id="v1").dataset_version_id == "v1"


def test_input_download_checks_lease_credentials_and_content(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from lerobot.data_platform.agent import AgentClient
    from lerobot.data_platform.execution import StopRequestedError

    client = AgentClient("https://curation.invalid", verify=False)
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield b"verified"

    def get(url, **kwargs):
        calls.append(kwargs["headers"])
        return Response()

    monkeypatch.setattr(client.session, "get", get)
    monkeypatch.setattr(client, "control_heartbeat", lambda *args, **kwargs: {"renewed": True})
    state = SimpleNamespace(node_token="test-node-credential")
    job = {"job_id": "job", "execution": {"attempt_id": "attempt", "credential": "attempt-credential"}}
    entry = {"size": 8, "sha256": hashlib.sha256(b"verified").hexdigest()}
    client.download_curation_input(state, job, "labeling/labels.jsonl", tmp_path / "input", entry)
    assert (tmp_path / "input").read_bytes() == b"verified"
    assert calls[0]["X-Job-Credential"] == "attempt-credential"
    with pytest.raises(ValueError, match="checksum"):
        client.download_curation_input(state, job, "file", tmp_path / "bad", {**entry, "sha256": "wrong"})
    monkeypatch.setattr(
        client, "control_heartbeat", lambda *args, **kwargs: {"renewed": True, "stop_mode": "graceful"}
    )
    with pytest.raises(StopRequestedError):
        client.download_curation_input(state, job, "file", tmp_path / "cancelled", entry)
    assert not (tmp_path / "cancelled").exists()

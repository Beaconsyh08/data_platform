"""Source-loss proofs preserve running work, independent datasets, and durable job history."""

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from lerobot.data_platform.control_plane import ControlPlaneStore
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.routes.source_cleanup import register_source_cleanup_routes
from lerobot.data_platform.source_cleanup import SourceCleanupManager
from lerobot.data_platform.source_cleanup_agent import SourceCleanupWorker
from lerobot.data_platform.source_cleanup_files import (
    missing_witness,
    prune_node_job_cache,
    prune_server_location_cache,
    prune_viewer_cache,
    remove_owned,
)


@pytest.fixture
def setup(tmp_path):
    store = ControlPlaneStore(f"sqlite:///{tmp_path / 'control.db'}")
    actor = store.bootstrap_admin(
        username="admin", password="strong-admin-password", bootstrap_token="t", expected_token="t"
    )
    token, node = store.enroll_node(
        name="node",
        hostname="node",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        enrollment_token="t",
        expected_token="t",
        capabilities={"source_cleanup_protocol": 1, "operations": ["viewer.prepare"], "job_protocol": 2},
    )
    source = tmp_path / "source"
    source.mkdir()
    cache = tmp_path / "vis/local_vis_source"
    (cache / "static").mkdir(parents=True)
    (cache / "static/evidence.json").write_text("{}")
    location = store.sync_locations(
        node["node_id"], [{"dataset_key": "node/source", "root": str(source), "output_dir": str(cache)}]
    )[0]
    manager = SourceCleanupManager(store, tmp_path / "remote-cache")
    return SimpleNamespace(
        store=store,
        actor=actor,
        node=node,
        token=token,
        source=source,
        cache=cache,
        location=location,
        manager=manager,
        tmp=tmp_path,
    )


def missing(setup, monkeypatch):
    shutil.rmtree(setup.source)
    body = {
        "location_id": setup.location["location_id"],
        "root": str(setup.source),
        "revision": setup.location["updated_at"],
        "status": "missing",
        "witness": missing_witness(setup.source, [setup.tmp]),
    }
    monkeypatch.setattr("lerobot.data_platform.source_cleanup.time.time", lambda: 100)
    assert setup.manager.report(setup.node, body)["status"] == "observed"
    monkeypatch.setattr("lerobot.data_platform.source_cleanup.time.time", lambda: 106)
    return body


def confirmed(setup, monkeypatch):
    body = missing(setup, monkeypatch)
    assert setup.manager.report(setup.node, body)["status"] == "waiting"
    return setup.manager.poll(setup.node)["task"]


def test_confirmed_deletion_hides_registration_preserves_history_and_restarts(setup, monkeypatch):
    job = setup.store.create_job(
        location_id=setup.location["location_id"],
        requested_by=setup.actor["user_id"],
        operation="viewer.prepare",
    )
    task = confirmed(setup, monkeypatch)
    assert task and not setup.store.list_locations()
    assert setup.store.get_job(job["job_id"])["status"] == "cancelled"
    assert setup.store.get_location(setup.location["location_id"])["state"] == "deleting"
    with pytest.raises(JobConflictError):
        setup.store.create_job(
            location_id=setup.location["location_id"],
            requested_by=setup.actor["user_id"],
            operation="viewer.prepare",
        )
    manager = SourceCleanupManager(setup.store, setup.manager.cache_root)
    lease = manager.begin_ack(setup.node["node_id"], task["token"])
    assert manager.begin_ack(setup.node["node_id"], task["token"]) == {"status": "busy"}
    manager.error(setup.node["node_id"], task["token"], "temporary disk error")
    lease = manager.begin_ack(setup.node["node_id"], task["token"])
    assert manager.finish_ack(setup.node["node_id"], task["token"], lease["owner"], "") == {"status": "done"}
    assert manager.begin_ack(setup.node["node_id"], task["token"]) is None
    assert setup.store.get_job(job["job_id"])
    assert setup.store.list_locations(include_deleted=True)[0]["state"] == "deleted"


def test_one_observation_stale_revision_foreign_node_do_not_delete(setup, monkeypatch):
    body = missing(setup, monkeypatch)
    assert setup.store.list_locations()
    with pytest.raises(ValueError, match="changed"):
        setup.manager.report(setup.node, {**body, "revision": "stale"})
    with pytest.raises(KeyError):
        setup.manager.report({**setup.node, "node_id": "wrong"}, body)
    assert setup.manager.poll(setup.node)["task"] is None


def test_running_and_interrupted_jobs_hold_caches(setup, monkeypatch):
    job = setup.store.create_job(
        location_id=setup.location["location_id"],
        requested_by=setup.actor["user_id"],
        operation="viewer.prepare",
    )
    setup.store.claim_job(setup.node["node_id"], worker_instance_id="test")
    assert confirmed(setup, monkeypatch) is None
    assert setup.store.get_job(job["job_id"])["status"] == "running"
    assert setup.cache.is_dir()
    from lerobot.data_platform.control_plane import RemoteJob

    with setup.store.sessions.begin() as session:
        session.get(RemoteJob, job["job_id"]).status = "interrupted"
    assert setup.manager.poll(setup.node)["task"] is None


def test_restore_aborts_cleanup_and_stale_sync_cannot_resurrect(setup, monkeypatch):
    task = confirmed(setup, monkeypatch)
    synced = setup.store.sync_locations(
        setup.node["node_id"],
        [{"dataset_key": "node/source", "root": str(setup.source), "output_dir": str(setup.cache)}],
    )
    assert synced[0]["state"] == "deleting"
    setup.source.mkdir()
    with pytest.raises(ValueError, match="Stale"):
        setup.manager.report(setup.node, {**task, "status": "present", "token": "old"})
    setup.manager.report(setup.node, {**task, "status": "present"})
    assert setup.store.list_locations()[0]["state"] == "available"
    assert setup.manager.poll(setup.node)["task"] is None
    with pytest.raises(ValueError):
        setup.manager.begin_ack(setup.node["node_id"], task["token"])


def test_missing_probe_keeps_errors_links_and_unknown_data(tmp_path, monkeypatch):
    existing = tmp_path / "not-a-dataset"
    existing.mkdir()
    assert missing_witness(existing, [tmp_path]) is None
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "missing")
    assert missing_witness(link, [tmp_path]) is None
    assert missing_witness(tmp_path / "missing", [tmp_path / "other"]) is None
    assert missing_witness(tmp_path / "missing", [tmp_path])["anchor"] == str(tmp_path)
    original = Path.lstat

    def denied(path, *args, **kwargs):
        if path == tmp_path / "denied":
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    assert missing_witness(tmp_path / "denied", [tmp_path]) is None


def test_remove_owned_does_not_follow_symlinks_or_remove_other_sources(tmp_path):
    base = tmp_path / "managed"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("keep")
    (base / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        remove_owned(base / "link/data", base)
    remove_owned(base / "link", base)
    assert (outside / "data").read_text() == "keep"
    with pytest.raises(PermissionError):
        remove_owned(base, base)
    protected = base / "cache"
    protected.mkdir()
    (protected / "derived").mkdir()
    with pytest.raises(PermissionError):
        remove_owned(protected, base, protected_roots=[protected / "derived"])


def test_viewer_cleanup_all_owned_files_preserves_source_and_other_cache(setup):
    other = setup.cache.parent / "other"
    other.mkdir()
    (other / "data").write_text("keep")
    task = {**setup.location, "protected_roots": [], "shared_cache": False}
    prune_viewer_cache(task, [setup.tmp], state_root=setup.tmp / "agent")
    assert not setup.cache.exists() and setup.source.exists() and other.exists()
    prune_viewer_cache(task, [setup.tmp], state_root=setup.tmp / "agent")


@pytest.mark.parametrize("case", ["shared", "outside", "unknown", "dataset"])
def test_viewer_cleanup_refuses_unproven_or_shared_paths(setup, case):
    task = {**setup.location, "protected_roots": [], "shared_cache": case == "shared"}
    roots = [setup.tmp] if case != "outside" else [setup.tmp / "elsewhere"]
    if case == "unknown":
        output = setup.tmp / "unknown"
        output.mkdir()
        task["output_dir"] = str(output)
    if case == "dataset":
        (setup.cache / "meta").mkdir()
        (setup.cache / "meta/info.json").write_text("{}")
    with pytest.raises(PermissionError):
        prune_viewer_cache(task, roots, state_root=setup.tmp / "agent")
    assert setup.cache.exists()


def test_console_state_kept_when_cache_and_console_share_directory(setup):
    (setup.cache / "management").mkdir()
    (setup.cache / "management/db").write_text("keep")
    (setup.cache / "static/lifecycle").mkdir()
    (setup.cache / "static/lifecycle/db").write_text("keep")
    prune_viewer_cache(
        {**setup.location, "shared_cache": False}, [setup.tmp], state_root=setup.cache / "management"
    )
    assert not (setup.cache / "static/evidence.json").exists()
    assert (setup.cache / "static/lifecycle/db").read_text() == "keep"
    assert (setup.cache / "management/db").read_text() == "keep"


def test_node_cleanup_keeps_final_derived_output_and_unacknowledged_work(tmp_path):
    state = tmp_path / "agent"
    job_id = str(uuid.uuid4())
    attempt = str(uuid.uuid4())
    work = state / "executions" / attempt
    work.mkdir(parents=True)
    final = tmp_path / "derived"
    final.mkdir()
    (final / "data").write_text("keep")
    staging = tmp_path / f".dp-{job_id}-{attempt}"
    (staging / "source-cache").mkdir(parents=True)
    (staging / "data").mkdir()
    marker = {"job": {"job_id": job_id}, "staging": str(staging), "acknowledged": False}
    (work / "marker.json").write_text(json.dumps(marker))
    with pytest.raises(RuntimeError):
        prune_node_job_cache(state, {}, [job_id], writable_roots=[tmp_path])
    assert (staging / "source-cache").exists()
    marker["acknowledged"] = True
    (work / "marker.json").write_text(json.dumps(marker))
    prune_node_job_cache(state, {}, [job_id], writable_roots=[tmp_path])
    assert not work.exists() and not (staging / "source-cache").exists()
    assert (final / "data").read_text() == "keep" and (staging / "data").exists()


def test_central_cache_current_history_backups_only(tmp_path):
    key = str(uuid.uuid4())
    root = tmp_path / "cache"
    owned = [root / key, root / ".history" / key, root / f".{key}.backup-one", root / f".{key}.stale-one"]
    for path in owned:
        path.mkdir(parents=True)
    other = root / "other"
    other.mkdir()
    prune_server_location_cache(root, key)
    assert all(not path.exists() for path in owned) and other.exists()


def test_aliases_removed_but_independent_derived_input_blocks_deletion(setup, monkeypatch):
    _, local = setup.store.enroll_node(
        name="local-test",
        capabilities={},
        hostname="test",
        allowed_roots=[str(setup.tmp)],
        writable_roots=[str(setup.tmp)],
        enrollment_token="t",
        expected_token="t",
    )
    cache = setup.manager.cache_root / setup.location["location_id"]
    alias = setup.store.sync_locations(
        local["node_id"],
        [{"root": str(cache / "remote_source"), "dataset_key": "node/source", "output_dir": str(cache)}],
    )[0]
    derived = setup.store.sync_locations(
        local["node_id"],
        [
            {
                "root": str(cache / "derived"),
                "dataset_key": "node/derived",
                "output_dir": str(setup.tmp / "derived-cache"),
            }
        ],
    )[0]
    task = confirmed(setup, monkeypatch)
    assert setup.store.get_location(alias["location_id"])["state"] == "deleting"
    assert setup.store.get_location(derived["location_id"])["state"] == "available"
    with pytest.raises(ValueError, match="independent"):
        setup.manager.begin_ack(setup.node["node_id"], task["token"])


def test_machine_routes_auth_and_retry_ack(setup, monkeypatch):
    app = Flask(__name__)
    callback = []
    ledger = setup.tmp / "ledger"
    ledger.mkdir()
    register_source_cleanup_routes(
        app,
        SimpleNamespace(
            control_plane_store=setup.store,
            lifecycle_store=SimpleNamespace(root=ledger),
            remote_cache_root=setup.manager.cache_root,
            unregister_missing_dataset=callback.append,
        ),
    )
    client = app.test_client()
    headers = {"Authorization": f"Bearer {setup.token}"}
    assert client.get("/api/agents/source-cleanup").status_code == 403
    assert client.get("/api/agents/source-cleanup", headers=headers).status_code == 200
    task = confirmed(setup, monkeypatch)
    cache = setup.manager.cache_root / setup.location["location_id"]
    (cache / "static").mkdir(parents=True)
    path = f"/api/agents/source-cleanup/{task['token']}/ack"
    assert client.post(path, headers=headers, json=["bad"]).status_code == 409
    monkeypatch.setenv("DATA_PLATFORM_RELEASE", "new")
    assert client.get("/api/agents/source-cleanup", headers=headers).status_code == 403
    monkeypatch.delenv("DATA_PLATFORM_RELEASE")
    result = client.post(path, headers=headers, json={"status": "done"})
    assert result.status_code == 200, result.json
    assert result.json["status"] == "done" and not cache.exists() and len(callback) == 1
    assert client.post(path, headers=headers, json={"status": "done"}).json["status"] == "done"


def test_worker_cleans_only_after_current_node_is_idle(setup, monkeypatch):
    task = confirmed(setup, monkeypatch)
    from threading import Event

    worker = SourceCleanupWorker.__new__(SourceCleanupWorker)
    worker.agent = SimpleNamespace(
        allowed_roots=[setup.tmp], writable_roots=[setup.tmp], state_path=setup.tmp / "agent/state.json"
    )
    worker.cursor_path = setup.tmp / "cursor.json"
    worker.stop = Event()
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/api/agents/source-cleanup":
            return {"checks": [], "next_cursor": "", "task": task}
        if path.endswith("/jobs"):
            return {"job_ids": [], "next_cursor": ""}
        return {"status": "done"}

    worker.request = request
    monkeypatch.setattr(
        "lerobot.data_platform.source_cleanup_agent.ExecutionSupervisor",
        lambda agent: SimpleNamespace(pending_here=lambda: True),
    )
    worker.once()
    assert setup.cache.exists() and len(calls) == 1
    monkeypatch.setattr(
        "lerobot.data_platform.source_cleanup_agent.ExecutionSupervisor",
        lambda agent: SimpleNamespace(pending_here=lambda: False),
    )
    worker.once()
    assert not setup.cache.exists() and calls[-1][1].endswith("/ack")


def test_maintenance_defers_proof_and_cleanup(setup, monkeypatch):
    from lerobot.data_platform.maintenance import set_maintenance

    body = missing(setup, monkeypatch)
    set_maintenance(setup.store, True)
    assert setup.manager.poll(setup.node)["task"] is None
    assert setup.manager.report(setup.node, body)["status"] == "maintenance"
    assert setup.store.list_locations()


def test_local_cache_alias_is_never_probed_as_an_original_source(setup):
    _, local = setup.store.enroll_node(
        name="local-cache",
        hostname="test",
        allowed_roots=[str(setup.tmp)],
        writable_roots=[str(setup.tmp)],
        enrollment_token="t",
        expected_token="t",
        capabilities={},
    )
    root = setup.manager.cache_root / setup.location["location_id"] / "remote_source"
    setup.store.sync_locations(
        local["node_id"],
        [{"root": str(root), "dataset_key": "remote/source", "output_dir": str(root.parent)}],
    )
    assert setup.manager.poll(local)["checks"] == []


def test_missing_secondary_merge_input_cancels_queued_job(setup, monkeypatch):
    other = setup.store.sync_locations(
        setup.node["node_id"],
        [
            {
                "root": str(setup.tmp / "other"),
                "dataset_key": "node/other",
                "output_dir": str(setup.tmp / "other-cache"),
            }
        ],
    )[0]
    job = setup.store.create_job(
        location_id=other["location_id"],
        requested_by=setup.actor["user_id"],
        operation="viewer.prepare",
        options={"source_location_ids": [setup.location["location_id"]]},
    )
    confirmed(setup, monkeypatch)
    assert setup.store.get_job(job["job_id"])["status"] == "cancelled"
    assert setup.store.get_location(other["location_id"])["state"] == "available"
    with pytest.raises(JobConflictError):
        setup.store.create_job(
            location_id=other["location_id"],
            requested_by=setup.actor["user_id"],
            operation="viewer.prepare",
            options={"source_location_ids": [setup.location["location_id"]]},
        )

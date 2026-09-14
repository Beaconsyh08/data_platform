"""Task onboarding, portable snapshots and curation regression coverage."""

import copy
import json
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from flask import Flask

from lerobot.data_platform.cli import get_default_output_dir, run_precompute
from lerobot.data_platform.lifecycle import LifecycleStore, materialize_manifest
from lerobot.data_platform.precompute.analysis import build_dataset_analysis
from lerobot.data_platform.precompute.annotation import compute_subtask_boundaries
from lerobot.data_platform.precompute.dataset_io import load_task_records
from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3
from lerobot.data_platform.routes import lifecycle as lifecycle_routes
from lerobot.data_platform.routes import tasks as task_routes
from lerobot.data_platform.task_catalog import (
    TaskConfigSnapshot,
    builtin_catalog,
    content_digest,
    task_inventory,
)
from lerobot.data_platform.task_text import cached_subtask_names, generate_subtask_text
from tests.datasets.test_lifecycle import _make_dataset, _snapshot, _write_jsonl

LAUNDRY_TASKS = [
    "Open the door of the washing machine below",
    "Close the door of the washing machine below",
    "Open the door of the clothes dryer above",
    "Close the door of the clothes dryer above",
    "Grasp the clothes to the washing machine",
]


def test_task_resolver_can_load_before_precompute_in_a_fresh_process():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from lerobot.data_platform.task_catalog import TaskConfigSnapshot; "
            "assert TaskConfigSnapshot.from_dict(None).resolve("
            "'Grasp the clothes to the washing machine').family == 'load_clothes'",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _catalog_with(store, definition):
    latest = store.tasks.catalogs()[0]
    tasks = [asdict(row) for row in latest.tasks if row.task_id != definition["task_id"]]
    return store.tasks.create_catalog(
        [*tasks, definition],
        expected_version_id=latest.catalog_version_id,
        created_by="test",
    )


def _fridge(**changes):
    return {
        "task_id": "open_fridge",
        "family": "open_door",
        "label": "Open the refrigerator door",
        "attributes": {"object": "door", "appliance": "refrigerator"},
        "aliases": ["Open the fridge"],
        "stage_strategy": "equal_time",
        "stage_count": 7,
        **changes,
    }


def _apply(store, key, texts, catalog, mappings=None):
    current = store.tasks.current(key)
    return store.tasks.apply(
        key,
        texts,
        catalog_version_id=catalog.catalog_version_id,
        mappings=mappings or {},
        expected_version_id=current.mapping_version_id if current else None,
        expected_inventory_digest=content_digest(task_inventory(texts)),
        created_by="test",
    )


def test_builtin_unifies_laundry_and_legacy_task_families():
    snapshot = TaskConfigSnapshot.from_dict(None)
    tasks = [snapshot.resolve(text) for text in LAUNDRY_TASKS]
    assert [task.family for task in tasks] == [
        "open_door",
        "close_door",
        "open_door",
        "close_door",
        "load_clothes",
    ]
    assert len({task.task_id for task in tasks}) == 5
    assert all(task.stage_strategy == "equal_time" for task in tasks)
    assert tasks[-1].attributes["destination"] == "washing_machine_interior"
    assert snapshot.resolve("Pick up the yellow duck").attributes["position_mode"] == "none"
    absolute = snapshot.resolve("Pick up the yellow duck on the left")
    relative = snapshot.resolve("Pick up the yellow duck to the left of the brown dog")
    assert absolute.family == relative.family == "pick"
    assert (absolute.scene, relative.scene) == ("directional_pick", "relational_pick")
    assert relative.attributes["reference"] == "brown dog"
    assert snapshot.resolve("Place object").family == "place"
    assert snapshot.resolve("Give the yellow duck to me").stage_count == 6
    assert snapshot.resolve("Fold the towel").status == "unmapped"
    assert snapshot.resolve("Grasp a new object").family == "unknown"


def test_catalog_alias_conflicts_explicit_mapping_and_optimistic_lock(tmp_path):
    store = LifecycleStore(tmp_path / "ledger")
    catalog = _catalog_with(store, _fridge())
    second = _catalog_with(store, _fridge(task_id="open_fridge_above", attributes={"location": "above"}))
    assert TaskConfigSnapshot(second).resolve("Open the fridge").status == "conflict"
    with pytest.raises(ValueError, match="equivalent texts"):
        store.tasks.preview(
            "local/test",
            ["Open the fridge"],
            second.catalog_version_id,
            {"Open the fridge": "open_fridge", "OPEN the fridge.": "open_fridge_above"},
        )
    mapping = _apply(store, "local/test", ["Open the fridge"], second, {"Open the fridge": "open_fridge"})
    resolved = TaskConfigSnapshot.from_dict(mapping.snapshot).resolve("  OPEN the fridge. ")
    assert resolved.task_id == "open_fridge" and resolved.status == "mapped"
    with pytest.raises(ValueError, match="revision conflict"):
        store.tasks.create_catalog(
            [_fridge()], expected_version_id=catalog.catalog_version_id, created_by="test"
        )
    with pytest.raises(ValueError, match="revision conflict"):
        store.tasks.apply(
            "local/test",
            ["Open the fridge"],
            catalog_version_id=second.catalog_version_id,
            mappings={},
            expected_version_id=None,
            expected_inventory_digest=content_digest(["open the fridge"]),
            created_by="test",
        )
    with pytest.raises(ValueError, match="inventory conflict"):
        store.tasks.apply(
            "local/test",
            ["Open the fridge", "New task"],
            catalog_version_id=second.catalog_version_id,
            mappings={},
            expected_version_id=mapping.mapping_version_id,
            expected_inventory_digest=mapping.task_inventory_digest,
            created_by="test",
        )
    assert store.tasks.current("local/test").mapping_version_id == mapping.mapping_version_id


def test_task_index_reordering_and_snapshot_integrity(tmp_path):
    store = LifecycleStore(tmp_path / "ledger")
    texts = [{"task_index": 0, "task": LAUNDRY_TASKS[0]}, {"task_index": 1, "task": LAUNDRY_TASKS[4]}]
    mapping = _apply(store, "local/test", texts, builtin_catalog())
    reordered = [{**row, "task_index": 1 - row["task_index"]} for row in texts]
    assert content_digest(task_inventory(reordered)) == mapping.task_inventory_digest
    snapshot = TaskConfigSnapshot.from_dict(mapping.snapshot)
    assert snapshot.resolve(reordered[0]["task"]).family == "open_door"
    damaged = copy.deepcopy(mapping.snapshot)
    damaged["catalog"]["tasks"][0]["family"] = "changed"
    with pytest.raises(ValueError, match="digest mismatch"):
        TaskConfigSnapshot.from_dict(damaged)
    damaged = {**mapping.snapshot, "protocol_version": 999}
    damaged["digest"] = content_digest({key: value for key, value in damaged.items() if key != "digest"})
    with pytest.raises(ValueError, match="unsupported task configuration protocol"):
        TaskConfigSnapshot.from_dict(damaged)


@pytest.mark.parametrize("convert_v3", [False, True])
def test_task_curation_snapshot_survives_catalog_upgrade_and_materialization(
    tmp_path, convert_v3, monkeypatch
):
    root = tmp_path / "dataset"
    _make_dataset(root)
    _write_jsonl(root / "meta" / "tasks.jsonl", [{"task_index": 0, "task": "Open the fridge"}])
    _write_jsonl(
        root / "meta" / "episodes.jsonl",
        [
            {"episode_index": index, "tasks": ["Open the fridge"], "length": length}
            for index, length in [(0, 2), (1, 3)]
        ],
    )
    if convert_v3:
        root = run_convert_v3(root, out_root=tmp_path / "dataset_v3", workers=1).out_root
    before = _snapshot(root)
    store = LifecycleStore(tmp_path / "ledger")
    base = store.ingest(root, "local/fridge")
    catalog = _catalog_with(store, _fridge())
    _apply(store, base.dataset_key, load_task_records(root), catalog)
    profile = store.create_dataset_profile(base.version_id)
    assert profile.distributions["task_family"] == {"open_door": 2}
    cohort = store.resolve_cohort(
        base.version_id, {"metadata": {"task_attributes.appliance": "refrigerator"}}
    )
    assert cohort["episode_count"] == 2
    requirement = store.create_requirement("doors", dimensions={"task_family": ["open_door"]})
    recipe = store.create_recipe(
        "door-selection",
        base.version_id,
        requirement_id=requirement.requirement_id,
        cohort_query_snapshot=cohort,
        composition={"group_by": "task_family", "target_counts": {"open_door": 1}},
    )
    newer = _catalog_with(store, _fridge(family="inspect_door", stage_count=3))
    _apply(store, base.dataset_key, load_task_records(root), newer)
    assert store.validate_recipe(recipe.recipe_id)["valid"]
    assert profile.distributions["task_family"] == {"open_door": 2}
    workspace = store.compile_recipe(recipe.recipe_id)
    assert (
        workspace.rule_versions["task_config"]["catalog"]["catalog_version_id"] == catalog.catalog_version_id
    )
    manifest = store.publish_workspace(
        workspace.workspace_id, expected_revision=workspace.revision, reviewer="test"
    )
    output, _ = materialize_manifest(store, manifest.manifest_id, tmp_path / "curated", workers=1)
    assert store.tasks.snapshot(output.dataset_key).resolve("Open the fridge").family == "open_door"
    assert len(output.episode_refs) == 1
    assert output.episode_uids() <= base.episode_uids()
    assert _snapshot(root) == before

    # Reopening the same materialization through the console also prepares the
    # Viewer with the inherited snapshot, even after the source configuration changed.
    monkeypatch.setattr(
        lifecycle_routes,
        "threading",
        SimpleNamespace(Thread=lambda target, **kwargs: SimpleNamespace(start=target)),
    )
    ctx = SimpleNamespace(
        lifecycle_store=store,
        jobs_registry={},
        jobs_lock=threading.Lock(),
        append_operation_log=None,
        meta_only_dataset_cls=lambda repo_id, root: SimpleNamespace(repo_id=repo_id, root=root),
        register_dataset=lambda dataset, path: tuple(dataset.repo_id.split("/", 1)),
        repo_id_from_key=lambda key: "/".join(key),
        serialize_job=lambda job: job,
        update_job=lambda job, payload: job.update(payload),
        finish_job=lambda job, message, **kwargs: job.update(status="done", **kwargs),
        fail_job=lambda job, message, exc: pytest.fail(str(exc)),
    )
    app = Flask(__name__)
    lifecycle_routes.register_lifecycle_routes(app, ctx)
    response = app.test_client().post(
        "/api/lifecycle/materialize/start",
        json={"manifest_id": manifest.manifest_id, "out_root": str(tmp_path / "curated"), "workers": 1},
    )
    assert response.status_code == 200 and response.get_json()["job"]["status"] == "done"
    static = get_default_output_dir(tmp_path / "curated") / "static"
    viewer_config = json.loads((static / "viewer_manifest.json").read_text())["task_config"]
    assert viewer_config["digest"] == store.tasks.snapshot(output.dataset_key).to_dict()["digest"]
    assert cached_subtask_names("Open the fridge", static, 0)[1][6] == "Stage 7/7"
    assert _snapshot(root) == before


def test_no_matches_never_turn_into_all_episodes_and_multitask_grouping_is_rejected(tmp_path):
    root = tmp_path / "dataset"
    _make_dataset(root)
    store = LifecycleStore(tmp_path / "ledger")
    base = store.ingest(root, "local/test")
    empty = store.resolve_cohort(base.version_id, {"metadata": {"task_family": "open_door"}})
    with pytest.raises(ValueError, match="no episodes"):
        store.create_recipe("empty", base.version_id, cohort_query_snapshot=empty)
    _write_jsonl(
        root / "meta" / "episodes.jsonl",
        [
            {"episode_index": index, "tasks": ["Pick up the cube", "Place object"], "length": length}
            for index, length in [(0, 2), (1, 3)]
        ],
    )
    base = store.ingest(root, "local/multitask")
    with pytest.raises(ValueError, match="exactly one task_family"):
        store.create_recipe(
            "multi", base.version_id, composition={"group_by": "task_family", "target_counts": {"pick": 1}}
        )


def test_custom_stage_counts_match_cache_analysis_and_viewer(tmp_path):
    root = tmp_path / "dataset"
    _make_dataset(root)
    _write_jsonl(
        root / "meta" / "episodes.jsonl",
        [
            {"episode_index": index, "tasks": ["Open the fridge"], "length": length}
            for index, length in [(0, 2), (1, 3)]
        ],
    )
    store = LifecycleStore(tmp_path / "ledger")
    catalog = _catalog_with(store, _fridge())
    config = TaskConfigSnapshot(catalog).to_dict()
    output = tmp_path / "cache"
    before = _snapshot(root)
    run_precompute(
        root,
        repo_id="local/test",
        output_dir=output,
        prepare_videos=False,
        prepare_workers=1,
        visualize_only=True,
        task_config=config,
        show_progress=False,
    )
    static = output / "static"
    from lerobot.data_platform.cli import load_platform_metadata

    meta = load_platform_metadata(root, "local/test")
    analysis = build_dataset_analysis(root, meta, static, task_config=config)
    assert analysis["task_dimensions"]["task_family"][0]["key"] == "open_door"
    assert analysis["episodes"][0]["stage_counts"] == {"0": 1, "6": 1}
    assert "unknown_object" not in analysis["episodes"][0]["review_reasons"]
    count, names = cached_subtask_names("Open the fridge", static, 0)
    assert count == 6 and names[6] == "Stage 7/7"
    assert generate_subtask_text(LAUNDRY_TASKS[4], 2) == "Stage 3/5"
    assert _snapshot(root) == before
    run_precompute(
        root,
        repo_id="local/test",
        output_dir=output,
        prepare_videos=False,
        overwrite_csv=True,
        prepare_workers=1,
        visualize_only=True,
        show_progress=False,
    )
    assert cached_subtask_names("Open the fridge", static, 0)[1][6] == "Stage 7/7"
    # A manual transition overrides configuration and survives a cache rebuild.
    manual = {"0": [{"time": 0.05, "state": 2}]}
    (static / "subtask_annotations.json").write_text(json.dumps(manual))
    changed = _catalog_with(store, _fridge(stage_count=3))
    run_precompute(
        root,
        repo_id="local/test",
        output_dir=output,
        prepare_videos=False,
        prepare_workers=1,
        visualize_only=True,
        task_config=TaskConfigSnapshot(changed).to_dict(),
        show_progress=False,
    )
    assert json.loads((static / "subtask_annotations.json").read_text())["0"] == manual["0"]
    assert json.loads((static / "csv/episode_000000_ds1.stages.json").read_text())["stage_count"] == 3


def test_unconfigured_tasks_are_visible_and_semantic_gaps_are_not_corruption(tmp_path):
    meta = SimpleNamespace(episodes={0: {"tasks": ["Fold an unfamiliar towel"]}}, fps=10, features={})
    analysis = build_dataset_analysis(tmp_path, meta, tmp_path, [0])
    assert analysis["task_configuration_pending"] == 1
    assert analysis["episodes"][0]["task"] == "Fold an unfamiliar towel"
    assert analysis["episodes"][0]["review_reasons"] == []
    assert analysis["episodes"][0]["cache_status"] == "missing_csv"


def test_task_routes_preview_apply_audit_and_conflict(tmp_path, monkeypatch):
    root = tmp_path / "dataset"
    _make_dataset(root)
    store = LifecycleStore(tmp_path / "ledger")
    events = []

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(task_routes.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(task_routes, "run_precompute", lambda **kwargs: events.append(kwargs))
    ctx = SimpleNamespace(
        lifecycle_store=store,
        control_plane_store=None,
        datasets_index={("local", "test"): {"root": str(root), "output_dir": str(tmp_path / "cache")}},
        repo_key=lambda key: tuple(key.split("/", 1)),
        jobs_registry={},
        jobs_lock=threading.Lock(),
        append_operation_log=lambda *args, **kwargs: events.append(args[1]),
        clear_dataset_caches=lambda key: None,
        update_job=lambda job, payload: job.update(payload),
        finish_job=lambda job, message: job.update(status="done"),
        fail_job=lambda job, message, exc: pytest.fail(str(exc)),
    )
    app = Flask(__name__, template_folder=str(Path(task_routes.__file__).parents[1] / "templates"))
    task_routes.register_task_routes(app, ctx)
    client = app.test_client()
    assert client.get("/tasks?dataset_key=local/test").status_code == 200
    assert client.get("/api/task-catalogs").get_json()["catalogs"][0]["version"] == 0
    preview = client.post("/api/task-mappings/preview", json={"dataset_key": "local/test"}).get_json()
    assert preview["tasks"][0]["episode_count"] == 2
    assert preview["tasks"][0]["raw_task"] == load_task_records(root)[0]["task"]
    body = {"dataset_key": "local/test", "expected_inventory_digest": preview["task_inventory_digest"]}
    response = client.post("/api/task-mappings", json=body)
    assert response.status_code == 200
    assert response.get_json()["job"]["status"] == "done"
    assert "task_mapping_apply" in events
    assert next(item for item in events if isinstance(item, dict))["visualize_only"]
    assert client.post("/api/task-mappings", json=body).status_code == 409
    assert client.get("/api/task-mappings?dataset_key=missing/test").status_code == 404

    pending = []

    class DeferredThread(ImmediateThread):
        def start(self):
            pending.append(self.target)

    monkeypatch.setattr(task_routes.threading, "Thread", DeferredThread)
    body["expected_version_id"] = store.tasks.current("local/test").mapping_version_id
    first = client.post("/api/task-mappings", json=body)
    assert first.status_code == 200
    body["expected_version_id"] = first.get_json()["mapping"]["mapping_version_id"]
    second = client.post("/api/task-mappings", json=body)
    assert second.status_code == 409 and "cache conflict" in second.get_json()["error"]
    assert store.tasks.current("local/test").mapping_version_id == body["expected_version_id"]
    pending.pop(0)()
    assert client.post("/api/task-mappings", json=body).status_code == 200
    pending.pop(0)()


def test_stage_dispatch_does_not_use_pick_for_explicit_custom_task(tmp_path):
    store = LifecycleStore(tmp_path / "ledger")
    catalog = _catalog_with(store, _fridge(aliases=["Pick up the yellow duck"], stage_count=3))
    snapshot = TaskConfigSnapshot(catalog, {"pick up the yellow duck": "open_fridge"}).to_dict()
    boundaries, issues = compute_subtask_boundaries(
        np.linspace(0, 6, 7), np.zeros((7, 2)), None, 1, task="Pick up the yellow duck", task_config=snapshot
    )
    assert boundaries["equal_time"] and boundaries["num_stages"] == 3
    assert not issues


def test_agent_receives_portable_snapshot_and_matches_local_analysis(tmp_path):
    from lerobot.data_platform.agent import discover_datasets
    from tests.datasets.test_data_platform_agent import _agent

    root = tmp_path / "dataset"
    _make_dataset(root)
    _write_jsonl(root / "meta/tasks.jsonl", [{"task_index": 0, "task": LAUNDRY_TASKS[4]}])
    _write_jsonl(
        root / "meta/episodes.jsonl",
        [
            {"episode_index": index, "tasks": [LAUNDRY_TASKS[4]], "length": length}
            for index, length in [(0, 2), (1, 3)]
        ],
    )
    locations = discover_datasets([root], node_name="test")
    assert locations[0]["metadata"]["tasks"][0]["task"] == LAUNDRY_TASKS[4]
    assert len(locations[0]["metadata"]["episodes"]) == 2
    agent = _agent(tmp_path, root, tmp_path)
    assert agent.capabilities()["task_config_protocol"] == 1
    config = TaskConfigSnapshot.from_dict(None).to_dict()
    cache = tmp_path / "agent-cache"
    agent.execute_job(
        {
            "job_id": "task-cache",
            "operation": "viewer.prepare",
            "options": {"task_config": config, "prepare_videos": False},
            "location": {"dataset_key": "local/test", "root": str(root), "output_dir": str(cache)},
        }
    )
    uploaded = {str(item[1]): item[2] for item in agent.client.uploads}
    assert str(agent.client.uploads[-1][1]) == "viewer_manifest.json"
    manifest = json.loads(uploaded["viewer_manifest.json"])
    assert manifest["task_config"] == config
    from lerobot.data_platform.cli import load_platform_metadata

    local = build_dataset_analysis(
        root, load_platform_metadata(root, "local/test"), cache / "static", task_config=config
    )
    remote_meta = SimpleNamespace(
        episodes={row["episode_index"]: row for row in manifest["episodes"]},
        total_episodes=manifest["total_episodes"],
        fps=manifest["fps"],
        features=manifest["features"],
    )
    remote = build_dataset_analysis(
        Path("/unavailable"), remote_meta, cache / "static", task_config=manifest["task_config"]
    )
    assert local["task_dimensions"] == remote["task_dimensions"]
    assert remote["task_families"] == ["load_clothes"]


def test_remote_mapping_upgrade_protocol_and_late_cache_completion(tmp_path):
    from lerobot.data_platform.control_plane import ControlPlaneStore
    from lerobot.data_platform.routes.control_plane import (
        register_control_plane_auth_routes,
        register_control_plane_routes,
    )
    from tests.datasets.test_control_plane import _bootstrap

    lifecycle = LifecycleStore(tmp_path / "ledger")
    control = ControlPlaneStore(f"sqlite:///{tmp_path / 'control.db'}")
    app = Flask(__name__)
    register_control_plane_auth_routes(
        app, control, bootstrap_token="bootstrap-secret", allow_registration=True
    )
    promoted = []
    register_control_plane_routes(
        app,
        control,
        enrollment_token="enrollment",
        remote_cache_root=tmp_path / "remote-cache",
        register_remote_cache=lambda location, path: promoted.append(path) or "/viewer",
        task_catalog_store=lifecycle.tasks,
    )
    ctx = SimpleNamespace(
        lifecycle_store=lifecycle,
        control_plane_store=control,
        append_operation_log=lambda *args, **kwargs: None,
    )
    task_routes.register_task_routes(app, ctx)
    client = app.test_client()
    assert _bootstrap(client).status_code == 200
    token, node = control.enroll_node(
        name="test",
        hostname="test",
        allowed_roots=["/remote"],
        writable_roots=["/remote"],
        capabilities={},
        enrollment_token="enrollment",
        expected_token="enrollment",
    )
    location = control.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "node-test/doors",
                "root": "/remote/doors",
                "output_dir": "/remote/cache",
                "metadata": {
                    "tasks": [{"task_index": 0, "task": "Open the fridge"}],
                    "episodes": [{"episode_index": 0, "tasks": ["Open the fridge"]}],
                },
            }
        ],
    )[0]
    url = f"/api/control/locations/{location['location_id']}/viewer-jobs"
    assert client.post(url, json={}).status_code == 409
    control.heartbeat(node["node_id"], capabilities={"task_config_protocol": 1})
    catalog = _catalog_with(lifecycle, _fridge())
    first_mapping = _apply(lifecycle, location["dataset_key"], ["Open the fridge"], catalog)
    response = client.post(url, json={})
    assert response.status_code == 202
    first_job = response.get_json()["job"]
    assert client.post(url, json={}).get_json()["job"]["job_id"] == first_job["job_id"]
    assert control.claim_job(node["node_id"])["job_id"] == first_job["job_id"]
    catalog = _catalog_with(lifecycle, _fridge(stage_count=3))
    second_mapping = _apply(lifecycle, location["dataset_key"], ["Open the fridge"], catalog)
    second_job = client.post(url, json={}).get_json()["job"]
    assert second_job["job_id"] != first_job["job_id"]
    assert control.claim_job(node["node_id"]) is None
    headers = {"Authorization": f"Bearer {token}"}
    for job, mapping in [(first_job, first_mapping), (second_job, second_mapping)]:
        if job == second_job:
            assert control.claim_job(node["node_id"])["job_id"] == second_job["job_id"]
        manifest = {"task_config": mapping.snapshot, "episodes": [{"episode_index": 0}]}
        upload = client.put(
            f"/api/agents/jobs/{job['job_id']}/artifacts/viewer_manifest.json",
            headers=headers,
            data=json.dumps(manifest),
        )
        assert upload.status_code == 200
        complete = client.post(
            f"/api/agents/jobs/{job['job_id']}/complete",
            headers=headers,
            json={"status": "done", "result": {}},
        )
        assert complete.status_code == 200
    assert len(promoted) == 1
    assert promoted[0] == tmp_path / "remote-cache" / ".jobs" / second_job["job_id"]
    assert control.get_job(first_job["job_id"])["result"]["task_config_stale"]
    assert (
        client.get("/api/task-mappings?dataset_key=node-test/doors").get_json()["tasks"][0]["family"]
        == "open_door"
    )

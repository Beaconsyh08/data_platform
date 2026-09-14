"""Analysis must run before Viewer/CSV preparation, locally and through the control plane."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from lerobot.data_platform import viewer
from lerobot.data_platform.agent import discover_datasets
from lerobot.data_platform.lifecycle import LifecycleStore
from lerobot.data_platform.precompute.analysis import (
    AnalysisMetadata,
    analysis_input_digest,
    build_dataset_analysis,
)
from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3
from lerobot.data_platform.routes.analysis import register_remote_analysis_routes
from lerobot.data_platform.task_catalog import TaskConfigSnapshot, builtin_catalog
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_lifecycle import _make_dataset, _snapshot, _write_jsonl

DOOR = "Open the door of the washing machine below"


def _report():
    return {
        "fps": 10,
        "total_episodes": 2,
        "total_frames": 150,
        "features": {},
        "tasks": [{"task_index": 42, "task": DOOR}],
        "episodes": [
            {"episode_index": 3, "task_index": 42, "length": 60},
            {
                "episode_index": 9,
                "tasks": ["Fold a towel"],
                "dataset_from_index": 60,
                "dataset_to_index": 150,
            },
        ],
    }


def test_metadata_analysis_preserves_sparse_ids_and_counts_without_cache(tmp_path):
    meta = AnalysisMetadata.from_report(_report())
    result = build_dataset_analysis(tmp_path / "source", meta, tmp_path / "no-cache")
    assert result["total_frames"] == 150
    assert result["total_duration_seconds"] == 15
    assert result["duration_episode_count"] == 2
    assert [row["episode_id"] for row in result["episodes"]] == [3, 9]
    assert [row["duration_seconds"] for row in result["episodes"]] == [6, 9]
    assert result["episodes"][0]["task_families"] == ["open_door"]
    assert result["episodes"][1]["task_status"] == "pending"
    assert all(row["review_reasons"] == [] for row in result["episodes"])
    assert result["cached_episodes"] == 0
    assert result["stage_distribution"] == []
    assert not (tmp_path / "no-cache").exists()
    # Task indices are local lookup keys; swapping the table indices must not change task identity.
    report = _report()
    report["tasks"][0]["task_index"] = 100
    report["episodes"][0]["task_index"] = 100
    reordered = build_dataset_analysis(tmp_path, AnalysisMetadata.from_report(report), None)
    assert reordered["task_dimensions"] == result["task_dimensions"]


def test_optional_sampled_csv_does_not_replace_metadata_totals_or_hide_duration(tmp_path):
    csv = tmp_path / "csv"
    csv.mkdir()
    path = csv / "episode_000003_ds30.csv"
    path.write_text("timestamp,stage,exist_label\n0,0,1\n3,1,0\n")
    meta = AnalysisMetadata.from_report(_report())
    digest = analysis_input_digest(meta, tmp_path)
    result = build_dataset_analysis(tmp_path, meta, tmp_path)
    assert result["total_frames"] == 150
    assert result["analyzed_frames"] == 2
    assert result["total_duration_seconds"] == 15
    assert result["episodes"][0]["frames"] == 60
    assert result["episodes"][0]["cache_status"] == "sampled"
    assert result["episodes"][1]["duration_bucket"] == "5-10s"
    assert sum(row["percent"] for row in result["stage_distribution"]) == 100
    path.write_text("timestamp,stage\n0,0\n1,0.5\n2,1\n")
    assert analysis_input_digest(meta, tmp_path) != digest
    meta.episodes[3]["length"] = 80
    assert analysis_input_digest(meta, None) != analysis_input_digest(
        AnalysisMetadata.from_report(_report()), None
    )


def test_new_stage_configuration_uses_current_tasks_without_requiring_cache_rebuild(tmp_path):
    old = builtin_catalog()
    task = next(task for task in old.tasks if DOOR in task.aliases)
    changed = replace(task, stage_count=7)
    current = replace(old, tasks=tuple(changed if row.task_id == task.task_id else row for row in old.tasks))
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv/episode_000003_ds1.csv").write_text("timestamp,stage\n0,0\n1,1\n")
    result = build_dataset_analysis(
        tmp_path,
        AnalysisMetadata.from_report(_report()),
        tmp_path,
        task_config=TaskConfigSnapshot(current).to_dict(),
        cached_task_config=TaskConfigSnapshot(old).to_dict(),
    )
    assert result["episodes"][0]["resolved_tasks"][0]["stage_count"] == 7
    assert result["annotation_cache_stale"]
    assert result["episodes"][0]["stage_counts"] == {}
    assert result["episodes"][0]["review_reasons"] == []
    assert result["total_duration_seconds"] == 15


@pytest.mark.parametrize("v3", [False, True])
def test_local_analysis_routes_run_without_precompute_and_refresh_metadata(tmp_path, monkeypatch, v3):
    root = tmp_path / "dataset"
    _make_dataset(root)
    if v3:
        root = run_convert_v3(root, out_root=tmp_path / "dataset_v3", workers=1).out_root
    before = _snapshot(root)
    static = tmp_path / "console/static"
    static.mkdir(parents=True)
    app = viewer.run_server(
        dataset=viewer.MetaOnlyDataset("local/test", root=root),
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
        start_server=False,
    )
    app.config["TESTING"] = True
    from lerobot.data_platform import cli

    monkeypatch.setattr(cli, "run_precompute", lambda *a, **k: pytest.fail("Analysis queued Prepare cache"))
    client = app.test_client()
    assert client.get("/local/test/analysis").status_code == 200
    for endpoint in ("summary", "refresh"):
        response = client.get(f"/api/analysis/local/test/{endpoint}")
        assert response.status_code == 200, response.get_data(as_text=True)
        summary = response.get_json()["summary"]
        assert summary["total_frames"] == 5
        assert summary["total_duration_seconds"] == 0.5
        assert summary["cached_episodes"] == 0
    rows = client.get("/api/analysis/local/test/episodes").get_json()["episodes"]
    assert [row["frames"] for row in rows] == [2, 3]
    assert _snapshot(root) == before
    assert not list(static.rglob("*.csv"))
    assert not list(static.rglob("*.mp4"))
    if not v3:
        _write_jsonl(
            root / "meta/episodes.jsonl",
            [
                {"episode_index": 0, "tasks": [DOOR], "length": 20},
                {"episode_index": 1, "tasks": ["Fold a towel"], "length": 30},
            ],
        )
        changed = client.get("/api/analysis/local/test/refresh").get_json()
        assert changed["summary"]["total_frames"] == 50
        assert changed["episodes"][0]["task_families"] == ["open_door"]
        reread = client.get("/api/analysis/local/test/episodes").get_json()["episodes"]
        assert [row["frames"] for row in reread] == [20, 30]


def _remote_context(tmp_path):
    control = _store(tmp_path)
    app = _app(tmp_path, control)
    _, node = control.enroll_node(
        name="metadata-node",
        hostname="unreachable.invalid",
        allowed_roots=["/remote/source"],
        writable_roots=["/remote"],
        capabilities={},
        enrollment_token="token",
        expected_token="token",
    )
    location = control.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "node-test/laundry",
                "root": "/remote/source",
                "metadata": _report(),
            }
        ],
    )[0]
    context = SimpleNamespace(
        control_plane_store=control,
        lifecycle_store=LifecycleStore(tmp_path / "lifecycle"),
        repo_key=lambda key: tuple(key.split("/", 1)),
        analysis_with_live_tags=None,
        static_dir_for_key=lambda key: pytest.fail("Unprepared dataset must not access a Viewer cache"),
    )
    register_remote_analysis_routes(app, context)
    return app, control, location


def test_remote_analysis_uses_synced_metadata_without_jobs_or_viewer_and_stays_current(tmp_path, monkeypatch):
    app, control, location = _remote_context(tmp_path)
    client = app.test_client()
    url = f"/api/control/locations/{location['location_id']}/analysis"
    assert client.get(url + "/summary").status_code == 401
    assert _bootstrap(client).status_code == 200
    monkeypatch.setattr(control, "create_job", lambda **kwargs: pytest.fail("Analysis created a remote job"))
    page = client.get(f"/remote/{location['location_id']}/analysis")
    assert page.status_code == 200
    assert url in page.get_data(as_text=True)
    for endpoint in ("summary", "episodes", "refresh"):
        response = client.get(url + "/" + endpoint)
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["summary"]["total_frames"] == 150
        assert payload["summary"]["remote_source"]["metadata_complete"]
        assert payload["episodes"][0]["viewer_url"] is None
    report = _report()
    report["episodes"][0]["length"] = 120
    report["total_frames"] = 210
    control.sync_locations(location["node_id"], [{**location, "metadata": report}])
    assert client.get(url + "/refresh").get_json()["summary"]["total_frames"] == 210
    assert not control.list_jobs()
    assert client.get("/api/control/locations/missing/analysis/summary").status_code == 404


def test_older_agent_shows_known_totals_without_inventing_episode_lengths(tmp_path):
    app, control, location = _remote_context(tmp_path)
    report = _report()
    report["episodes"] = [
        {"episode_index": 3, "task_index": 42},
        {"episode_index": 9, "tasks": ["Fold a towel"]},
    ]
    control.sync_locations(location["node_id"], [{**location, "metadata": report}])
    client = app.test_client()
    _bootstrap(client)
    payload = client.get(f"/api/control/locations/{location['location_id']}/analysis/summary").get_json()
    assert payload["summary"]["total_frames"] == 150
    assert payload["summary"]["total_duration_seconds"] == 15
    assert not payload["summary"]["remote_source"]["metadata_complete"]
    assert payload["summary"]["duration_episode_count"] == 0
    assert all(row["duration_source"] == "unavailable" for row in payload["episodes"])
    assert not control.list_jobs()


@pytest.mark.parametrize("v3", [False, True])
def test_agent_sync_reports_episode_lengths_without_preparation(tmp_path, v3):
    root = tmp_path / "dataset"
    _make_dataset(root)
    if v3:
        root = run_convert_v3(root, out_root=tmp_path / "v3", workers=1).out_root
    before = _snapshot(root)
    report = discover_datasets([root], node_name="test")[0]["metadata"]
    assert report["analysis_metadata_version"] == 1
    assert [row["length"] for row in report["episodes"]] == [2, 3]
    assert _snapshot(root) == before
    local = build_dataset_analysis(root, viewer.MetaOnlyDataset("local/test", root=root).meta, None)
    remote = build_dataset_analysis(Path("/inaccessible"), AnalysisMetadata.from_report(report), None)
    for key in ("task_dimensions", "duration_distribution", "total_frames", "total_duration_seconds"):
        assert remote[key] == local[key]


def test_unreadable_optional_csv_keeps_metadata_statistics(tmp_path):
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv/episode_000003_ds1.csv").write_text("")
    result = build_dataset_analysis(tmp_path, AnalysisMetadata.from_report(_report()), tmp_path)
    assert result["total_frames"] == 150
    assert result["total_duration_seconds"] == 15
    assert result["episodes"][0]["review_reasons"] == ["csv_read_error"]
    assert result["episodes"][1]["review_reasons"] == []


def test_full_console_registers_remote_analysis_for_viewer_role(tmp_path, monkeypatch):
    _, control, location = _remote_context(tmp_path)
    monkeypatch.setenv("DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-secret")
    static = tmp_path / "full-console/static"
    static.mkdir(parents=True)
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
        static_folder=static,
        template_folder=Path(viewer.__file__).parent / "templates",
        database_url=f"sqlite:///{tmp_path / 'control-plane.db'}",
        start_server=False,
    )
    client = app.test_client()
    assert _bootstrap(client).status_code == 200
    control.register_user(username="analysis-viewer", password="viewer-password", display_name="Viewer")
    client.post("/api/auth/logout", json={})
    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": "analysis-viewer",
                "password": "viewer-password",
            },
        ).status_code
        == 200
    )
    assert client.get(f"/remote/{location['location_id']}/analysis").status_code == 200
    response = client.get(f"/api/control/locations/{location['location_id']}/analysis/refresh")
    assert response.status_code == 200
    assert response.get_json()["summary"]["total_frames"] == 150
    assert not control.list_jobs()

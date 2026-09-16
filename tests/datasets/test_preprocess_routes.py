import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from lerobot.data_platform.precompute.preprocess.common import PreprocessResult
from lerobot.data_platform.routes import preprocess as preprocess_routes


class _ImmediateThread:
    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


def _route_context(src_root: Path, *, legacy: bool = False):
    # These route doubles model legacy DVT jobs; declare that policy explicitly.
    from lerobot.data_platform.precompute.data_profile import profile_from_data_version, write_data_profile

    if legacy:
        write_data_profile(
            src_root, profile_from_data_version("DVT2", {}, resolution_source="test_fixture", confirmed=True)
        )
    jobs = {}
    lock = threading.Lock()

    def update_job(job, payload):
        job.update(payload)
        total = job.get("total") or 0
        current = job.get("current") or 0
        job["progress"] = int(current / total * 100) if total else 0
        if payload.get("message"):
            append_job_log(job, payload["message"])

    def finish_job(job, message, **updates):
        job.update(status="done", progress=100, message=message, **updates)
        append_job_log(job, message)

    def fail_job(job, message, exc):
        job.update(status="error", message=message, error=str(exc))

    def append_job_log(job, message):
        job.setdefault("logs", []).append({"time": "00:00:00", "message": message})

    return SimpleNamespace(
        datasets_index={
            ("local", "source"): {
                "repo_id": "local/source",
                "root": str(src_root),
                "output_dir": str(src_root / "vis"),
            }
        },
        jobs_registry=jobs,
        jobs_lock=lock,
        dataset_key_from_body=lambda body: tuple(body["dataset_key"].split("/", 1)),
        repo_id_from_key=lambda key: "/".join(key),
        bool_option=lambda options, key, default: bool(options.get(key, default)),
        update_job=update_job,
        finish_job=finish_job,
        fail_job=fail_job,
        append_job_log=append_job_log,
        serialize_job=lambda job: dict(job),
    )


@pytest.mark.parametrize("mapping_mode", ["names", "indices"])
@pytest.mark.parametrize("policy", ["min", "pad"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_merge_min_route_rebuilds_cache_before_registration_and_reports_plan(
    tmp_path: Path, monkeypatch, dry_run, policy, mapping_mode
):
    mapping = (
        {"dimension_names": [{"action": ["a", "b"]}] * 2}
        if mapping_mode == "names"
        else {"dimension_indices": [{"action": [0, 1]}] * 2}
    )
    sources = [tmp_path / "source", tmp_path / "second"]
    info = {
        "fps": 10,
        "robot_type": "test",
        "features": {"action": {"dtype": "float32", "shape": [2], "names": ["left", "right"]}},
    }
    for source in sources:
        (source / "meta").mkdir(parents=True)
        (source / "data").mkdir()
        (source / "meta/info.json").write_text(json.dumps(info))
    ctx = _route_context(sources[0])
    ctx.datasets_index[("local", "second")] = {"root": str(sources[1])}
    ctx.repo_key = lambda value: tuple(value.split("/", 1))
    ctx.parse_int_list = lambda value: value
    ctx.ensure_dataset_loaded = lambda key: (
        SimpleNamespace(root=Path(ctx.datasets_index[key]["root"]), total_episodes=1),
        None,
    )
    ctx.meta_only_dataset_cls = lambda repo_id, root: SimpleNamespace(repo_id=repo_id, root=root)
    ctx.lifecycle_store = None
    ctx.append_operation_log = None
    events = []
    ctx.register_dataset = lambda *_args, **_kwargs: events.append("register") or ("local", "merged")
    output = tmp_path / "merged"
    summary = {
        "dimension_alignment": [{"source_position": 1, "fields": {"action": {"source_indices": [1, 0]}}}]
    }

    def merge(roots, **kwargs):
        assert roots == sources
        assert kwargs["dimension_policy"] == policy
        assert all(kwargs[key] == value for key, value in mapping.items())
        assert kwargs["padding_value"] == (2 if policy == "pad" else 0)
        assert kwargs["dry_run"] == dry_run
        events.append("merge")
        kwargs["progress_callback"]({"status": "done", "current": 2, "total": 2})
        assert next(iter(ctx.jobs_registry.values()))["status"] == "running"
        if not dry_run:
            (output / "meta").mkdir(parents=True)
            (output / "meta/info.json").write_text(json.dumps(info))
        return PreprocessResult(
            op="merge",
            src_roots=roots,
            out_root=output,
            repo_id="local/merged",
            dry_run=dry_run,
            summary=summary,
        )

    def precompute(**kwargs):
        assert kwargs["root"] == output
        assert kwargs["prepare_csv"] is True
        events.append("cache")
        kwargs["progress_callback"]({"status": "done", "current": 2, "total": 2})
        assert next(iter(ctx.jobs_registry.values()))["status"] == "running"

    monkeypatch.setattr(preprocess_routes, "run_merge", merge)
    monkeypatch.setattr(preprocess_routes, "run_precompute", precompute)
    monkeypatch.setattr(preprocess_routes.threading, "Thread", _ImmediateThread)
    app = Flask(__name__)
    preprocess_routes.register_preprocess_routes(app, ctx)
    response = app.test_client().post(
        "/api/preprocess/merge/start",
        json={
            "options": {
                "src_keys": ["local/source", "local/second"],
                "out_root": str(output),
                "dimension_policy": policy,
                **mapping,
                "padding_value": 2 if policy == "pad" else 0,
                "dry_run": dry_run,
            }
        },
    )
    assert response.status_code == 200
    job = response.get_json()["job"]
    assert job["status"] == "done"
    assert job["result_summary"]["dimension_alignment"] == summary["dimension_alignment"]
    assert events == (["merge"] if dry_run else ["merge", "cache", "register"])


def test_merge_route_rejects_unknown_dimension_policy_before_starting_job(tmp_path: Path):
    source = tmp_path / "source"
    (source / "meta").mkdir(parents=True)
    (source / "data").mkdir()
    (source / "meta/info.json").write_text(json.dumps({"features": {}}))
    ctx = _route_context(source)
    ctx.repo_key = lambda value: tuple(value.split("/", 1))
    ctx.parse_int_list = lambda value: value
    app = Flask(__name__)
    preprocess_routes.register_preprocess_routes(app, ctx)
    response = app.test_client().post(
        "/api/preprocess/merge/start",
        json={
            "options": {
                "src_keys": ["local/source", "local/source"],
                "dimension_policy": "truncate",
            }
        },
    )
    assert response.status_code == 400
    assert "dimension_policy" in response.get_json()["error"]
    assert not ctx.jobs_registry


def test_standardize_reports_lifecycle_registration_after_precompute(tmp_path: Path, monkeypatch):
    src_root = tmp_path / "source"
    out_root = tmp_path / "standardized"
    (src_root / "meta").mkdir(parents=True)
    (src_root / "data").mkdir()
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v2.1", "total_episodes": 2, "features": {}})
    )
    dataset = SimpleNamespace(root=src_root, repo_id="local/source", total_episodes=2)

    def fake_precompute(**kwargs):
        kwargs["progress_callback"](
            {"status": "done", "current": 1, "total": 1, "message": "Precompute complete"}
        )

    def fake_standardize(*_args, **_kwargs):
        (out_root / "meta").mkdir(parents=True)
        (out_root / "data").mkdir()
        (out_root / "meta" / "info.json").write_text(
            json.dumps({"codebase_version": "v2.1", "total_episodes": 2, "features": {}})
        )
        return PreprocessResult(
            op="standardize",
            src_roots=[src_root],
            out_root=out_root,
            repo_id="local/standardized",
            total_episodes=2,
        )

    monkeypatch.setattr(preprocess_routes, "run_precompute", fake_precompute)
    monkeypatch.setattr(preprocess_routes, "run_standardize_dataset", fake_standardize)
    monkeypatch.setattr(preprocess_routes.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        preprocess_routes,
        "resolve_processing_profile",
        lambda *_args, **_kwargs: SimpleNamespace(legacy_data_version="DVT2"),
    )
    app = Flask(__name__)
    ctx = _route_context(src_root, legacy=True)
    ctx.parse_int_list = lambda value: value
    ctx.ensure_dataset_loaded = lambda _key: (dataset, src_root / "vis" / "static")
    ctx.dataset_episode_ids = lambda *_args: [0, 1]
    ctx.meta_only_dataset_cls = lambda repo_id, root: SimpleNamespace(repo_id=repo_id, root=root)
    ctx.register_dataset = lambda *_args, **_kwargs: ("local", "standardized")
    ctx.lifecycle_store = None
    ctx.append_operation_log = None
    preprocess_routes.register_preprocess_routes(app, ctx)

    response = app.test_client().post(
        "/api/preprocess/standardize/start",
        json={"dataset_key": "local/source", "options": {"out_root": str(out_root)}},
    )

    assert response.status_code == 200
    job = response.get_json()["job"]
    assert job["status"] == "done"
    messages = [entry["message"] for entry in job["logs"]]
    precompute_index = max(
        index for index, message in enumerate(messages) if "Precompute complete" in message
    )
    registration_index = next(
        index for index, message in enumerate(messages) if "registering output dataset" in message
    )
    assert precompute_index < registration_index
    assert "scanning meta/data/videos" in messages[registration_index]


def test_convert_v3_route_uses_indexed_root_without_legacy_dataset_load(tmp_path: Path, monkeypatch):
    src_root = tmp_path / "source"
    (src_root / "meta").mkdir(parents=True)
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v2.1", "total_episodes": 2})
    )
    out_root = tmp_path / "source_v3"
    calls = {}

    def fake_convert(src, **kwargs):
        calls.update(src=src, **kwargs)
        kwargs["progress_callback"](
            {"status": "done", "current": 2, "total": 2, "message": "Converted test dataset"}
        )
        return PreprocessResult(
            op="convert_v3",
            src_roots=[src],
            out_root=out_root,
            repo_id="local/source_v3",
            total_episodes=2,
            total_frames=5,
            summary={"action": "convert", "target_version": "v3.0"},
        )

    monkeypatch.setattr(preprocess_routes, "run_convert_v3", fake_convert)
    monkeypatch.setattr(preprocess_routes.threading, "Thread", _ImmediateThread)
    app = Flask(__name__)
    ctx = _route_context(src_root)
    preprocess_routes.register_preprocess_routes(app, ctx)

    response = app.test_client().post(
        "/api/preprocess/convert_v3/start",
        json={
            "dataset_key": "local/source",
            "options": {
                "out_root": str(out_root),
                "data_file_size_in_mb": 123,
                "video_file_size_in_mb": 234,
                "workers": 6,
                "image_video_mode": "rgb_lossless",
            },
        },
    )

    assert response.status_code == 200
    job = response.get_json()["job"]
    assert job["status"] == "done"
    assert job["output_root"] == str(out_root)
    assert calls["src"] == src_root
    assert calls["data_file_size_in_mb"] == 123
    assert calls["video_file_size_in_mb"] == 234
    assert calls["workers"] == 6
    assert calls["image_video_mode"] == "rgb_lossless"
    assert calls["overwrite"] is False
    assert calls["dry_run"] is False
    assert any("not auto-registered" in entry["message"] for entry in job["logs"])


def test_convert_v3_route_rejects_nonpositive_file_limit(tmp_path: Path):
    src_root = tmp_path / "source"
    (src_root / "meta").mkdir(parents=True)
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v2.1", "total_episodes": 2})
    )
    app = Flask(__name__)
    preprocess_routes.register_preprocess_routes(app, _route_context(src_root))

    response = app.test_client().post(
        "/api/preprocess/convert_v3/start",
        json={
            "dataset_key": "local/source",
            "options": {"data_file_size_in_mb": 0},
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "data/video file size limits must be positive"


def test_convert_v3_route_rejects_nonpositive_workers(tmp_path: Path):
    src_root = tmp_path / "source"
    (src_root / "meta").mkdir(parents=True)
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v2.1", "total_episodes": 2})
    )
    app = Flask(__name__)
    preprocess_routes.register_preprocess_routes(app, _route_context(src_root))

    response = app.test_client().post(
        "/api/preprocess/convert_v3/start",
        json={
            "dataset_key": "local/source",
            "options": {"workers": 0},
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "workers must be positive"


def test_convert_v3_route_rejects_unknown_image_video_mode(tmp_path: Path):
    src_root = tmp_path / "source"
    (src_root / "meta").mkdir(parents=True)
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v2.1", "total_episodes": 2})
    )
    app = Flask(__name__)
    preprocess_routes.register_preprocess_routes(app, _route_context(src_root))

    response = app.test_client().post(
        "/api/preprocess/convert_v3/start",
        json={
            "dataset_key": "local/source",
            "options": {"image_video_mode": "unknown_mode"},
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == ("image_video_mode must be one of: lerobot_official, rgb_lossless")


def test_convert_v3_route_requests_confirmation_before_overwrite(
    tmp_path: Path,
    monkeypatch,
):
    src_root = tmp_path / "source"
    (src_root / "meta").mkdir(parents=True)
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v2.1", "total_episodes": 2})
    )
    expected_out = tmp_path / "source_v3"
    expected_out.mkdir()
    app = Flask(__name__)
    preprocess_routes.register_preprocess_routes(app, _route_context(src_root))

    response = app.test_client().post(
        "/api/preprocess/convert_v3/start",
        json={"dataset_key": "local/source", "options": {}},
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["requires_overwrite_confirmation"] is True
    assert payload["output_root"] == str(expected_out)
    assert payload["error"] == f"Output dataset already exists: {expected_out}"

    calls = {}

    def fake_convert(src, **kwargs):
        calls.update(src=src, **kwargs)
        return PreprocessResult(
            op="convert_v3",
            src_roots=[src],
            out_root=expected_out,
            repo_id="local/source_v3",
            total_episodes=2,
            total_frames=5,
            summary={"action": "convert", "target_version": "v3.0"},
        )

    monkeypatch.setattr(preprocess_routes, "run_convert_v3", fake_convert)
    monkeypatch.setattr(preprocess_routes.threading, "Thread", _ImmediateThread)
    confirmed = app.test_client().post(
        "/api/preprocess/convert_v3/start",
        json={
            "dataset_key": "local/source",
            "options": {"overwrite": True},
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["job"]["status"] == "done"
    assert calls["out_root"] == expected_out
    assert calls["overwrite"] is True


def test_convert_v3_route_reports_existing_v3_without_output(tmp_path: Path, monkeypatch):
    src_root = tmp_path / "source_v3"
    (src_root / "meta").mkdir(parents=True)
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v3.0", "total_episodes": 2})
    )
    requested_out = tmp_path / "unused_copy"
    calls = {}

    def fake_convert(src, **kwargs):
        calls.update(src=src, **kwargs)
        return PreprocessResult(
            op="convert_v3",
            src_roots=[src],
            out_root=src,
            repo_id="local/source_v3",
            total_episodes=2,
            total_frames=5,
            summary={"action": "already_v3", "already_v3": True, "target_version": "v3.0"},
        )

    monkeypatch.setattr(preprocess_routes, "run_convert_v3", fake_convert)
    monkeypatch.setattr(preprocess_routes.threading, "Thread", _ImmediateThread)
    app = Flask(__name__)
    ctx = _route_context(src_root)
    app_ctx_entry = ctx.datasets_index.pop(("local", "source"))
    ctx.datasets_index[("local", "source_v3")] = app_ctx_entry
    preprocess_routes.register_preprocess_routes(app, ctx)

    response = app.test_client().post(
        "/api/preprocess/convert_v3/start",
        json={
            "dataset_key": "local/source_v3",
            "options": {
                "out_root": str(requested_out),
                "data_file_size_in_mb": 0,
                "video_file_size_in_mb": 0,
                "image_video_mode": "unknown_mode",
            },
        },
    )

    assert response.status_code == 200
    job = response.get_json()["job"]
    assert job["status"] == "done"
    assert job["output_root"] == str(src_root)
    assert "already v3.0" in job["message"]
    assert calls["out_root"] is None
    assert not requested_out.exists()
    assert not any("not auto-registered" in entry["message"] for entry in job["logs"])


def test_repair_v3_video_timestamps_route(tmp_path: Path, monkeypatch):
    src_root = tmp_path / "source_v3"
    (src_root / "meta").mkdir(parents=True)
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v3.0", "total_episodes": 2})
    )
    calls = {}

    def fake_repair(root, **kwargs):
        calls.update(root=root, **kwargs)
        return PreprocessResult(
            op="repair_v3_video_timestamps",
            src_roots=[root],
            out_root=root,
            repo_id="local/source_v3",
            total_episodes=2,
            total_frames=5,
            summary={"videos_reencoded": False},
        )

    monkeypatch.setattr(
        preprocess_routes,
        "repair_v3_video_timestamps",
        fake_repair,
    )
    monkeypatch.setattr(preprocess_routes.threading, "Thread", _ImmediateThread)
    app = Flask(__name__)
    ctx = _route_context(src_root)
    app_ctx_entry = ctx.datasets_index.pop(("local", "source"))
    ctx.datasets_index[("local", "source_v3")] = app_ctx_entry
    preprocess_routes.register_preprocess_routes(app, ctx)

    response = app.test_client().post(
        "/api/preprocess/repair_v3_video_timestamps/start",
        json={
            "dataset_key": "local/source_v3",
            "options": {"dry_run": False},
        },
    )

    assert response.status_code == 200
    job = response.get_json()["job"]
    assert job["status"] == "done"
    assert "without re-encoding" in job["message"]
    assert calls["root"] == src_root
    assert calls["dry_run"] is False


def test_delete_all_flagged_refreshes_viewer_episode_state_before_job_finishes(
    tmp_path: Path,
    monkeypatch,
):
    src_root = tmp_path / "source"
    static_dir = src_root / "vis" / "static"
    (src_root / "meta").mkdir(parents=True)
    static_dir.mkdir(parents=True)
    (src_root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v2.1", "total_episodes": 3, "features": {}})
    )
    (static_dir / "flagged_episodes.json").write_text(json.dumps({"flagged_episodes": [1]}))
    dataset = SimpleNamespace(
        root=src_root,
        repo_id="local/source",
        meta=SimpleNamespace(episodes={0: {}, 1: {}, 2: {}}),
        total_episodes=3,
    )
    events = []

    def fake_delete(dataset_obj, episode_ids, **_kwargs):
        events.append("delete")
        assert episode_ids == [1]
        dataset_obj.meta.episodes = {0: {}, 1: {}}
        dataset_obj.total_episodes = 2
        return {"deleted_episode_ids": [1], "new_total_episodes": 2, "next_episode": 1}

    def refresh_viewer(dataset_key, dataset_obj, refreshed_static_dir):
        events.append("refresh")
        assert dataset_key == ("local", "source")
        assert dataset_obj is dataset
        assert refreshed_static_dir == static_dir
        return [0, 1]

    monkeypatch.setattr(preprocess_routes, "delete_episodes_inplace", fake_delete)
    monkeypatch.setattr(preprocess_routes.threading, "Thread", _ImmediateThread)
    app = Flask(__name__)
    ctx = _route_context(src_root, legacy=True)
    audit = {}
    ctx.ensure_dataset_loaded = lambda _key: (dataset, static_dir)
    ctx.parse_int_list = lambda value: value
    ctx.clear_dataset_caches = None
    ctx.refresh_dataset_after_episode_delete = refresh_viewer
    ctx.append_operation_log = lambda *_args, **kwargs: audit.update(kwargs)
    original_finish_job = ctx.finish_job

    def finish_job(job, message, **updates):
        events.append("finish")
        original_finish_job(job, message, **updates)

    ctx.finish_job = finish_job
    preprocess_routes.register_preprocess_routes(app, ctx)

    missing_reason = app.test_client().post(
        "/api/preprocess/flag_fixes/start",
        json={
            "dataset_key": "local/source",
            "options": {"fix_kind": "delete_all_flagged"},
        },
    )
    assert missing_reason.status_code == 400
    assert "reason is required" in missing_reason.get_json()["error"]

    response = app.test_client().post(
        "/api/preprocess/flag_fixes/start",
        json={
            "dataset_key": "local/source",
            "options": {
                "fix_kind": "delete_all_flagged",
                "reason": "corrupted collection batch",
            },
        },
    )

    assert response.status_code == 200
    job = response.get_json()["job"]
    assert job["status"] == "done"
    assert job["viewer_url"] == "/local/source/episode_0"
    assert events == ["delete", "refresh", "finish"]
    assert audit["details"]["reason"] == "corrupted collection batch"


def test_convert_v3_homepage_wiring():
    template = (
        Path(__file__).parents[2]
        / "lerobot"
        / "data_platform"
        / "templates"
        / "visualize_dataset_homepage.html"
    ).read_text()

    assert 'value="convert_v3"' in template
    assert "/api/preprocess/convert_v3/start" in template
    assert "preprocess.v3_data_file_size_mb" in template
    assert "preprocess.v3_video_file_size_mb" in template
    assert "preprocess.v3_workers" in template
    assert "preprocess.v3_image_video_mode" in template
    assert "LeRobot official — AV1 / YUV420 / CRF 30" in template
    assert "RGB lossless — H.264 RGB / CRF 0" in template
    assert "auto: <src>_v3" in template
    assert "auto: <src>_v3_<timestamp>" not in template
    assert "requires_overwrite_confirmation" in template
    assert "payload.options.overwrite = true" in template
    assert 'value="repair_v3_video_timestamps"' in template
    assert "/api/preprocess/repair_v3_video_timestamps/start" in template
    assert "Videos are not re-encoded." in template
    assert "224×224" not in template
    assert "selectedJob().output_root" in template
    assert "Dataset is already v3.0; no conversion is needed." in template
    assert "selectedDatasetIsV3()" in template
    assert "v3.0 viewer support uses a read-only adapter in this environment." in template
    assert "Prepare v3.0 viewer cache first" in template

import json
from pathlib import Path

from lerobot.data_platform import agent as agent_module
from lerobot.data_platform.agent import DataPlatformAgent, discover_datasets
from lerobot.data_platform.precompute.preprocess.common import PreprocessResult


def _make_dataset(root: Path, *, episodes: int = 2) -> None:
    (root / "meta").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "meta" / "info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v2.1",
                "fps": 30,
                "total_episodes": episodes,
                "total_frames": episodes * 5,
                "features": {},
                "data_profile": {
                    "schema_version": 1,
                    "robot_profile": "h10w_dvt2",
                    "legacy_data_version": "DVT2",
                },
            }
        )
    )


class _FakeClient:
    server_url = "https://server-a.example"

    def __init__(self):
        self.events = []
        self.uploads = []

    def event(self, _state, job_id, message, payload=None):
        self.events.append((job_id, message, payload))

    def upload_artifact(self, _state, job_id, relative_path, path, *, derived=False):
        self.uploads.append((job_id, Path(relative_path), Path(path).read_bytes(), derived))


def _agent(
    tmp_path: Path,
    allowed_root: Path,
    writable_root: Path,
    *,
    allow_source_mutations: bool = False,
) -> DataPlatformAgent:
    state_path = tmp_path / "agent-state.json"
    state_path.write_text(
        json.dumps(
            {
                "node_id": "node-id",
                "node_token": "node-token",
                "name": "server-b",
                "server_url": "https://server-a.example",
            }
        )
    )
    return DataPlatformAgent(
        client=_FakeClient(),
        state_path=state_path,
        name="server-b",
        allowed_roots=[allowed_root],
        writable_roots=[writable_root],
        enrollment_token="",
        allow_source_mutations=allow_source_mutations,
    )


def test_agent_installer_uses_service_accessible_system_python_and_recovers_enrollment():
    installer = (
        Path(__file__).parents[2] / "deploy" / "data-platform" / "agent-bundle" / "install.sh"
    ).read_text()

    assert "PYTHON_BIN=/usr/bin/python3.10" in installer
    assert 'runuser -u "$SERVICE_USER" -- "$PYTHON_BIN"' in installer
    assert "UV_NO_MANAGED_PYTHON=1" in installer
    assert "DATA_PLATFORM_AGENT_ALLOW_SOURCE_MUTATIONS=0" in installer
    assert "if grep -q '^DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN='" in installer


def test_discover_datasets_stops_at_dataset_roots(tmp_path: Path):
    first = tmp_path / "team" / "first"
    second = tmp_path / "team" / "nested" / "second"
    _make_dataset(first)
    _make_dataset(second)
    (first / "data" / "ignored-child").mkdir()

    found = discover_datasets([tmp_path / "team"], node_name="server-b")

    assert {item["root"] for item in found} == {str(first.resolve()), str(second.resolve())}
    assert all(item["dataset_key"].startswith("node-server-b/") for item in found)


def test_agent_remote_preprocess_uses_sibling_output_and_reports_location(tmp_path: Path, monkeypatch):
    source = tmp_path / "datasets" / "source"
    _make_dataset(source)
    agent = _agent(tmp_path, tmp_path / "datasets", tmp_path / "datasets")

    def fake_preprocess(op, src_root, out_root, progress_callback, **options):
        assert op == "standardize"
        assert Path(src_root) == source.resolve()
        assert Path(out_root).parent == source.parent
        assert options == {"profile": "dvt2"}
        _make_dataset(Path(out_root), episodes=3)
        progress_callback({"message": "done", "current": 3, "total": 3})
        return PreprocessResult(
            op=op,
            src_roots=[Path(src_root)],
            out_root=Path(out_root),
            repo_id="local/standardized",
            total_episodes=3,
        )

    precompute_calls = []
    monkeypatch.setattr(agent_module, "run_preprocess_op", fake_preprocess)

    def fake_precompute(**kwargs):
        precompute_calls.append(kwargs)
        if Path(kwargs["root"]) != source.resolve():
            static = Path(kwargs["output_dir"]) / "static"
            static.mkdir(parents=True, exist_ok=True)
            (static / "viewer_manifest.json").write_text(json.dumps({"episodes": [{"episode_index": 0}]}))
        return {}

    monkeypatch.setattr(agent_module, "run_precompute", fake_precompute)
    result = agent.execute_job(
        {
            "job_id": "job-1",
            "operation": "preprocess.standardize",
            "options": {"profile": "dvt2"},
            "location": {"dataset_key": "node-server-b/source", "root": str(source)},
        }
    )

    output = Path(result["dataset_location"]["root"])
    assert output != source
    assert output.parent == source.parent
    assert (source / "meta" / "info.json").is_file()
    assert result["dataset_location"]["metadata"]["total_episodes"] == 3
    assert result["dataset_location"]["metadata"]["stage"] == "standard"
    assert result["viewer_cache"]["uploaded_files"] == 1
    assert [Path(call["root"]) for call in precompute_calls] == [source.resolve(), output]
    assert agent.client.uploads[-1][1] == Path("viewer_manifest.json")
    assert agent.client.uploads[-1][3] is True
    assert agent.client.events[-1][2]["current"] == 100


def test_agent_viewer_prepare_uploads_manifest_last(tmp_path: Path, monkeypatch):
    source = tmp_path / "datasets" / "source"
    _make_dataset(source)
    agent = _agent(tmp_path, tmp_path / "datasets", tmp_path)
    output_dir = tmp_path / "viewer-cache"

    def fake_precompute(**kwargs):
        assert kwargs["visualize_only"] is True
        assert kwargs["data_version"] == "DVT2"
        assert kwargs["prepare_videos"] is False
        assert kwargs["prepare_csv"] is True
        assert kwargs["overwrite"] is True
        assert kwargs["overwrite_csv"] is True
        static = Path(kwargs["output_dir"]) / "static"
        (static / "csv").mkdir(parents=True)
        (static / "csv" / "episode_000000_ds1.csv").write_text("timestamp\n0\n")
        (static / "viewer_manifest.json").write_text(json.dumps({"episodes": [{"episode_index": 0}]}))
        return {"prepared": True}

    monkeypatch.setattr(agent_module, "run_precompute", fake_precompute)
    result = agent.execute_job(
        {
            "job_id": "job-viewer",
            "operation": "viewer.prepare",
            "options": {
                "data_version": "DVT2",
                "overwrite": True,
                "overwrite_csv": True,
                "prepare_csv": True,
                "prepare_videos": False,
            },
            "location": {
                "dataset_key": "node-server-b/source",
                "root": str(source),
                "output_dir": str(output_dir),
            },
        }
    )

    assert result["uploaded_files"] == 2
    assert agent.client.uploads[-1][1] == Path("viewer_manifest.json")


def test_agent_remote_preprocess_accepts_safe_custom_output_and_overwrite(tmp_path: Path, monkeypatch):
    source = tmp_path / "datasets" / "source"
    custom_output = tmp_path / "datasets" / "custom-standard"
    _make_dataset(source)
    agent = _agent(tmp_path, tmp_path / "datasets", tmp_path / "datasets")

    def fake_preprocess(op, src_root, out_root, progress_callback, **options):
        assert op == "standardize"
        assert Path(src_root) == source.resolve()
        assert Path(out_root) == custom_output.resolve()
        assert options["overwrite"] is True
        _make_dataset(Path(out_root), episodes=3)
        return PreprocessResult(
            op=op,
            src_roots=[Path(src_root)],
            out_root=Path(out_root),
            repo_id="local/custom-standard",
            total_episodes=3,
        )

    monkeypatch.setattr(agent_module, "run_preprocess_op", fake_preprocess)

    def fake_precompute(**kwargs):
        if Path(kwargs["root"]) == custom_output.resolve():
            static = Path(kwargs["output_dir"]) / "static"
            static.mkdir(parents=True, exist_ok=True)
            (static / "viewer_manifest.json").write_text(json.dumps({"episodes": [{"episode_index": 0}]}))
        return {}

    monkeypatch.setattr(agent_module, "run_precompute", fake_precompute)
    monkeypatch.setattr(
        agent_module,
        "_AgentDataset",
        lambda repo_id, root: type(
            "Dataset",
            (),
            {"repo_id": repo_id, "root": root, "total_frames": 12},
        )(),
    )
    deleted = []

    def fake_delete(_dataset, episode_ids, **_kwargs):
        deleted.extend(episode_ids)
        return {"deleted_episode_ids": episode_ids, "new_total_episodes": 1}

    monkeypatch.setattr(agent_module, "delete_episodes_inplace", fake_delete)
    result = agent.execute_job(
        {
            "job_id": "custom-output",
            "operation": "preprocess.standardize",
            "options": {
                "delete_episodes": "1,2",
                "out_root": str(custom_output),
                "overwrite_output": True,
            },
            "location": {"dataset_key": "node-server-b/source", "root": str(source)},
        }
    )

    assert Path(result["dataset_location"]["root"]) == custom_output.resolve()
    assert deleted == [1, 2]
    assert result["preprocess"]["summary"]["episodes_after_delete"] == 1


def test_agent_remote_preprocess_rejects_unsafe_custom_output(tmp_path: Path):
    source = tmp_path / "datasets" / "source"
    _make_dataset(source)
    agent = _agent(tmp_path, tmp_path / "datasets", tmp_path / "datasets")

    for output in (source / "child", tmp_path / "outside"):
        try:
            agent.execute_job(
                {
                    "job_id": f"unsafe-{output.name}",
                    "operation": "preprocess.standardize",
                    "options": {"out_root": str(output), "overwrite_output": True},
                    "location": {"dataset_key": "node-server-b/source", "root": str(source)},
                }
            )
        except (PermissionError, ValueError):
            pass
        else:
            raise AssertionError(f"unsafe output was accepted: {output}")


def test_agent_rejects_source_outside_allowed_roots(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    _make_dataset(outside)
    agent = _agent(tmp_path, allowed, tmp_path)

    try:
        agent.execute_job(
            {
                "job_id": "escape",
                "operation": "preprocess.standardize",
                "options": {},
                "location": {"dataset_key": "outside/data", "root": str(outside)},
            }
        )
    except PermissionError as exc:
        assert "outside configured roots" in str(exc)
    else:
        raise AssertionError("outside source root was accepted")


def test_agent_source_mutation_is_off_by_default(tmp_path: Path):
    source = tmp_path / "datasets" / "source"
    _make_dataset(source)
    agent = _agent(tmp_path, tmp_path / "datasets", tmp_path / "datasets")

    try:
        agent.execute_job(
            {
                "job_id": "mutation-disabled",
                "operation": "mutation.value_edit",
                "options": {
                    "edits": [{"field": "action", "dimension": 0, "value": 0}],
                    "reason": "test",
                },
                "location": {"dataset_key": "node-server-b/source", "root": str(source)},
            }
        )
    except PermissionError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("source mutation ran without the Agent switch")


def test_agent_source_mutation_keeps_backup_and_restores_on_failure(tmp_path: Path, monkeypatch):
    source = tmp_path / "datasets" / "source"
    _make_dataset(source)
    parquet = source / "data" / "episode_000000.parquet"
    parquet.write_text("original")
    agent = _agent(
        tmp_path,
        tmp_path / "datasets",
        tmp_path / "datasets",
        allow_source_mutations=True,
    )

    def failing_preprocess(_op, src_root, **_kwargs):
        temporary = Path(src_root) / "data" / ".replacement.tmp"
        temporary.write_text("changed")
        temporary.replace(parquet)
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(agent_module, "run_preprocess_op", failing_preprocess)
    try:
        agent.execute_job(
            {
                "job_id": "mutation-failure",
                "operation": "mutation.value_edit",
                "options": {
                    "edits": [{"field": "action", "dimension": 0, "value": 0}],
                    "reason": "test rollback",
                    "requested_by": {"username": "platform-admin"},
                },
                "location": {"dataset_key": "node-server-b/source", "root": str(source)},
            }
        )
    except RuntimeError as exc:
        assert "simulated write failure" in str(exc)
    else:
        raise AssertionError("simulated mutation failure was ignored")

    assert parquet.read_text() == "original"
    backups = list((tmp_path / "datasets" / ".data-platform-backups" / "source").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "dataset" / "data" / parquet.name).read_text() == "original"
    audit_log = tmp_path / "datasets" / "vis" / "local_vis_source" / "static" / "operation_log.jsonl"
    events = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert [event["status"] for event in events[-2:]] == ["started", "failed"]
    assert events[-1]["details"]["restored"] is True


def test_agent_source_mutation_reuses_completed_job_marker(tmp_path: Path, monkeypatch):
    source = tmp_path / "datasets" / "source"
    _make_dataset(source)
    parquet = source / "data" / "episode_000000.parquet"
    parquet.write_text("original")
    agent = _agent(
        tmp_path,
        tmp_path / "datasets",
        tmp_path / "datasets",
        allow_source_mutations=True,
    )
    calls = 0

    def successful_preprocess(op, src_root, **_kwargs):
        nonlocal calls
        calls += 1
        temporary = Path(src_root) / "data" / ".replacement.tmp"
        temporary.write_text("changed")
        temporary.replace(parquet)
        return PreprocessResult(
            op=op,
            src_roots=[Path(src_root)],
            out_root=Path(src_root),
            repo_id="node-server-b/source",
            total_episodes=2,
        )

    monkeypatch.setattr(agent_module, "run_preprocess_op", successful_preprocess)
    job = {
        "job_id": "mutation-success",
        "operation": "mutation.value_edit",
        "options": {
            "edits": [{"field": "action", "dimension": 0, "value": 0}],
            "reason": "test idempotency",
            "requested_by": {"username": "platform-admin"},
        },
        "location": {"dataset_key": "node-server-b/source", "root": str(source)},
    }

    first = agent.execute_job(job)
    second = agent.execute_job(job)

    assert calls == 1
    assert second == first
    assert parquet.read_text() == "changed"
    assert Path(first["backup_root"]).joinpath("dataset", "data", parquet.name).read_text() == "original"
    assert agent._completed_mutation_path(job["job_id"]).is_file()


def test_agent_does_not_overwrite_success_with_error_when_completion_response_fails(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "datasets" / "source"
    _make_dataset(source)
    agent = _agent(tmp_path, tmp_path / "datasets", tmp_path)

    class RunClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.completions = []

        def heartbeat(self, _state, _capabilities):
            return None

        def claim(self, _state, *, lease_seconds):
            assert lease_seconds == agent.lease_seconds
            return {
                "job_id": "job-complete",
                "operation": "viewer.prepare",
                "location": {"dataset_key": "node-server-b/source", "root": str(source)},
            }

        def complete(self, _state, _job_id, *, status, result=None, error=None):
            self.completions.append((status, result, error))
            if status == "done":
                raise RuntimeError("response was lost")

    client = RunClient()
    agent.client = client
    monkeypatch.setattr(agent, "execute_job", lambda _job: {"prepared": True})

    assert agent.run_once(sync=False) is True
    assert [status for status, _result, _error in client.completions] == ["done"]

"""Operator discovery is lazy and legacy dispatch retains computation behavior."""

import json
import subprocess
import sys

import pytest

from lerobot.data_platform.operators import get_operator, list_operators
from lerobot.data_platform.operators.schema import validate_parameters
from lerobot.data_platform.processing.planner import PipelineSpec


def test_discovery_does_not_import_models():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, sys
from lerobot.data_platform.operators import list_operators
descriptions = [spec.to_dict() for spec in list_operators()]
assert len(descriptions) >= 43
json.dumps(descriptions, allow_nan=False)
assert not {'torch', 'transformers', 'flask', 'numpy', 'pyarrow'} & set(sys.modules)
""",
        ],
        check=True,
    )


def test_registry_versions_and_parameter_defaults():
    assert len({spec.operator_id for spec in list_operators()}) == len(list_operators())
    with pytest.raises(ValueError, match="version unavailable"):
        get_operator("preprocess.split", "2.0.0")
    spec = get_operator("preprocess.split")
    params = validate_parameters(spec, {})
    assert params["episode_range"] is None
    with pytest.raises(ValueError, match="Unsupported parameters"):
        validate_parameters(spec, {"src_root": "/unauthorized"})
    with pytest.raises(ValueError, match="Invalid type"):
        validate_parameters(get_operator("preprocess.smooth_action"), {"window": True})


@pytest.mark.parametrize("operator", ["mutation.delete_episodes", "preprocess.clear_flags", "inspect"])
def test_privileged_or_non_worker_operators_cannot_enter_pipeline(operator):
    with pytest.raises(ValueError, match="dedicated authorized"):
        PipelineSpec.from_dict({"name": "unsafe", "steps": [{"step_id": "a", "operator_id": operator}]})


def test_plan_rejects_forward_references_and_artifact_as_dataset():
    with pytest.raises(ValueError, match="Dataset input"):
        PipelineSpec.from_dict(
            {
                "name": "invalid",
                "steps": [
                    {"step_id": "a", "operator_id": "curation.quality"},
                    {"step_id": "b", "operator_id": "preprocess.split", "input": "a"},
                ],
            }
        )


def test_plan_round_trip_pins_versions():
    spec = PipelineSpec.from_dict(
        {
            "name": "split",
            "steps": [
                {"step_id": "a", "operator_id": "preprocess.split"},
                {"step_id": "b", "operator_id": "preprocess.split", "input": "a"},
            ],
        }
    )
    restored = PipelineSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert restored.digest == spec.digest
    assert all(step.version == "1.0.0" for step in restored.steps)


def sample_operator(context, parameters):
    from lerobot.data_platform.operators import OperatorResult

    context.emit({"message": "Counting metadata"})
    path = context.work_dir / "count.json"
    path.write_text(json.dumps({"count": parameters["count"]}))
    return OperatorResult(summary={"count": parameters["count"]}, artifacts=({"path": "count.json"},))


def test_registered_operator_executes_and_publishes_without_agent_dispatch_changes(tmp_path, monkeypatch):
    import shutil
    from types import SimpleNamespace

    from flask import Flask

    from lerobot.data_platform.curation import validate_bundle
    from lerobot.data_platform.operators import OperatorSpec, register_operator, registry
    from lerobot.data_platform.routes.processing import register_processing_routes
    from tests.datasets.test_data_platform_agent import _agent
    from tests.datasets.test_quality_eef import make_eef

    source = make_eef(tmp_path / "source")
    spec = OperatorSpec(
        "example.count",
        "1.0.0",
        "Count metadata",
        "example",
        "read",
        entry_point=__name__ + ":sample_operator",
        parameter_schema_override={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))
    register_operator(spec)
    agent = _agent(tmp_path, source, tmp_path)
    ledger = SimpleNamespace(root=tmp_path / "lifecycle")
    attempt = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    target = {"location_id": "location", "dataset_key": "node/source", "dataset_version_id": None}
    job = {
        "job_id": "job",
        "operation": spec.operator_id,
        "execution": {"attempt_id": attempt},
        "location": {"root": str(source), "dataset_key": "node/source"},
        "options": {
            "parameters": {"count": 4},
            "target": target,
            "operator_snapshot": {
                "operator_id": spec.operator_id,
                "version": spec.version,
            },
        },
    }
    staged = ledger.root / "curation" / ".jobs" / "job" / attempt

    def upload(_state, _job, name, path, **kwargs):
        assert kwargs["curation"]
        destination = staged / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)

    agent.client.upload_artifact = upload
    result = agent.execute_job(job)
    assert result["summary"] == {"count": 4}
    validate_bundle(staged, spec.operator_id, target)
    app = Flask(__name__)
    register_processing_routes(app, SimpleNamespace(control_plane_store=object(), lifecycle_store=ledger))
    with app.test_request_context(headers={"X-Job-Attempt": attempt}):
        published = app.extensions["data_platform_complete_operator"](job, result)
    assert published["artifacts"][0]["url"] == "/api/operator-results/job/count.json"
    assert (ledger.root / "operator-results/job/static/count.json").is_file()


def test_registry_retains_pinned_versions(monkeypatch):
    from lerobot.data_platform.operators import OperatorSpec, register_operator, registry

    monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))
    monkeypatch.setattr(registry, "_VERSIONS", dict(registry._VERSIONS))
    old = register_operator(OperatorSpec("test.versioned", "1.0.0", "first", "custom", "read"))
    current = register_operator(OperatorSpec("test.versioned", "2.0.0", "second", "custom", "read"))
    assert get_operator("test.versioned") == current
    assert get_operator("test.versioned", "1.0.0") == old
    assert registry.worker_manifest()["operator_version_history"]["test.versioned"] == ["1.0.0", "2.0.0"]


def test_every_background_entry_has_an_explicit_registered_adapter():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[2] / "lerobot/data_platform"
    for path in [root / "viewer.py", *(root / "routes").glob("*.py"), *(root / "processing").glob("*.py")]:
        for call in ast.walk(ast.parse(path.read_text())):
            if (
                not isinstance(call, ast.Call)
                or not isinstance(call.func, ast.Name)
                or call.func.id != "launch_background"
            ):
                continue
            keywords = {item.arg: item.value for item in call.keywords}
            assert {"command", "compatibility_operator", "continuation"} & keywords.keys(), (
                f"Unmapped entry: {path.name}:{call.lineno}"
            )
            if "compatibility_operator" in keywords:
                spec = get_operator(ast.literal_eval(keywords["compatibility_operator"]))
                assert not spec.remote

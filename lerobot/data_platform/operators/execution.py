"""Common execution and artifact preparation for independently registered operators."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from lerobot.data_platform.operators import ExecutionContext, OperatorResult, get_operator
from lerobot.data_platform.operators.registry import execute_operator
from lerobot.data_platform.operators.schema import validate_parameters


def execute_registered(agent, job, source: Path, location: dict) -> dict:
    from lerobot.data_platform.agent import (
        _dataset_payload,
        _json_value,
        _require_safe_output,
        _require_within,
    )
    from lerobot.data_platform.curation import bundle_manifest
    from lerobot.data_platform.execution import atomic_json
    from lerobot.data_platform.execution_pools import check_stop
    from lerobot.data_platform.execution_resources import ExecutionResources

    spec = get_operator(job["operation"], job.get("_operator_version"))
    options = job["options"]
    parameters = validate_parameters(spec, options.get("parameters", {}))
    bundle = (
        agent.state_path.parent / "operator-work" / job["job_id"] / job["execution"]["attempt_id"] / "bundle"
    )
    static = bundle / "static"
    static.mkdir(parents=True, exist_ok=True)
    output = None
    if spec.output_type == "dataset":
        output = _require_safe_output(Path(options["out_root"]), source, agent.writable_roots)

    def progress(payload):
        check_stop()
        agent.client.event(agent.state, job["job_id"], str(payload.get("message") or "Computing"), payload)

    context = ExecutionContext(
        source_root=source,
        output_root=output,
        dataset_key=location["dataset_key"],
        work_dir=static,
        progress=progress,
        check_cancelled=check_stop,
        resources=ExecutionResources.resolve(),
        artifacts={name: Path(options["input_root"]) / name for name in options.get("input_files", {})},
    )
    value = execute_operator(spec.operator_id, context, parameters, version=spec.version)
    if not isinstance(value, OperatorResult):
        value = OperatorResult.from_legacy(value)
    result = _json_value(asdict(value))
    if spec.output_type == "dataset":
        if (
            len(value.derived_datasets) != 1
            or Path(value.derived_datasets[0]["root"]).resolve() != output.resolve()
        ):
            raise ValueError("Operator must return its one declared dataset output")
        from lerobot.data_platform.lifecycle import validate_materialized_dataset
        from lerobot.data_platform.precompute.dataset_io import load_episode_records

        validate_materialized_dataset(output, expected_episode_count=len(load_episode_records(output)))
        result["dataset_location"] = _dataset_payload(output, node_name=agent.name)
        from lerobot.data_platform.precompute.preprocess.common import PreprocessResult

        info = result["dataset_location"]["metadata"]
        result["preprocess"] = _json_value(
            PreprocessResult(
                spec.operator_id,
                [source],
                output,
                result["dataset_location"]["dataset_key"],
                int(info["total_episodes"]),
                int(info["total_frames"]),
                False,
                summary=value.summary,
                episode_lineage=list(value.episode_lineage),
            )
        )
    for artifact in value.artifacts:
        relative = Path(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Operator artifact paths must be relative to its working directory")
        path = static / relative
        _require_within(path, [static], "operator artifact")
        if not path.is_file() or path.is_symlink():
            raise ValueError("Operator artifact is missing or a symlink")
    result["operator_id"], result["operator_version"] = spec.operator_id, spec.version
    if result.get("dataset_location") and options.get("version_inputs"):
        from lerobot.data_platform.operators.dataset_versions import publish_preprocess_version

        return publish_preprocess_version(agent, job, source, result)
    atomic_json(bundle / "result.json", result)
    bundle_manifest(bundle, operation=spec.operator_id, target=options["target"])
    paths = sorted(
        (path for path in bundle.rglob("*") if path.is_file()),
        key=lambda path: path.name == "curation-manifest.json",
    )
    for path in paths:
        check_stop()
        agent.client.upload_artifact(
            agent.state, job["job_id"], path.relative_to(bundle).as_posix(), path, curation=True
        )
    return result

"""Publish validated preprocessing identities using the existing immutable lifecycle protocol."""

import json
from pathlib import Path

from lerobot.data_platform.curation import bundle_manifest, snapshot_from_version
from lerobot.data_platform.curation_execution import hydrate_builder
from lerobot.data_platform.execution import atomic_json
from lerobot.data_platform.lifecycle import LifecycleStore, dataset_snapshot, validate_materialized_dataset
from lerobot.data_platform.precompute.preprocess.common import PreprocessResult
from lerobot.data_platform.processing.lineage import register_preprocess_result
from lerobot.data_platform.task_catalog import content_digest


def publish_preprocess_version(agent, job, source, result):
    inputs = job["options"].get("version_inputs")
    if not inputs or not result.get("dataset_location"):
        return result
    attempt = job["execution"]["attempt_id"]
    if job["operation"].startswith("preprocess."):
        output = Path(result["dataset_location"]["root"])
        validate_materialized_dataset(
            output, expected_episode_count=int(result["dataset_location"]["metadata"]["total_episodes"])
        )
        atomic_json(
            agent.state_path.parent / "operator-work" / job["job_id"] / "completed-preprocess.json",
            {
                "command": content_digest([job["operation"], job.get("_operator_version"), job["options"]]),
                "output_fingerprint": dataset_snapshot(output)[0],
                "result": result,
            },
        )
    working = agent.state_path.parent / "operator-work" / job["job_id"] / attempt
    bundle = working / "bundle"
    ledger = LifecycleStore(working / "lifecycle")
    roots = [
        source,
        *[
            Path(item["root"])
            for item in job["options"].get("_source_locations", [])
            if Path(item["root"]) != source
        ],
    ]
    by_root = {str(root.resolve()): root for root in roots}
    parents = []
    for snapshot in inputs:
        root = by_root.get(str(Path(snapshot["version"]["root"]).resolve()))
        if root is None:
            raise ValueError("Version snapshot is outside the registered computation inputs")
        parents.append(hydrate_builder(ledger, snapshot, root, job["options"]))
    payload = dict(result["preprocess"])
    payload["src_roots"] = [Path(item) for item in payload["src_roots"]]
    payload["out_root"] = Path(payload["out_root"])
    computed = PreprocessResult(**payload)
    version = register_preprocess_result(ledger, computed, parents, job["operation"])
    snapshot = snapshot_from_version(ledger, version, computed.out_root, job["options"].get("task_config"))
    bundle.mkdir(parents=True, exist_ok=True)
    atomic_json(bundle / "snapshot.json", snapshot.to_dict())
    result = {**result, "dataset_version_id": version.version_id}
    atomic_json(bundle / "result.json", result)
    bundle_manifest(bundle, operation=job["operation"], target=job["options"]["target"])
    for path in sorted(bundle.rglob("*"), key=lambda item: item.name == "curation-manifest.json"):
        if path.is_file():
            agent.client.upload_artifact(
                agent.state, job["job_id"], path.relative_to(bundle).as_posix(), path, curation=True
            )
    return result


def resume_preprocess_result(agent, job):
    """Reuse validated computation after metadata/publication failure, with all inputs rechecked."""
    path = agent.state_path.parent / "operator-work" / job["job_id"] / "completed-preprocess.json"
    if not path.is_file():
        return None
    from lerobot.data_platform.agent import _require_safe_output, _require_within

    value = json.loads(path.read_text())
    if value["command"] != content_digest([job["operation"], job.get("_operator_version"), job["options"]]):
        raise ValueError("Completed computation does not match the pinned command")
    for snapshot in job["options"].get("version_inputs", []):
        source = _require_within(Path(snapshot["version"]["root"]), agent.allowed_roots, "versioned input")
        if dataset_snapshot(source)[0] != snapshot["version"]["fingerprint"]:
            raise ValueError("Completed computation input changed")
    output = Path(value["result"]["dataset_location"]["root"])
    if str(output) != job["options"]["out_root"]:
        raise ValueError("Completed computation output does not match the plan")
    output = _require_safe_output(output, Path(job["location"]["root"]), agent.writable_roots)
    if dataset_snapshot(output)[0] != value["output_fingerprint"]:
        raise ValueError("Completed computation output changed")
    return value["result"]

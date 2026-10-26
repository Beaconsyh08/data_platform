"""Local compatibility adapters execute domain calls through the registered runtime."""

from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from lerobot.data_platform.operators import get_operator


def execute_local_operator(agent, job: dict) -> dict:
    from lerobot.data_platform.agent import _dataset_payload, _json_value, _require_within
    from lerobot.data_platform.dataset_metadata import MetaOnlyDataset

    if not agent.name.startswith("local-"):
        raise PermissionError("Local computations require the registered local executor")
    options = job["options"]
    command = options["local_compute"]
    if command.get("operator_id") != job["operation"]:
        raise ValueError("Local command operator does not match the queued job")
    spec = get_operator(job["operation"], (options.get("operator_snapshot") or {}).get("version"))
    if spec.effect == "source":
        if not agent.allow_source_mutations:
            raise PermissionError("Source mutation is disabled on the execution node")
        previous = agent._load_completed_mutation(job["job_id"])
        if previous is not None:
            return previous
    entry = spec.local_entry_point or spec.entry_point
    if not entry:
        raise ValueError("This local computation has no domain execution adapter")
    secrets_path = agent.state_path.parent / "request-secrets" / f"{job['job_id']}.json"
    secrets = json.loads(secrets_path.read_text()) if secrets_path.exists() else {}

    def restore(value):
        if isinstance(value, dict):
            if set(value) == {"__secret_ref__"}:
                return secrets[value["__secret_ref__"]]
            return {key: restore(item) for key, item in value.items()}
        if isinstance(value, list):
            return [restore(item) for item in value]
        return value

    parameters = restore(command["parameters"])
    # The persisted command describes the requested destination. The supervisor's
    # execution copy owns staging and worker limits; never write around publication.
    if spec.operator_id == "viewer.prepare":
        parameters["output_dir"] = job["location"]["output_dir"]
    if spec.effect == "dataset" and options.get("out_root"):
        parameters["out_root"] = options["out_root"]
        for key in ("overwrite_output", "overwrite"):
            if key in parameters:
                parameters[key] = False
    for key in ("workers", "prepare_workers"):
        if key in parameters and key in options:
            parameters[key] = options[key]
    source = _require_within(Path(job["location"]["root"]), agent.allowed_roots, "dataset root")
    for key in ("root", "src_root", "base_root", "dataset_root"):
        if key in parameters:
            parameters[key] = _require_within(Path(parameters[key]), agent.allowed_roots, "dataset root")
    for key in ("src_roots", "subtract_roots"):
        if key in parameters:
            parameters[key] = [
                _require_within(Path(item), agent.allowed_roots, "source root") for item in parameters[key]
            ]
    for key in (
        "out_root",
        "static_dir",
        "static_folder",
        "output_dir",
        "src_static_dir",
        "out_static_dir",
        "labeling_dir",
    ):
        if parameters.get(key) is not None:
            parameters[key] = _require_within(Path(parameters[key]), agent.writable_roots, "output directory")
    if parameters.get("src_static_dirs"):
        parameters["src_static_dirs"] = [
            _require_within(Path(item), agent.writable_roots, "source cache")
            for item in parameters["src_static_dirs"]
        ]
    if spec.effect == "source":
        _require_within(source, agent.writable_roots, "source mutation root")
    module, name = entry.split(":")
    function = getattr(importlib.import_module(module), name)
    signature = inspect.signature(function)
    if "meta" in signature.parameters:
        parameters["meta"] = MetaOnlyDataset(job["location"]["dataset_key"], root=source).meta
    if "dataset" in signature.parameters:
        parameters["dataset"] = MetaOnlyDataset(job["location"]["dataset_key"], root=source)
    if "ckpt_path" in parameters and parameters["ckpt_path"]:
        parameters["ckpt_path"] = Path(parameters["ckpt_path"])
    if "progress_callback" in signature.parameters:
        parameters["progress_callback"] = lambda payload: agent.client.event(
            agent.state, job["job_id"], str(payload.get("message") or "Computing"), payload
        )
    if "log" in signature.parameters:
        parameters["log"] = lambda message: agent.client.event(agent.state, job["job_id"], message)
    fingerprints = {}
    if spec.effect == "dataset" and spec.family == "preprocess":
        from lerobot.data_platform.lifecycle import dataset_snapshot

        roots = parameters.get("src_roots") or [source, *(parameters.get("subtract_roots") or [])]
        fingerprints = {str(Path(root).resolve()): dataset_snapshot(Path(root))[0] for root in roots}
    if command.get("composite") and spec.operator_id == "preprocess.standardize":
        value = agent._run_preprocess({**job, "options": parameters}, source, job["location"])
        value = value["preprocess"]
    else:
        value = function(**parameters)
    result = _json_value(asdict(value) if is_dataclass(value) else value or {})
    if spec.operator_id == "viewer.prepare":
        agent._upload_viewer_artifacts(job, Path(parameters["output_dir"]) / "static")
    payload = {
        "computation": result,
        "local_job": {
            **options.get("local_job", {}),
            "status": "done",
            "progress": 100,
            "message": "Computation complete",
        },
    }
    payload["local_job"]["result_summary"] = result.get("summary", result) if isinstance(result, dict) else {}
    if fingerprints and not result.get("dry_run"):
        payload["local_preprocess"] = {"result": result, "fingerprints": fingerprints}
    if isinstance(result, dict) and (result.get("summary") or {}).get("native_images"):
        from lerobot.data_platform.cli import get_default_output_dir, run_precompute

        run_precompute(
            root=Path(result["out_root"]),
            repo_id=result["repo_id"],
            output_dir=get_default_output_dir(Path(result["out_root"])),
            visualize_only=True,
            show_progress=False,
        )
    registrations = []
    output = result.get("out_root") if isinstance(result, dict) else None
    if (
        spec.effect == "dataset"
        and output
        and not result.get("dry_run")
        and (Path(output) / "meta/info.json").is_file()
    ):
        from lerobot.data_platform.cli import get_default_output_dir

        directory = get_default_output_dir(Path(output))
        registrations.append(
            {
                "root": output,
                "repo_id": result.get("repo_id") or f"local/{Path(output).name}",
                "output_dir": str(directory),
            }
        )
        payload["dataset_location"] = _dataset_payload(Path(output), node_name=agent.name)
        payload["local_job"].update(
            output_root=output, viewer_url=f"/{registrations[-1]['repo_id']}/episode_0"
        )
    if spec.effect == "source" or spec.operator_id == "viewer.prepare":
        registrations.append(
            {
                "root": str(source),
                "repo_id": job["location"]["dataset_key"],
                "output_dir": job["location"]["output_dir"],
            }
        )
        if spec.operator_id == "viewer.prepare" and result.get("episodes"):
            payload["local_job"]["viewer_url"] = (
                f"/{job['location']['dataset_key']}/episode_{result['episodes'][0]}"
            )
    payload["local_registrations"] = registrations
    if spec.effect == "source":
        agent._write_completed_mutation(job["job_id"], payload)
    secrets_path.unlink(missing_ok=True)
    return payload

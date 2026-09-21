"""Shared local/Agent Curation computations using the existing dataset operators."""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from pathlib import Path

from lerobot.data_platform.curation import (
    CURATION_PROTOCOL,
    OPERATIONS,
    CurationSnapshot,
    artifact_path,
    bundle_manifest,
    scan_snapshot,
    snapshot_from_version,
)
from lerobot.data_platform.execution import atomic_json
from lerobot.data_platform.lifecycle import (
    DatasetVersion,
    LifecycleStore,
    dataset_snapshot,
    materialize_manifest,
)
from lerobot.data_platform.precompute.data_profile import require_dataset_operation
from lerobot.data_platform.task_catalog import TaskConfigSnapshot, content_digest

PARAMETERS = {
    "snapshot": set(),
    "validate_source": set(),
    "stage": {"episodes", "prepare_workers", "fallback_stage_count", "data_version"},
    "quality": {"episodes", "data_version", "workers", "overwrite", "clear_manual_flags"},
    "labeling": {
        "episodes",
        "backend",
        "model_id",
        "endpoint",
        "qwen_model",
        "min_pixels",
        "max_pixels",
        "box_threshold",
        "text_threshold",
        "save_vis",
        "workers",
        "devices",
        "output_variant",
        "run_mode",
        "trial",
        "trial_per_type",
        "trial_seed",
    },
    "tagging": {
        "episodes",
        "selected_tags",
        "vlm_backend",
        "vlm_model",
        "vlm_endpoint",
        "output_variant",
        "workers",
        "overwrite",
        "trial",
        "trial_per_type",
        "trial_seed",
    },
    "embedding": {"episodes", "ckpt_path", "layer_hook", "openpi_config", "refit", "workers", "devices"},
    "project": {"method", "seed", "n_neighbors", "min_dist", "metric"},
    "compare_summary": set(),
    "construction_preview": {"uncertainty_threshold", "allow_pick_to_give"},
    "construction": {
        "uncertainty_threshold",
        "per_scenario_counts",
        "include_positives",
        "oversample_factor",
        "allow_pick_to_give",
    },
    "materialize": {"workers"},
}


def validate_parameters(operation: str, parameters: dict) -> dict:
    if operation not in OPERATIONS or not isinstance(parameters, dict):
        raise ValueError("Unsupported Curation operation")
    result = dict(parameters)
    allowed = PARAMETERS[operation.split(".")[1]]
    if set(result) - allowed:
        raise ValueError(f"Unsupported parameters: {', '.join(sorted(set(result) - allowed))}")
    if "episodes" in result:
        episodes = result["episodes"]
        if episodes is not None and (
            not isinstance(episodes, list) or any(type(index) is not int or index < 0 for index in episodes)
        ):
            raise ValueError("episodes must be a list of non-negative integers")
    for key in ("workers", "prepare_workers", "trial_per_type", "min_pixels", "max_pixels"):
        if key in result and (type(result[key]) is not int or result[key] < 1):
            raise ValueError(f"{key} must be a positive integer")
    for key in ("fallback_stage_count", "n_neighbors"):
        if key in result and (type(result[key]) is not int or result[key] < 2):
            raise ValueError(f"{key} must be an integer of at least 2")
    for key in (
        "trial",
        "overwrite",
        "clear_manual_flags",
        "save_vis",
        "refit",
        "include_positives",
        "allow_pick_to_give",
    ):
        if key in result and type(result[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    for key in ("box_threshold", "text_threshold", "min_dist", "uncertainty_threshold", "oversample_factor"):
        if key in result and (
            type(result[key]) not in (int, float) or not math.isfinite(result[key]) or result[key] < 0
        ):
            raise ValueError(f"{key} must be a finite non-negative number")
    for key in ("box_threshold", "text_threshold"):
        if result.get(key, 0) > 1:
            raise ValueError(f"{key} must be between 0 and 1")
    if "method" in result and result["method"] not in {"auto", "umap", "pca"}:
        raise ValueError("Unknown projection method")
    if "run_mode" in result and result["run_mode"] not in {"missing", "full"}:
        raise ValueError("Unknown labeling run mode")
    if "per_scenario_counts" in result:
        counts = result["per_scenario_counts"]
        if not isinstance(counts, dict) or any(
            type(count) is not int or count < 0 for count in counts.values()
        ):
            raise ValueError("Scenario counts must be non-negative integers")
    if operation in {"curation.labeling", "curation.tagging"}:
        for key in ("trial_per_type", "trial_seed"):
            if key in result and (type(result[key]) is not int or result[key] < 0):
                raise ValueError(f"{key} must be a non-negative integer")
    if "output_variant" in result:
        value = str(result["output_variant"] or "")
        if value and (not value.replace("-", "").replace("_", "").isalnum() or len(value) > 80):
            raise ValueError("Invalid result variant")
    if operation == "curation.embedding" and not result.get("ckpt_path"):
        raise ValueError("Select a checkpoint on the execution node")
    return result


@lru_cache(maxsize=1)
def worker_capabilities() -> dict:
    from lerobot.data_platform.precompute.embedding import get_capabilities as embedding_capabilities
    from lerobot.data_platform.precompute.labeling import get_capabilities as labeling_capabilities
    from lerobot.data_platform.precompute.tagging.vlm_backend import get_capabilities as tagging_capabilities

    labeling = labeling_capabilities()
    tagging = tagging_capabilities()
    for group in (labeling, tagging):
        for backend in group.get("backends", {}).values():
            if backend.get("requires_token") and not backend.get("token_configured"):
                backend["available"] = False
                backend["error"] = "Configure the model credential in the execution node environment"
    return {
        "curation_protocol": CURATION_PROTOCOL,
        "curation_operations": sorted(OPERATIONS),
        "curation_backends": {
            "labeling": labeling,
            "tagging": tagging,
            "embedding": embedding_capabilities(),
        },
    }


def hydrate_builder(store: LifecycleStore, snapshot: dict, source: Path, options: dict) -> DatasetVersion:
    """Restore only immutable inputs in an attempt-local, disposable builder ledger."""
    value = CurationSnapshot.from_dict(snapshot)
    for parent in options.get("parents", []):
        store._put_record("versions", parent["version_id"], parent, immutable=True)
    for batch in options.get("source_batches", []):
        store._put_record("source_batches", batch["source_batch_id"], batch, immutable=True)
    artifact = store._persist_identity_artifact(value.identity)
    version = DatasetVersion.from_dict(
        {
            **value.version,
            "root": str(source),
            "identity_artifact_uri": str(store._identity_path(artifact.artifact_digest)),
        }
    )
    store._put_record("versions", version.version_id, version.to_dict(), immutable=True)
    atomic_json(store._content_manifest_path(version.fingerprint), value.content)
    tasks = TaskConfigSnapshot.from_dict(options.get("task_config") or value.task_config)
    store.repository.put(
        "task_catalogs", tasks.catalog.catalog_version_id, tasks.catalog.to_dict(), immutable=True
    )
    return version


def _review_inputs(static: Path, snapshot: dict, workspace: dict | None) -> None:
    """Project reviewed labels/tags into the old operators' input format, without mutating their runs."""
    if not workspace:
        return
    from lerobot.data_platform.precompute.labeling.review import load_labels_jsonl, resolved_labels_path

    originals = load_labels_jsonl(resolved_labels_path(static / "labeling"))
    episodes = {int(row["episode_index"]): row for row in snapshot["episodes"]}
    indices = DatasetVersion.from_dict(snapshot["version"]).index_by_uid()
    labels, tags = [], []
    for patch in workspace.get("annotation_patches", []):
        index = indices[patch["episode_ref"]["episode_uid"]]
        fields = patch["fields"]
        if fields.get("first_frame_bbox"):
            bbox = fields["first_frame_bbox"]
            original = originals.get(index, {})
            labels.append(
                {
                    **original,
                    "episode_index": index,
                    "task": fields.get("task")
                    or original.get("task")
                    or (episodes[index].get("tasks") or [""])[0],
                    "detections_all_target": bbox.get("all_target", []),
                    "detections_target": [bbox["target"]] if bbox.get("target") else [],
                    "detections_ref": [bbox["reference"]] if bbox.get("reference") else [],
                    "selected": bbox.get("selected"),
                    "selected_target": bbox.get("selected_target", bbox.get("selected")),
                    "active_arm": bbox.get("active_arm"),
                    "relation_satisfied": bbox.get("relation_satisfied"),
                    "manual": True,
                }
            )
        if fields.get("tags"):
            tags.append({"episode_index": index, "tags": fields["tags"]})
    for folder, filename, rows in (
        ("labeling", "labels_reviewed.jsonl", labels),
        ("tagging", "tags_reviewed.jsonl", tags),
    ):
        if rows:
            path = static / folder / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def execute_curation_job(agent, job: dict, source: Path, location: dict) -> dict:
    from lerobot.data_platform.agent import _dataset_payload, _json_value, _require_within
    from lerobot.data_platform.viewer import MetaOnlyDataset

    options = job["options"]
    operation = job["operation"]
    parameters = validate_parameters(operation, options.get("parameters", {}))
    kind = operation.split(".")[1]
    snapshot = options.get("snapshot")
    expected = snapshot["version"]["fingerprint"] if snapshot else None

    def progress(payload):
        agent.client.event(
            agent.state, job["job_id"], str(payload.get("message", "Curation in progress")), payload
        )

    progress({"phase": "validating", "message": "Checking source version"})
    before, _ = dataset_snapshot(source)
    if kind != "snapshot" and expected and before != expected:
        raise ValueError("Source changed; synchronize a new dataset version before continuing")
    aliases = {
        "stage": "auto_stage",
        "quality": "quality_flags",
        "embedding": "embedding",
        "project": "embedding",
    }
    require_dataset_operation(source, aliases.get(kind, kind))
    folder = _require_within(Path(location["output_dir"]), agent.writable_roots, "Curation staging")
    bundle = folder / "curation-result"
    static = bundle / "static"
    static.mkdir(parents=True, exist_ok=True)
    input_root = options.get("input_root")
    if input_root:
        inputs = Path(input_root)
        for name in options.get("input_files", {}):
            path = artifact_path(inputs, name)
            destination = artifact_path(static, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    _review_inputs(static, snapshot, options.get("workspace"))
    meta = MetaOnlyDataset(location["dataset_key"], root=source).meta
    if kind in {"labeling", "tagging"}:
        trial = parameters.pop("trial", False)
        per_type = parameters.pop("trial_per_type", 20)
        seed = parameters.pop("trial_seed", 0)
        if trial:
            from lerobot.data_platform.precompute.labeling.runner import sample_episodes_by_task_type

            sample = sample_episodes_by_task_type(
                source, meta, per_type=per_type, episodes=parameters.get("episodes"), seed=seed
            )
            if not sample.episodes:
                raise ValueError("No supported episodes for a trial")
            parameters["episodes"] = sample.episodes
            parameters["output_variant"] = "trial"
            progress({"message": f"Trial sample: {sample.counts}; seed={sample.seed}"})
    result = {}
    progress({"phase": "computing", "message": f"Running {kind}"})
    if kind == "snapshot":
        value = scan_snapshot(
            source,
            location["dataset_key"],
            options["dataset_id"],
            previous=snapshot,
            task_config=options.get("task_config"),
        )
        atomic_json(bundle / "snapshot.json", value.to_dict())
    elif kind == "validate_source":
        result = {"fingerprint": before}
    elif kind == "stage":
        import csv

        from lerobot.data_platform.cli import run_precompute

        result = run_precompute(
            root=source,
            repo_id=location["dataset_key"],
            output_dir=bundle,
            prepare_videos=False,
            prepare_csv=True,
            downsample=1,
            visualize_only=True,
            force_recompute_stage=True,
            task_config=options.get("task_config"),
            progress_callback=progress,
            show_progress=False,
            **parameters,
        )
        transitions = {}
        skipped = []
        for path in (static / "csv").glob("episode_*_ds1.csv"):
            metadata_path = path.with_suffix(".stages.json")
            if not metadata_path.is_file():
                skipped.append(int(path.name.split("_")[1]))
                continue
            profile = json.loads(metadata_path.read_text())
            maximum = max(1, int(profile["stage_count"]) - 1)
            rows = []
            with path.open() as handle:
                for row in csv.DictReader(handle):
                    if "stage" not in row:
                        continue
                    state = int(round(float(row["stage"]) * maximum))
                    if not rows or rows[-1]["state"] != state:
                        rows.append({"time": float(row["timestamp"]), "state": state})
            if rows:
                transitions[str(int(path.name.split("_")[1]))] = rows
        result = {"transitions": transitions, "skipped_episodes": skipped}
    elif kind == "quality":
        from lerobot.data_platform.precompute.preprocess.quality_flags import run_quality_flag_detection

        result = run_quality_flag_detection(source, static, progress_callback=progress, **parameters)
    elif kind == "labeling":
        from lerobot.data_platform.precompute.labeling import run_labeling

        episodes = parameters.pop("episodes", None)
        result = run_labeling(
            source, meta, episodes, static, progress_callback=progress, show_progress=False, **parameters
        )
    elif kind == "tagging":
        from lerobot.data_platform.precompute.tagging.runner import run_tagging

        episodes = parameters.pop("episodes", None)
        result = run_tagging(source, meta, episodes, static, progress_callback=progress, **parameters)
    elif kind == "embedding":
        from lerobot.data_platform.precompute.embedding import run_embedding

        episodes = parameters.pop("episodes", None)
        checkpoint = Path(parameters.pop("ckpt_path")).expanduser()
        model_roots = [
            Path(path) for path in os.environ.get("DATA_PLATFORM_MODEL_ROOTS", "").split(os.pathsep) if path
        ]
        checkpoint = _require_within(checkpoint, [*agent.allowed_roots, *model_roots], "model checkpoint")
        result = run_embedding(
            source, meta, episodes, static, checkpoint, progress_callback=progress, **parameters
        )
    elif kind == "project":
        from lerobot.data_platform.precompute.embedding import project_existing_embeddings

        result = project_existing_embeddings(static, **parameters)
    elif kind == "compare_summary":
        from lerobot.data_platform.precompute.compare.overlap import scenario_distribution, tag_distribution
        from lerobot.data_platform.precompute.compare.stats import action_stats, metadata_stats
        from lerobot.data_platform.precompute.compare.visual import visual_samples
        from lerobot.data_platform.precompute.construction.vocab import build_vocab
        from lerobot.data_platform.precompute.embedding import load_points

        result = {
            "metadata": metadata_stats(source, meta),
            "action": action_stats(source, meta),
            "scenarios": scenario_distribution(meta),
            "embedding": load_points(static, meta),
            "tags": tag_distribution(static, static),
            "vocab": sorted(build_vocab(meta)),
            "visual": visual_samples(
                location["dataset_key"],
                meta,
                [key for key, feature in meta.features.items() if feature.get("dtype") in {"image", "video"}],
            ),
        }
        atomic_json(bundle / "compare.json", result)
    elif kind == "construction_preview":
        from lerobot.data_platform.precompute.construction import preview_construction

        result = preview_construction(
            meta, static / "labeling", task_config=options.get("task_config"), **parameters
        )
    elif kind in {"construction", "materialize"}:
        output = _require_within(Path(options["out_root"]), agent.writable_roots, "output dataset")
        if output.exists() or output == source or source in output.parents or output in source.parents:
            raise ValueError("Output must be a new sibling dataset")
        with tempfile.TemporaryDirectory(prefix="curation-build-", dir=folder) as temporary:
            ledger = LifecycleStore(Path(temporary))
            base = hydrate_builder(ledger, snapshot, source, options)
            if kind == "materialize":
                manifest, profile = options["manifest"], options["profile"]
                ledger._put_record("manifests", manifest["manifest_id"], manifest, immutable=True)
                ledger._put_record("profiles", profile["profile_id"], profile, immutable=True)
                version, materialization = materialize_manifest(
                    ledger,
                    manifest["manifest_id"],
                    output,
                    profile_id=profile["profile_id"],
                    progress_callback=progress,
                    **parameters,
                )
                result = {"materialization": materialization}
            else:
                from lerobot.data_platform.precompute.construction import run_construction

                built = run_construction(
                    source,
                    meta,
                    static / "labeling",
                    output,
                    {**parameters, "task_config": options.get("task_config")},
                    progress_callback=progress,
                )
                from lerobot.data_platform.precompute.dataset_io import load_episode_records

                generated = {plan.new_episode_index: plan.src_episode_index for plan in built.plans}
                lineage = {}
                uids = {}
                original_indices = sorted(base.uid_by_index())
                for row in load_episode_records(output):
                    index = int(row["episode_index"])
                    source_index = generated.get(
                        index, original_indices[index] if index < len(original_indices) else None
                    )
                    if source_index is None:
                        raise ValueError("Construction output has no episode lineage")
                    source_uid = base.uid_by_index()[source_index]
                    lineage[index] = {"dataset_version_id": base.version_id, "episode_uid": source_uid}
                    uids[index] = (
                        uuid.uuid5(
                            uuid.NAMESPACE_URL, content_digest([base.version_id, parameters, index])
                        ).hex
                        if index in generated
                        else source_uid
                    )
                version = ledger.ingest(
                    output,
                    built.repo_id,
                    parent_version_ids=[base.version_id],
                    operation="construction",
                    stage="curated",
                    episode_uid_by_index=uids,
                    episode_lineage_by_index=lineage,
                )
                result = {"construction": _json_value(asdict(built))}
            value = snapshot_from_version(ledger, version, output, options.get("task_config"))
            atomic_json(bundle / "snapshot.json", value.to_dict())
            from lerobot.data_platform.cli import get_default_output_dir, run_precompute

            progress({"phase": "preparing_viewer", "message": "Preparing the output dataset for review"})
            run_precompute(
                root=output,
                repo_id=version.dataset_key,
                output_dir=get_default_output_dir(output),
                visualize_only=True,
                prepare_workers=parameters.get("workers", 4),
                task_config=options.get("task_config"),
                progress_callback=progress,
                show_progress=False,
            )
            agent._upload_viewer_artifacts(job, get_default_output_dir(output) / "static", derived=True)
            result["dataset_location"] = _dataset_payload(output, node_name=agent.name)
    else:
        raise ValueError("Unsupported Curation operation")
    if is_dataclass(result):
        result = asdict(result)
    result = _json_value(result)
    if kind in {"labeling", "tagging"}:
        result["output_variant"] = parameters.get("output_variant")
    progress({"phase": "validating", "message": "Validating source and result artifacts"})
    after, _ = dataset_snapshot(source)
    if after != before:
        raise ValueError("Source changed during Curation execution")
    atomic_json(bundle / "result.json", result)
    bundle_manifest(bundle, operation=operation, target=options["target"])
    paths = sorted(
        (path for path in bundle.rglob("*") if path.is_file()),
        key=lambda path: path.name == "curation-manifest.json",
    )
    for index, path in enumerate(paths):
        progress(
            {
                "phase": "uploading",
                "current": index,
                "total": len(paths),
                "message": f"Publishing artifact {index + 1}/{len(paths)}",
            }
        )
        agent.client.upload_artifact(
            agent.state, job["job_id"], path.relative_to(bundle), path, curation=True
        )
    return {**result, "curation_protocol": CURATION_PROTOCOL}

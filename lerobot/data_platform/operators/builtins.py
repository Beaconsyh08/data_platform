"""Metadata only: importing this module never imports an algorithm or model."""

from .contracts import OperatorSpec

_PREPROCESS = {
    "convert_action": ("action_dim:run_convert_action", "Trim action/state vectors to a target dimension."),
    "convert_v3": ("dataset_version:run_convert_v3", "Convert compatible LeRobot dataset layouts."),
    "drop_field": ("field_ops:run_drop_field", "Remove a field from a derived dataset."),
    "smooth_action": ("smooth_action:run_smooth_action", "Smooth selected action/state signals."),
    "standardize": ("standardize:run_standardize_dataset", "Standardize compatible DVT data."),
    "value_edit": ("value_edit:run_value_edits", "Edit selected signal values in a derived dataset."),
    "quality_flags": (
        "quality_flags:run_quality_flag_detection",
        "Inspect dataset quality and report evidence.",
    ),
    "clear_flags": ("flag_clear:clear_all_flags", "Clear legacy quality flags."),
    "flag_fixes": ("flag_fixes:run_flag_fix", "Apply legacy flag-specific repairs."),
    "split": ("dataset_split:run_split", "Create a reindexed dataset subset."),
    "merge": ("dataset_merge:run_merge", "Merge compatible registered datasets."),
    "subtract": ("dataset_subtract:run_subtract", "Subtract episodes from a dataset."),
    "rewrite_prompts": ("prompt_rewrite:run_rewrite_prompts", "Rewrite prompts in a derived dataset."),
    "fix_prompt_prepositions": ("prompt_rewrite:run_fix_prompt_prepositions", "Repair prompt prepositions."),
    "lowercase_prompts": ("prompt_rewrite:run_lowercase_prompts", "Normalize prompt case."),
    "stage_return_alignment": ("stage_return_alignment:run_stage_return_alignment", "Align return stages."),
    "stage_return_height_alignment": (
        "stage_return_height_alignment:run_stage_return_height_alignment",
        "Align return heights.",
    ),
    "trajectory_cleanup": (
        "trajectory_cleanup:run_trajectory_cleanup",
        "Clean compatible joint trajectories.",
    ),
    "pose_interpolate": (
        "pose_interpolate:run_pose_interpolate",
        "Interpolate explicitly reviewed pose intervals.",
    ),
}
_REMOTE_PREPROCESS = {
    "convert_action",
    "convert_v3",
    "drop_field",
    "merge",
    "smooth_action",
    "split",
    "standardize",
    "value_edit",
    "pose_interpolate",
    "subtract",
    "stage_return_alignment",
    "stage_return_height_alignment",
    "trajectory_cleanup",
}
_CURATION_PARAMETERS = {
    "snapshot": (),
    "validate_source": (),
    "stage": ("episodes", "prepare_workers", "fallback_stage_count", "data_version"),
    "quality": (
        "episodes",
        "data_version",
        "workers",
        "overwrite",
        "clear_manual_flags",
        "checks",
        "qc_config",
        "video_mode",
        "video_workers",
    ),
    "labeling": (
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
    ),
    "tagging": (
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
    ),
    "embedding": ("episodes", "ckpt_path", "layer_hook", "openpi_config", "refit", "workers", "devices"),
    "project": ("method", "seed", "n_neighbors", "min_dist", "metric"),
    "compare_summary": (),
    "construction_preview": ("uncertainty_threshold", "allow_pick_to_give"),
    "construction": (
        "uncertainty_threshold",
        "per_scenario_counts",
        "include_positives",
        "oversample_factor",
        "allow_pick_to_give",
    ),
    "materialize": ("workers",),
}


def _curation_schema(parameters):
    bools = {
        "trial",
        "overwrite",
        "clear_manual_flags",
        "save_vis",
        "refit",
        "include_positives",
        "allow_pick_to_give",
    }
    ints = {
        "workers",
        "prepare_workers",
        "trial_per_type",
        "min_pixels",
        "max_pixels",
        "fallback_stage_count",
        "n_neighbors",
        "trial_seed",
        "seed",
    }
    numbers = {"box_threshold", "text_threshold", "min_dist", "uncertainty_threshold", "oversample_factor"}
    props = {}
    for name in parameters:
        if name == "video_workers":
            props[name] = {"type": "integer", "minimum": 1, "maximum": 16}
        elif name == "video_mode":
            props[name] = {"type": "string", "enum": ["sampled", "full"]}
        elif name in bools:
            props[name] = {"type": "boolean"}
        elif name in ints:
            props[name] = {"type": "integer", "minimum": 0 if name in {"trial_seed", "seed"} else 1}
        elif name in numbers:
            props[name] = {"type": "number", "minimum": 0}
        elif name == "devices":
            props[name] = {
                "type": ["string", "null"] if "ckpt_path" in parameters else ["string", "array", "null"],
                "items": {"type": "string"},
            }
        elif name in {"episodes", "checks", "selected_tags"}:
            item = {"type": "integer", "minimum": 0} if name == "episodes" else {"type": "string"}
            props[name] = {
                "type": ["array", "null"] if name in {"episodes", "selected_tags"} else "array",
                "items": item,
            }
            if name == "episodes":
                # Curation execution treats omitted and null episode selection as all episodes.
                # Publish that default so previews, historical comparisons and admission agree.
                props[name]["default"] = None
        elif name in {"qc_config", "per_scenario_counts"}:
            props[name] = {"type": "object"}
        else:
            nullable = name in {
                "data_version",
                "endpoint",
                "qwen_model",
                "vlm_endpoint",
                "output_variant",
                "openpi_config",
            }
            props[name] = {"type": ["string", "null"] if nullable else "string"}
    return {"type": "object", "properties": props, "required": [], "additionalProperties": False}


def builtins() -> list[OperatorSpec]:
    result = []
    for name, (entry, description) in _PREPROCESS.items():
        source = name in {"flag_fixes", "rewrite_prompts", "fix_prompt_prepositions", "lowercase_prompts"}
        result.append(
            OperatorSpec(
                f"preprocess.{name}",
                "1.0.0",
                description,
                "preprocess",
                "source" if source else "cache" if name in {"quality_flags", "clear_flags"} else "dataset",
                "lerobot.data_platform.precompute.preprocess." + entry,
                legacy_remote=name
                in {
                    "convert_action",
                    "convert_v3",
                    "drop_field",
                    "merge",
                    "smooth_action",
                    "split",
                    "standardize",
                    "value_edit",
                },
                remote=name in _REMOTE_PREPROCESS,
                parallel=name in _REMOTE_PREPROCESS,
                cancellable=name in _REMOTE_PREPROCESS,
                output_type="artifact" if name in {"quality_flags", "clear_flags"} else "dataset",
                required_modules=("pandas", "pyarrow", "numpy"),
                source_commit="6ba72ff6ca4ccb143eeeb15bcafd3047aac14845"
                if name == "pose_interpolate"
                else None,
                review_required=name == "pose_interpolate",
                requires=("quality_report",) if name == "pose_interpolate" else (),
            )
        )
    for name, parameters in _CURATION_PARAMETERS.items():
        dataset = name in {"construction", "materialize"}
        result.append(
            OperatorSpec(
                f"curation.{name}",
                "1.0.0",
                f"Run {name.replace('_', ' ')} with versioned Curation evidence.",
                "curation",
                "dataset" if dataset else "cache",
                parallel=True,
                cancellable=True,
                output_type="dataset" if dataset else "artifact",
                parameter_names=parameters,
                provides={
                    "quality": ("quality_report",),
                    "labeling": ("labels",),
                    "tagging": ("tags",),
                    "embedding": ("embeddings",),
                    "project": ("projection",),
                }.get(name, ()),
                requires={
                    "project": ("embeddings",),
                    "construction": ("labels",),
                    "construction_preview": ("labels",),
                }.get(name, ()),
                parameter_schema_override=_curation_schema(parameters),
                local_entry_point={
                    "labeling": "lerobot.data_platform.precompute.labeling.runner:run_labeling",
                    "tagging": "lerobot.data_platform.precompute.tagging.runner:run_tagging",
                    "embedding": "lerobot.data_platform.precompute.embedding.runner:run_embedding",
                    "construction": "lerobot.data_platform.precompute.construction.runner:run_construction",
                }.get(name, ""),
            )
        )
    local_entries = {
        "delete_episodes": "lerobot.data_platform.precompute.preprocess.delete_episodes:delete_episodes_inplace",
        "repair_v3_video_timestamps": "lerobot.data_platform.precompute.preprocess.dataset_version:repair_v3_video_timestamps",
        **{
            name: "lerobot.data_platform.precompute.preprocess." + _PREPROCESS[name][0]
            for name in (
                "value_edit",
                "rewrite_prompts",
                "fix_prompt_prepositions",
                "lowercase_prompts",
                "clear_flags",
                "flag_fixes",
            )
        },
    }
    local_entries["apply_prompt_assignments"] = (
        "lerobot.data_platform.processing.compatibility:apply_prompt_assignments"
    )
    for name in ("trim_episode", *local_entries):
        result.append(
            OperatorSpec(
                f"mutation.{name}",
                "1.0.0",
                f"Privileged source operation: {name}.",
                "mutation",
                "source",
                remote=name
                in {"trim_episode", "delete_episodes", "repair_v3_video_timestamps", "value_edit"},
                local_entry_point=local_entries.get(name, ""),
            )
        )
    result.extend(
        [
            OperatorSpec(
                "viewer.prepare",
                "1.0.0",
                "Prepare a read-only Viewer cache.",
                "viewer",
                "cache",
                parallel=True,
                cancellable=True,
                local_entry_point="lerobot.data_platform.cli:run_precompute",
                parameter_names=(
                    "prepare_videos",
                    "prepare_csv",
                    "prepare_workers",
                    "data_version",
                    "downsample",
                    "max_frames",
                    "task_config",
                ),
            ),
            OperatorSpec(
                "caption.annotate",
                "1.1.0",
                "Annotate temporal captions with explicit signal selection.",
                "caption",
                "cache",
                parallel=True,
                cancellable=True,
                parameter_names=("episode_index", "scheme", "include_signals"),
                parameter_schema_override={
                    "type": "object",
                    "properties": {
                        "episode_index": {"type": "integer", "minimum": 0},
                        "scheme": {
                            "type": "string",
                            "enum": ["multiview_semantics", "video_events", "fusion_review", "flora"],
                        },
                        "include_signals": {"type": "boolean", "default": True},
                    },
                    "required": ["episode_index", "scheme"],
                    "additionalProperties": False,
                },
            ),
            OperatorSpec(
                "import.mcap.inspect",
                "1.0.0",
                "Inspect a registered MCAP source.",
                "mcap",
                "read",
                parallel=True,
                cancellable=True,
                input_type="mcap",
                parameter_names=("config",),
                parameter_schema_override={
                    "type": "object",
                    "properties": {"config": {"type": "object"}},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            OperatorSpec(
                "import.mcap.convert",
                "1.0.0",
                "Convert MCAP with verified checkpoints.",
                "mcap",
                "dataset",
                parallel=True,
                cancellable=True,
                checkpoint=True,
                output_type="dataset",
                input_type="mcap",
                parameter_names=("config",),
                parameter_schema_override={
                    "type": "object",
                    "properties": {"config": {"type": "object"}},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            OperatorSpec(
                "preprocess.apply_prompt_assignments",
                "1.0.0",
                "Apply reviewed task assignments.",
                "local",
                "cache",
                remote=False,
            ),
            OperatorSpec(
                "task_mapping_cache",
                "1.0.0",
                "Rebuild a versioned task mapping cache.",
                "local",
                "cache",
                remote=False,
            ),
            OperatorSpec(
                "analysis", "1.0.0", "Read bounded dataset analysis.", "local", "read", remote=False
            ),
            OperatorSpec("inspect", "1.0.0", "Read bounded dataset records.", "local", "read", remote=False),
        ]
    )
    for operator_id, effect, description in (
        ("compare.build", "cache", "Build the legacy two-dataset comparison cache."),
        ("tasks.suggest", "cache", "Propose robot task definitions for human review."),
        ("lifecycle.ingest", "read", "Register a local immutable source version."),
        ("lifecycle.preprocess", "dataset", "Execute a published local preprocessing profile."),
        ("lifecycle.materialize", "dataset", "Materialize a published local Curation manifest."),
        ("viewer.preload", "cache", "Load legacy Viewer metadata and caches."),
    ):
        result.append(
            OperatorSpec(
                operator_id,
                "1.0.0",
                description,
                "compatibility",
                effect,
                remote=False,
                output_type="dataset" if effect == "dataset" else "artifact",
            )
        )
    return result

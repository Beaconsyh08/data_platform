"""Deterministic, bounded plans with explicit data and evidence dependencies."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from lerobot.data_platform.operators import get_operator
from lerobot.data_platform.operators.schema import validate_parameters
from lerobot.data_platform.task_catalog import content_digest


class PipelineValidationError(ValueError):
    """A strict parse failure with a safe, step-addressable preview diagnostic."""

    def __init__(self, reason, row, position, code="invalid_step", **details):
        super().__init__(reason)
        self.issue = {
            "step_id": row.get("step_id", "") if isinstance(row, dict) else "",
            "operator_id": row.get("operator_id", "") if isinstance(row, dict) else "",
            "position": position,
            "code": code,
            "reason": reason,
            **details,
        }


@dataclass(frozen=True)
class PipelineStep:
    step_id: str
    operator_id: str
    version: str
    parameters: dict
    input: str = "source"
    artifacts: tuple[str, ...] = ()
    review_before: bool = False
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineSpec:
    name: str
    steps: tuple[PipelineStep, ...]
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {"name": self.name, "steps": [asdict(step) for step in self.steps], "schema_version": 1}

    @property
    def digest(self) -> str:
        return content_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict) -> PipelineSpec:
        if not isinstance(value, dict) or set(value) - {"name", "steps", "schema_version"}:
            raise ValueError("Invalid pipeline definition")
        name = str(value.get("name") or "").strip()
        if not name or len(name) > 128:
            raise ValueError("Pipeline name must contain 1–128 characters")
        rows = value.get("steps")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
            raise ValueError("A pipeline requires 1–50 steps")
        known, steps = {}, []
        for position, row in enumerate(rows, 1):
            try:
                if not isinstance(row, dict) or set(row) - {
                    "step_id",
                    "operator_id",
                    "version",
                    "parameters",
                    "input",
                    "artifacts",
                    "review_before",
                    "options",
                }:
                    raise ValueError("Invalid pipeline step")
                key = row.get("step_id")
                if (
                    not isinstance(key, str)
                    or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,39}", key)
                    or key in known
                    or key == "source"
                ):
                    raise ValueError("Step IDs must be unique identifiers of at most 40 characters")
                spec = get_operator(str(row.get("operator_id") or ""), row.get("version"))
                if not spec.pipeline_allowed:
                    raise ValueError(f"{spec.operator_id} requires its dedicated authorized entry point")
                if value.get("schema_version", 1) != 1:
                    raise ValueError("Unsupported pipeline schema version")
                source = row.get("input", "source")
                if not isinstance(source, str):
                    raise ValueError("Dataset input must be a step identifier")
                if source != "source" and (source not in known or known[source].output_type != "dataset"):
                    raise PipelineValidationError(
                        "Dataset input must reference source or an earlier dataset output",
                        row,
                        position,
                        "invalid_input",
                    )
                if not isinstance(source, str):
                    raise ValueError("Dataset input must be a step identifier")
                artifacts = row.get("artifacts", [])
                if not isinstance(artifacts, (list, tuple)) or any(
                    not isinstance(item, str) or item not in known for item in artifacts
                ):
                    raise PipelineValidationError(
                        "Evidence must reference earlier pipeline steps",
                        row,
                        position,
                        "invalid_evidence_reference",
                    )
                options = row.get("options", {})
                allowed = {
                    "source_location_ids",
                    "workspace_id",
                    "expected_revision",
                    "manifest_id",
                    "profile_id",
                }
                if not isinstance(options, dict) or set(options) - allowed:
                    raise ValueError("Unsupported pipeline step options")
                if "source_location_ids" in options and (
                    spec.operator_id not in {"preprocess.merge", "preprocess.subtract"}
                    or not isinstance(options["source_location_ids"], list)
                    or any(not isinstance(item, str) for item in options["source_location_ids"])
                ):
                    raise ValueError("Registered secondary inputs are supported only for Merge and Subtract")
                try:
                    parameters = validate_parameters(spec, row.get("parameters", {}))
                except (ValueError, TypeError) as exc:
                    raise PipelineValidationError(str(exc), row, position, "invalid_parameters") from exc
                if spec.output_type == "dataset" and parameters.get("dry_run"):
                    raise ValueError("A dataset-producing pipeline step cannot be a dry run")
                review = row.get("review_before", False)
                if type(review) is not bool:
                    raise ValueError("review_before must be a boolean")
                provided = {
                    kind
                    for item in artifacts
                    if next(previous for previous in steps if previous.step_id == item).input == source
                    for kind in known[item].provides
                }
                if not set(spec.requires) <= provided:
                    raise PipelineValidationError(
                        f"Missing required evidence: {', '.join(sorted(set(spec.requires) - provided))}",
                        row,
                        position,
                        "missing_evidence",
                        required=sorted(set(spec.requires) - provided),
                        suggestion="Reference a compatible earlier producer on the same dataset input, or add it first.",
                    )
                review = review or spec.review_required
                steps.append(
                    PipelineStep(
                        key,
                        spec.operator_id,
                        spec.version,
                        parameters,
                        source,
                        tuple(artifacts),
                        review,
                        dict(options),
                    )
                )
                known[key] = spec
            except PipelineValidationError:
                raise
            except (ValueError, TypeError, KeyError) as exc:
                raise PipelineValidationError(str(exc), row, position) from exc
        return cls(name, tuple(steps))


class AvailabilityReason(str):
    """String-compatible reason with a stable preview category."""

    def __new__(cls, message, code):
        instance = super().__new__(cls, message)
        instance.code = code
        return instance


def availability(
    spec, node: dict, info: dict, parameters: dict | None = None, *, validate_dataset=True
) -> str | None:
    """Intersect dataset semantics with the owning node's installed implementation."""
    caps = node.get("capabilities", {})
    if not spec.pipeline_allowed:
        return AvailabilityReason("Use the dedicated authorized operation form", "permission_denied")
    version = str(info.get("codebase_version") or "")
    if (
        validate_dataset
        and version
        and spec.input_type == "dataset"
        and version not in spec.supported_formats
    ):
        return AvailabilityReason("Dataset format is unsupported by this operator", "unsupported_data")
    if caps.get("job_protocol", 1) < 2:
        return "Upgrade this node to durable Job protocol 2"
    if node.get("status") != "online":
        return "Execution node is offline"
    installed = caps.get("operator_version_history", {}).get(
        spec.operator_id, [caps.get("operator_versions", {}).get(spec.operator_id)]
    )
    if spec.version not in installed:
        return (
            caps.get("operator_unavailable", {}).get(spec.operator_id)
            or "Upgrade the execution node to install this operator version"
        )
    if spec.operator_id not in caps.get("operations", []):
        return "Operator dependencies or authorization are unavailable on this node"
    from lerobot.data_platform.precompute.data_profile import require_operation

    try:
        if (
            validate_dataset
            and spec.operator_id != "curation.quality"
            and not spec.operator_id.startswith("import.mcap.")
        ):
            require_operation(info, spec.operator_id)
        if spec.operator_id.startswith("curation.") and parameters is not None:
            from lerobot.data_platform.curation_execution import validate_parameters as validate_curation

            validate_curation(spec.operator_id, parameters or {})
        if spec.operator_id == "preprocess.pose_interpolate" and parameters is not None and validate_dataset:
            from lerobot.data_platform.precompute.preprocess.pose_interpolate import PoseRepair

            for repair in parameters["repairs"]:
                PoseRepair.parse(repair, info.get("features", {}))
        if spec.family == "caption":
            from lerobot.data_platform.temporal_caption_jobs import validate_options, validate_source

            if validate_dataset:
                validate_source(info)
            if parameters is not None:
                validate_options(parameters)
        if spec.family == "mcap":
            if not caps.get("mcap_import", {}).get("ready"):
                return "MCAP import runtime is unavailable"
            if parameters is not None:
                from lerobot.data_platform.mcap_contract import McapImportConfig

                McapImportConfig.from_dict(parameters.get("config") or {})
        if spec.operator_id == "curation.quality":
            checks = (parameters or {}).get("checks", ["legacy"])
            installed = set(caps.get("quality_checks", []))
            if parameters is None:
                checks = list(installed)
            if not checks or not set(checks).issubset(installed):
                return AvailabilityReason(
                    "Selected quality checks are unavailable on this node", "invalid_parameters"
                )
            if validate_dataset:
                from lerobot.data_platform.precompute.data_profile import (
                    profile_from_info,
                    quality_check_capabilities,
                )

                supported = quality_check_capabilities(profile_from_info(info), info.get("features", {}))
                reasons = [
                    supported.get(check, {}).get("reason") or f"Unsupported Quality check: {check}"
                    for check in checks
                    if not supported.get(check, {}).get("available")
                ]
                if reasons and (parameters is not None or len(reasons) == len(checks)):
                    return AvailabilityReason("; ".join(reasons), "unsupported_data")
        if spec.operator_id in {"curation.labeling", "curation.tagging", "curation.embedding"}:
            kind = spec.operator_id.split(".")[1]
            backend = caps.get("curation_backends", {}).get(kind, {})
            defaults = {
                "labeling": ("backend", "grounding_dino"),
                "tagging": ("vlm_backend", "qwen_dashscope"),
            }
            if kind in defaults:
                key, default = defaults[kind]
                choices = backend.get("backends", {})
                if parameters is None and any(choice.get("available") for choice in choices.values()):
                    backend = {"available": True}
                else:
                    backend = choices.get((parameters or {}).get(key, default), {})
            if not backend.get("available"):
                return backend.get("error") or "Selected model backend is unavailable"
    except (ValueError, TypeError, KeyError) as exc:
        return AvailabilityReason(str(exc), "invalid_parameters")
    return None

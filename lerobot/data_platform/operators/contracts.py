"""Execution-independent descriptions shared by APIs, planners and workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class OperatorSpec:
    operator_id: str
    version: str
    description: str
    family: str
    effect: str
    entry_point: str = ""
    legacy_remote: bool = False
    remote: bool = True
    parallel: bool = False
    cancellable: bool = False
    checkpoint: bool = False
    output_type: str = "artifact"
    input_type: str = "dataset"
    supported_formats: tuple[str, ...] = ("v2.1", "v3.0")
    required_modules: tuple[str, ...] = ()
    resource_requirements: tuple[str, ...] = ("cpu", "memory", "disk")
    parameter_names: tuple[str, ...] = ()
    source_commit: str | None = None
    review_required: bool = False
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    local_entry_point: str = ""
    parameter_schema_override: dict | None = field(default=None, compare=False, hash=False, repr=False)

    @property
    def pipeline_allowed(self) -> bool:
        return self.remote and self.effect != "source"

    def to_dict(self) -> dict:
        from .schema import parameter_schema

        value = asdict(self)
        value.pop("entry_point")
        value.pop("local_entry_point")
        value.pop("parameter_schema_override")
        value.pop("parameter_names")
        value.update(pipeline_allowed=self.pipeline_allowed, parameters=parameter_schema(self))
        return value


@dataclass
class ExecutionContext:
    source_root: Path
    output_root: Path | None = None
    dataset_key: str = ""
    source_roots: tuple[Path, ...] = ()
    work_dir: Path | None = None
    progress: Callable[[dict], None] | None = None
    check_cancelled: Callable[[], None] | None = None
    resources: Any = None
    artifacts: Mapping[str, Path] = field(default_factory=dict)
    handlers: Mapping[str, Callable[[], Any]] = field(default_factory=dict)

    def emit(self, payload: dict) -> None:
        if self.check_cancelled:
            self.check_cancelled()
        if self.progress:
            self.progress(payload)


@dataclass(frozen=True)
class OperatorResult:
    summary: dict = field(default_factory=dict)
    artifacts: tuple[dict, ...] = ()
    derived_datasets: tuple[dict, ...] = ()
    episode_lineage: tuple[dict, ...] = ()

    @classmethod
    def from_legacy(cls, result: Any) -> OperatorResult:
        value = asdict(result) if hasattr(result, "__dataclass_fields__") else dict(result or {})
        derived = value.get("dataset_location")
        if not derived and value.get("out_root"):
            derived = {"root": str(value["out_root"]), "dataset_key": value.get("repo_id")}
        return cls(
            summary=value.get("summary", value),
            derived_datasets=(derived,) if derived else (),
            episode_lineage=tuple(value.get("episode_lineage") or ()),
        )

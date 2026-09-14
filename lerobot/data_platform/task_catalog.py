"""Portable, versioned task semantics shared by the console and node agents.

Task configuration is an overlay. It never edits source dataset metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache

TASK_CONFIG_PROTOCOL = 1
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def content_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_task(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().rstrip(".").lower())


def task_inventory(tasks: list) -> list[str]:
    return sorted(
        {normalize_task(row.get("task", "") if isinstance(row, dict) else row) for row in tasks} - {""}
    )


def is_task_dimension(key: str) -> bool:
    return key in {"task_id", "task_family"} or (
        key.startswith("task_attributes.") and bool(_KEY.fullmatch(key.removeprefix("task_attributes.")))
    )


@dataclass(frozen=True)
class TaskDefinition:
    task_id: str
    label: str
    family: str
    attributes: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    stage_strategy: str = "equal_time"
    stage_count: int = 5

    @classmethod
    def from_dict(cls, value: dict) -> TaskDefinition:
        if not isinstance(value, dict):
            raise ValueError("task definition must be an object")
        task_id, family = str(value.get("task_id") or ""), str(value.get("family") or "")
        if not _KEY.fullmatch(task_id) or not _KEY.fullmatch(family):
            raise ValueError("task_id and family must use lowercase letters, digits and underscores")
        if family in {"unknown", "overview", "review", "pick_group"}:
            raise ValueError("task family name is reserved")
        label = str(value.get("label") or "").strip()
        if not label:
            raise ValueError("task label is required")
        attributes = value.get("attributes") or {}
        if not isinstance(attributes, dict) or any(
            not _KEY.fullmatch(str(key)) or not isinstance(item, str) for key, item in attributes.items()
        ):
            raise ValueError("task attributes must be flat text values with lowercase keys")
        aliases = value.get("aliases") or []
        if not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases):
            raise ValueError("task aliases must be a list of strings")
        strategy = str(value.get("stage_strategy") or "equal_time")
        if strategy not in {"equal_time", "legacy_pick", "legacy_place", "legacy_give"}:
            raise ValueError("unsupported stage strategy")
        if strategy.startswith("legacy_") and strategy != f"legacy_{family}":
            raise ValueError("legacy stage strategy must match the task family")
        count = value.get("stage_count", 5)
        if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 100:
            raise ValueError("stage_count must be an integer between 2 and 100")
        if strategy.startswith("legacy_"):
            count = 6 if family == "give" else 5
        return cls(task_id, label, family, dict(attributes), sorted(set(aliases)), strategy, count)


@dataclass(frozen=True)
class TaskCatalogVersion:
    catalog_version_id: str
    version: int
    tasks: list[TaskDefinition]
    digest: str
    created_at: str
    created_by: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> TaskCatalogVersion:
        return cls(**{**value, "tasks": [TaskDefinition.from_dict(row) for row in value["tasks"]]})


@dataclass(frozen=True)
class ResolvedTask:
    raw_task: str
    task_id: str | None
    family: str
    label: str
    attributes: dict[str, str]
    status: str
    catalog_version_id: str
    stage_strategy: str = "equal_time"
    stage_count: int = 5

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def scene(self) -> str:
        if self.family == "pick":
            return {"absolute": "directional_pick", "relative": "relational_pick"}.get(
                self.attributes.get("position_mode"), "pick"
            )
        return self.family


def _legacy_task(text: str) -> TaskDefinition | None:
    # precompute.__init__ also exports analysis, which imports this module.
    from lerobot.data_platform.precompute.labeling.task_parser import parse_task

    normalized = normalize_task(text)
    parsed = parse_task(text)
    attributes = {}
    if parsed:
        family = parsed.get("action", "pick")
        attributes["object"] = normalize_task(parsed["target"])
        if family == "pick":
            attributes["position_mode"] = (
                "relative" if parsed.get("reference") else "absolute" if parsed.get("direction") else "none"
            )
        if parsed.get("direction"):
            attributes["direction"] = parsed["direction"]
        if parsed.get("reference"):
            attributes["reference"] = normalize_task(parsed["reference"])
    elif re.fullmatch(r"(?:place|put)(?: the)? object", normalized):
        family = "place"
    elif match := re.fullmatch(r"pick (?:up )?(?:the )?(.+)", normalized):
        family = "pick"
        attributes = {"object": match[1], "position_mode": "none"}
    elif match := re.fullmatch(r"(pick|place|put|give|hand over|hand)(?: (?:the )?(.+))?", normalized):
        family = {"put": "place", "hand over": "give", "hand": "give"}.get(match[1], match[1])
        if match[2]:
            attributes["object"] = match[2]
        if family == "pick":
            attributes["position_mode"] = "none"
    else:
        return None
    return TaskDefinition(
        f"legacy_{content_digest([family, attributes])[:20]}",
        text,
        family,
        attributes,
        [text],
        f"legacy_{family}",
        6 if family == "give" else 5,
    )


@lru_cache(maxsize=1)
def builtin_catalog() -> TaskCatalogVersion:
    objects = ["yellow duck", "brown dog", "orange lion", "green dinosaur"]
    prompts = ["Place object"]
    for obj in objects:
        prompts.extend([f"Pick up the {obj}", f"Give the {obj} to me"])
        for direction in ("left", "right"):
            prompts.append(f"Pick up the {obj} on the {direction}")
            prompts.extend(
                f"Pick up the {obj} to the {direction} of the {reference}"
                for reference in objects
                if reference != obj
            )
    definitions = [_legacy_task(prompt) for prompt in prompts]
    for appliance, phrase, location in (
        ("washing_machine", "washing machine", "below"),
        ("clothes_dryer", "clothes dryer", "above"),
    ):
        for action in ("open", "close"):
            prompt = f"{action.title()} the door of the {phrase} {location}"
            definitions.append(
                TaskDefinition(
                    f"{action}_{appliance}_door_{location}",
                    prompt,
                    f"{action}_door",
                    {"object": "door", "appliance": appliance, "location": location},
                    [prompt],
                )
            )
    definitions.append(
        TaskDefinition(
            "load_clothes_washing_machine",
            "Load clothes into the washing machine",
            "load_clothes",
            {"object": "clothes", "appliance": "washing_machine", "destination": "washing_machine_interior"},
            ["Grasp the clothes to the washing machine"],
        )
    )
    tasks = sorted(definitions, key=lambda item: item.task_id)
    digest = content_digest([asdict(item) for item in tasks])
    return TaskCatalogVersion(f"tc_{digest[:24]}", 0, tasks, digest, "", "builtin")


@dataclass(frozen=True)
class TaskConfigSnapshot:
    catalog: TaskCatalogVersion
    mappings: dict[str, str] = field(default_factory=dict)
    inventory: list[str] = field(default_factory=list)
    mapping_version_id: str | None = None

    def to_dict(self) -> dict:
        value = {**asdict(self), "protocol_version": TASK_CONFIG_PROTOCOL}
        value["digest"] = content_digest(value)
        return value

    @classmethod
    def from_dict(cls, value: dict | None) -> TaskConfigSnapshot:
        if value is None:
            return cls(builtin_catalog())
        payload = dict(value)
        digest = payload.pop("digest", None)
        if digest != content_digest(payload):
            raise ValueError("task configuration digest mismatch")
        if payload.pop("protocol_version", None) != TASK_CONFIG_PROTOCOL:
            raise ValueError("unsupported task configuration protocol; upgrade the Agent")
        payload["catalog"] = TaskCatalogVersion.from_dict(payload["catalog"])
        return cls(**payload)

    def resolve(self, text: str) -> ResolvedTask:
        normalized = normalize_task(text)
        explicit = self.mappings.get(normalized)
        candidates = (
            [task for task in self.catalog.tasks if task.task_id == explicit]
            if explicit
            else [
                task
                for task in self.catalog.tasks
                if normalized in {normalize_task(alias) for alias in task.aliases}
            ]
        )
        status = "mapped" if explicit else "alias"
        if len(candidates) == 1:
            task = candidates[0]
        elif not candidates and not explicit and (task := _legacy_task(text)):
            status = "legacy"
        else:
            return ResolvedTask(
                text,
                None,
                "unknown",
                text,
                {},
                "conflict" if candidates or explicit else "unmapped",
                self.catalog.catalog_version_id,
            )
        return ResolvedTask(
            text,
            task.task_id,
            task.family,
            task.label,
            dict(task.attributes),
            status,
            self.catalog.catalog_version_id,
            task.stage_strategy,
            task.stage_count,
        )

    def stage_digest(self, tasks: list[str]) -> str:
        return content_digest(
            [
                [text, resolved.stage_strategy, resolved.stage_count]
                for text in task_inventory(tasks)
                for resolved in [self.resolve(text)]
            ]
        )


@dataclass(frozen=True)
class DatasetTaskMappingVersion:
    mapping_version_id: str
    dataset_key: str
    previous_version_id: str | None
    task_inventory_digest: str
    snapshot: dict
    created_at: str
    created_by: str

    def to_dict(self) -> dict:
        return asdict(self)


class TaskCatalogStore:
    """Use the existing lifecycle repository and its transactions for task records."""

    def __init__(self, repository):
        self.repository = repository

    def catalogs(self) -> list[TaskCatalogVersion]:
        records = [TaskCatalogVersion.from_dict(row) for row in self.repository.list("task_catalogs")]
        return sorted([builtin_catalog(), *records], key=lambda item: item.version, reverse=True)

    def get_catalog(self, version_id: str | None) -> TaskCatalogVersion:
        if not version_id:
            return builtin_catalog()
        for catalog in self.catalogs():
            if catalog.catalog_version_id == version_id:
                return catalog
        raise KeyError("task catalog version not found")

    def create_catalog(
        self, tasks: list[dict], *, expected_version_id: str, created_by: str
    ) -> TaskCatalogVersion:
        if not isinstance(tasks, list):
            raise ValueError("tasks must be a list of task definitions")
        definitions = sorted([TaskDefinition.from_dict(row) for row in tasks], key=lambda item: item.task_id)
        if len({item.task_id for item in definitions}) != len(definitions):
            raise ValueError("duplicate task_id")
        digest = content_digest([asdict(item) for item in definitions])
        with self.repository.transaction() as connection:
            records = self.repository.list("task_catalogs", connection=connection)
            latest = max(
                [builtin_catalog(), *[TaskCatalogVersion.from_dict(row) for row in records]],
                key=lambda item: item.version,
            )
            if latest.catalog_version_id != expected_version_id:
                raise ValueError("task catalog revision conflict; refresh before saving")
            if latest.digest == digest:
                return latest
            catalog = TaskCatalogVersion(
                f"tc_{digest[:24]}",
                latest.version + 1,
                definitions,
                digest,
                datetime.now().astimezone().isoformat(),
                created_by,
            )
            existing = self.repository.get("task_catalogs", catalog.catalog_version_id, connection=connection)
            if existing:
                return TaskCatalogVersion.from_dict(existing)
            self.repository.put(
                "task_catalogs",
                catalog.catalog_version_id,
                catalog.to_dict(),
                immutable=True,
                connection=connection,
            )
            return catalog

    def current(self, dataset_key: str, *, connection=None) -> DatasetTaskMappingVersion | None:
        head = self.repository.get("task_mapping_heads", dataset_key, connection=connection)
        if not head:
            return None
        return DatasetTaskMappingVersion(
            **self.repository.get(
                "task_mappings",
                head["mapping_version_id"],
                connection=connection,
            )
        )

    def snapshot(self, dataset_key: str) -> TaskConfigSnapshot:
        mapping = self.current(dataset_key)
        return TaskConfigSnapshot.from_dict(mapping.snapshot if mapping else None)

    def preview(
        self,
        dataset_key: str,
        tasks: list,
        catalog_version_id: str | None,
        mappings: dict[str, str] | None = None,
    ) -> dict:
        inventory = task_inventory(tasks)
        if not isinstance(mappings or {}, dict):
            raise ValueError("task mappings must be an object")
        normalized_mappings = {}
        for key, value in (mappings or {}).items():
            if not value:
                continue
            normalized = normalize_task(key)
            if normalized in normalized_mappings and normalized_mappings[normalized] != value:
                raise ValueError("task mapping conflict: equivalent texts reference different task_ids")
            normalized_mappings[normalized] = str(value)
        mappings = normalized_mappings
        catalog = self.get_catalog(catalog_version_id)
        if set(mappings) - set(inventory):
            raise ValueError("task mapping contains texts missing from the dataset")
        if set(mappings.values()) - {task.task_id for task in catalog.tasks}:
            raise ValueError("task mapping references a missing task_id")
        snapshot = TaskConfigSnapshot(catalog, mappings, inventory)
        original_texts = {}
        for row in tasks:
            text = str(row.get("task", "") if isinstance(row, dict) else row)
            if normalize_task(text):
                original_texts.setdefault(normalize_task(text), text)
        return {
            "dataset_key": dataset_key,
            "task_inventory_digest": content_digest(inventory),
            "snapshot": snapshot.to_dict(),
            "tasks": [
                {**snapshot.resolve(original_texts[text]).to_dict(), "mapping_key": text}
                for text in inventory
            ],
        }

    def apply(
        self,
        dataset_key: str,
        tasks: list,
        *,
        catalog_version_id: str | None,
        mappings: dict[str, str] | None,
        expected_version_id: str | None,
        expected_inventory_digest: str,
        created_by: str,
        connection=None,
    ) -> DatasetTaskMappingVersion:
        preview = self.preview(dataset_key, tasks, catalog_version_id, mappings)
        if expected_inventory_digest != preview["task_inventory_digest"]:
            raise ValueError("task inventory conflict; preview the current dataset again")
        if connection is None:
            with self.repository.transaction() as transaction:
                return self.apply(
                    dataset_key,
                    tasks,
                    catalog_version_id=catalog_version_id,
                    mappings=mappings,
                    expected_version_id=expected_version_id,
                    expected_inventory_digest=expected_inventory_digest,
                    created_by=created_by,
                    connection=transaction,
                )
        current = self.current(dataset_key, connection=connection)
        if (current.mapping_version_id if current else None) != expected_version_id:
            raise ValueError("task mapping revision conflict; refresh before applying")
        version_id = f"tm_{content_digest([dataset_key, expected_version_id, preview['snapshot']])[:24]}"
        snapshot = TaskConfigSnapshot.from_dict(preview["snapshot"])
        snapshot = TaskConfigSnapshot(snapshot.catalog, snapshot.mappings, snapshot.inventory, version_id)
        mapping = DatasetTaskMappingVersion(
            version_id,
            dataset_key,
            expected_version_id,
            preview["task_inventory_digest"],
            snapshot.to_dict(),
            datetime.now().astimezone().isoformat(),
            created_by,
        )
        self.repository.put(
            "task_mappings", version_id, mapping.to_dict(), immutable=True, connection=connection
        )
        self.repository.put(
            "task_mapping_heads", dataset_key, {"mapping_version_id": version_id}, connection=connection
        )
        return mapping

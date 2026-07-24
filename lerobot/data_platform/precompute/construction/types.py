from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Scenario:
    name: str
    parsed: dict | None


@dataclass
class ConstructionPlan:
    src_episode_index: int
    new_episode_index: int
    scenario: str
    src_task: str
    new_task: str
    missing_object: str
    src_uncertainty: int
    detected_existing: list[str]
    detected_missing: list[str]
    source_scenario: str | None = None
    source_visual_object: str | None = None
    source_reference_object: str | None = None
    direction: str | None = None
    background: str | None = None
    object_count: int | None = None
    object_count_bucket: str | None = None
    rejected: bool = False
    reject_reason: str | None = None

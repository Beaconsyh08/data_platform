from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path

from lerobot.data_platform.precompute.construction.prompt import make_prompt
from lerobot.data_platform.precompute.construction.scenario import (
    GIVE,
    RELATIVE_PICK,
    SINGLE_PICK,
    UNKNOWN,
    classify_task,
)
from lerobot.data_platform.precompute.construction.types import ConstructionPlan
from lerobot.data_platform.precompute.labeling.review import load_labels_jsonl, uncertainty


def _records_from_labels(labels) -> list[dict]:
    if isinstance(labels, (str, Path)):
        return list(load_labels_jsonl(Path(labels)).values())
    if isinstance(labels, dict):
        return list(labels.values())
    return list(labels or [])


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().lower()


def _has_detection(record: dict, field: str) -> bool:
    if field == "target":
        return record.get("selected") is not None or bool(record.get("detections_target"))
    return bool(record.get("detections_ref"))


def _object_count_from_tags(tags_by_episode: dict[int, dict] | None, episode_index: int) -> int | None:
    if not tags_by_episode:
        return None
    record = tags_by_episode.get(int(episode_index))
    if not record:
        return None
    tags = record.get("tags") or {}
    value = tags.get("object_count")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _background_from_tags(tags_by_episode: dict[int, dict] | None, episode_index: int) -> str | None:
    if not tags_by_episode:
        return None
    record = tags_by_episode.get(int(episode_index))
    if not record:
        return None
    tags = record.get("tags") or {}
    value = tags.get("background")
    if value in (None, ""):
        return None
    value = str(value).strip().lower()
    return value or None


def object_count_bucket(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value == 2:
        return "2"
    if value == 3:
        return "3"
    return "4_plus"


def _detected_existing(record: dict, parsed: dict) -> list[str]:
    out = []
    target = _norm(parsed.get("target"))
    reference = _norm(parsed.get("reference"))
    if target and _has_detection(record, "target"):
        out.append(target)
    if reference and _has_detection(record, "reference"):
        out.append(reference)
    return sorted(set(out))


def _candidate_from_record(
    record: dict,
    vocab: set[str],
    threshold: int,
    tags_by_episode: dict[int, dict] | None = None,
) -> dict | None:
    score = uncertainty(record)
    if score < 0 or score > threshold:
        return None
    scenario = classify_task(record.get("task") or "")
    if scenario.name == UNKNOWN or scenario.parsed is None:
        return None

    existing = _detected_existing(record, scenario.parsed)
    target = _norm(scenario.parsed.get("target"))
    reference = _norm(scenario.parsed.get("reference"))
    direction = _norm(scenario.parsed.get("direction"))
    if not target or target not in existing:
        return None
    if scenario.name == RELATIVE_PICK and (not reference or reference not in existing):
        return None

    missing = sorted(set(vocab) - set(existing))
    if not missing:
        return None
    episode_index = int(record["episode_index"])
    object_count = _object_count_from_tags(tags_by_episode, episode_index)
    background = _background_from_tags(tags_by_episode, episode_index)

    return {
        "record": record,
        "scenario": scenario,
        "uncertainty": score,
        "detected_existing": existing,
        "detected_missing": missing,
        "source_visual_object": target,
        "source_reference_object": reference if reference in existing else None,
        "direction": direction,
        "background": background,
        "object_count": object_count,
        "object_count_bucket": object_count_bucket(object_count),
    }


def summarize_candidates(
    labels,
    vocab: set[str],
    uncertainty_threshold: int,
    tags_by_episode: dict[int, dict] | None = None,
    allow_pick_to_give: bool = False,
) -> dict:
    summary: dict[str, dict] = defaultdict(lambda: {"candidate_count": 0, "missing_distribution": Counter()})
    for record in _records_from_labels(labels):
        candidate = _candidate_from_record(record, vocab, uncertainty_threshold, tags_by_episode)
        if candidate is None:
            continue
        scenario_names = [candidate["scenario"].name]
        if allow_pick_to_give and candidate["scenario"].name == SINGLE_PICK:
            scenario_names.append(GIVE)
        for scenario_name in scenario_names:
            bucket = summary[scenario_name]
            bucket["candidate_count"] += 1
            bucket["missing_distribution"].update(candidate["detected_missing"])
            bucket.setdefault("source_visual_distribution", Counter()).update([candidate["source_visual_object"]])
            bucket.setdefault("source_scenario_distribution", Counter()).update([candidate["scenario"].name])
            if candidate["source_reference_object"]:
                bucket.setdefault("reference_distribution", Counter()).update([candidate["source_reference_object"]])
            if candidate["direction"]:
                bucket.setdefault("direction_distribution", Counter()).update([candidate["direction"]])
            bucket.setdefault("background_distribution", Counter()).update([candidate.get("background") or "unknown"])
            bucket.setdefault("object_count_distribution", Counter()).update([candidate["object_count_bucket"]])

    return {
        scenario: {
            "candidate_count": values["candidate_count"],
            "missing_distribution": dict(sorted(values["missing_distribution"].items())),
            "source_visual_distribution": dict(sorted(values.get("source_visual_distribution", {}).items())),
            "source_scenario_distribution": dict(sorted(values.get("source_scenario_distribution", {}).items())),
            "reference_distribution": dict(sorted(values.get("reference_distribution", {}).items())),
            "direction_distribution": dict(sorted(values.get("direction_distribution", {}).items())),
            "background_distribution": dict(sorted(values.get("background_distribution", {}).items())),
            "object_count_distribution": dict(sorted(values.get("object_count_distribution", {}).items())),
        }
        for scenario, values in sorted(summary.items())
    }


def select_sources(
    labels,
    vocab: set[str],
    uncertainty_threshold: int,
    per_scenario_counts: dict[str, int],
    oversample_factor: float = 1.0,
    tags_by_episode: dict[int, dict] | None = None,
    allow_pick_to_give: bool = False,
) -> list[ConstructionPlan]:
    candidates_by_scenario: dict[str, list[dict]] = defaultdict(list)
    for record in _records_from_labels(labels):
        candidate = _candidate_from_record(record, vocab, uncertainty_threshold, tags_by_episode)
        if candidate is not None:
            candidates_by_scenario[candidate["scenario"].name].append(candidate)
            if allow_pick_to_give and candidate["scenario"].name == SINGLE_PICK:
                candidates_by_scenario[GIVE].append(candidate)

    plans: list[ConstructionPlan] = []
    for scenario_name, requested in per_scenario_counts.items():
        requested = int(requested or 0)
        if requested <= 0:
            continue
        target_count = max(requested, int(math.ceil(requested * max(1.0, float(oversample_factor or 1.0)))))
        candidates = sorted(
            candidates_by_scenario.get(scenario_name, []),
            key=lambda item: (item["uncertainty"], int(item["record"]["episode_index"])),
        )
        used_episodes: set[int] = set()
        assigned_counts: Counter[str] = Counter()
        assigned_source_counts: Counter[str] = Counter()
        assigned_reference_counts: Counter[str] = Counter()
        assigned_pair_counts: Counter[tuple[str, str]] = Counter()
        assigned_direction_counts: Counter[str] = Counter()
        assigned_background_counts: Counter[str] = Counter()
        assigned_object_count_counts: Counter[str] = Counter()
        assigned_missing_background_pair_counts: Counter[tuple[str, str]] = Counter()
        assigned_missing_count_pair_counts: Counter[tuple[str, str]] = Counter()

        for _ in range(target_count):
            available = [
                candidate
                for candidate in candidates
                if int(candidate["record"]["episode_index"]) not in used_episodes
            ]
            if not available:
                break

            def option_rank(
                option: tuple[dict, str],
                assigned_counts=assigned_counts,
                assigned_object_count_counts=assigned_object_count_counts,
                assigned_background_counts=assigned_background_counts,
                assigned_missing_count_pair_counts=assigned_missing_count_pair_counts,
                assigned_missing_background_pair_counts=assigned_missing_background_pair_counts,
                assigned_source_counts=assigned_source_counts,
                assigned_reference_counts=assigned_reference_counts,
                assigned_pair_counts=assigned_pair_counts,
                assigned_direction_counts=assigned_direction_counts,
            ):
                candidate, missing_obj = option
                source_obj = candidate["source_visual_object"]
                reference_obj = candidate.get("source_reference_object") or ""
                direction = candidate.get("direction") or ""
                background = candidate.get("background") or "unknown"
                count_bucket = candidate.get("object_count_bucket") or "unknown"
                return (
                    assigned_counts[missing_obj],
                    assigned_object_count_counts[count_bucket],
                    assigned_background_counts[background],
                    assigned_missing_count_pair_counts[(missing_obj, count_bucket)],
                    assigned_missing_background_pair_counts[(missing_obj, background)],
                    assigned_source_counts[source_obj],
                    assigned_reference_counts[reference_obj],
                    assigned_pair_counts[(missing_obj, source_obj)],
                    assigned_direction_counts[direction],
                    candidate["uncertainty"],
                    int(candidate["record"]["episode_index"]),
                    missing_obj,
                )

            candidate, missing_obj = min(
                ((candidate, missing_obj) for candidate in available for missing_obj in candidate["detected_missing"]),
                key=option_rank,
            )
            source_obj = candidate["source_visual_object"]
            reference_obj = candidate.get("source_reference_object") or ""
            direction = candidate.get("direction") or ""
            background = candidate.get("background") or "unknown"
            count_bucket = candidate.get("object_count_bucket") or "unknown"
            assigned_counts[missing_obj] += 1
            assigned_object_count_counts[count_bucket] += 1
            assigned_background_counts[background] += 1
            assigned_missing_count_pair_counts[(missing_obj, count_bucket)] += 1
            assigned_missing_background_pair_counts[(missing_obj, background)] += 1
            assigned_source_counts[source_obj] += 1
            assigned_reference_counts[reference_obj] += 1
            assigned_pair_counts[(missing_obj, source_obj)] += 1
            assigned_direction_counts[direction] += 1
            record = candidate["record"]
            new_task = make_prompt(
                scenario_name,
                missing_obj,
                candidate["scenario"].parsed,
                candidate["detected_existing"],
            )
            plans.append(
                ConstructionPlan(
                    src_episode_index=int(record["episode_index"]),
                    new_episode_index=len(plans),
                    scenario=scenario_name,
                    src_task=str(record.get("task") or ""),
                    new_task=new_task,
                    missing_object=missing_obj,
                    src_uncertainty=int(candidate["uncertainty"]),
                    detected_existing=candidate["detected_existing"],
                    detected_missing=candidate["detected_missing"],
                    source_scenario=candidate["scenario"].name,
                    source_visual_object=source_obj,
                    source_reference_object=candidate["source_reference_object"],
                    direction=candidate["direction"],
                    background=candidate.get("background"),
                    object_count=candidate.get("object_count"),
                    object_count_bucket=count_bucket,
                )
            )
            used_episodes.add(int(record["episode_index"]))

    return plans

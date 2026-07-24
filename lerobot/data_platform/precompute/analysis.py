from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


ANALYSIS_SCHEMA_VERSION = 8

CANONICAL_OBJECTS = ["yellow duck", "brown dog", "orange lion", "green dinosaur"]
CANONICAL_DIRECTIONS = ["left", "right"]
CANONICAL_OBJECT_ALIASES = {
    "dinosaur": "green dinosaur",
    "dinasour": "green dinosaur",
    "green dinasour": "green dinosaur",
}

DURATION_BUCKETS = [
    (0, 5, "0-5s"),
    (5, 10, "5-10s"),
    (10, 15, "10-15s"),
    (15, 20, "15-20s"),
    (20, 30, "20-30s"),
    (30, 45, "30-45s"),
    (45, 60, "45-60s"),
    (60, None, "60s+"),
]

SCENE_LABELS = {
    "pick": "普通 pick",
    "place": "普通 place",
    "directional_pick": "方位 pick",
    "relational_pick": "参照方位 pick",
    "give": "give",
    "unknown": "unknown",
}

_DIRECTION_WORDS = {
    "left",
    "right",
    "front",
    "back",
    "behind",
    "near",
    "next",
    "beside",
    "between",
    "under",
    "above",
    "top",
    "bottom",
    "左",
    "右",
    "前",
    "后",
    "旁边",
    "附近",
    "上",
    "下",
}

_TARGET_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "back",
    "behind",
    "beside",
    "between",
    "blue",
    "bottom",
    "front",
    "from",
    "give",
    "green",
    "hand",
    "into",
    "left",
    "near",
    "next",
    "me",
    "object",
    "of",
    "on",
    "onto",
    "pick",
    "place",
    "put",
    "red",
    "right",
    "take",
    "the",
    "to",
    "top",
    "up",
    "white",
    "with",
    "yellow",
}


def _canonical_object_pattern() -> str:
    return "|".join(re.escape(obj) for obj in sorted(CANONICAL_OBJECTS, key=len, reverse=True))


def _normalize_task_text(task: str) -> str:
    text = re.sub(r"\s+", " ", (task or "").strip().lower())
    return text.replace("dinasour", "dinosaur")


def parse_canonical_task(task: str) -> dict:
    text = _normalize_task_text(task)
    object_pattern = _canonical_object_pattern()
    result = {
        "is_canonical": False,
        "task_type": "unknown",
        "scene": "unknown",
        "scene_label": SCENE_LABELS["unknown"],
        "target": "unknown",
        "reference": None,
        "side": None,
        "canonical_task": "unknown",
    }
    if not text:
        return result

    match = re.fullmatch(rf"give the ({object_pattern}) to me", text)
    if match:
        target = match.group(1)
        return {
            **result,
            "is_canonical": True,
            "task_type": "give",
            "scene": "give",
            "scene_label": SCENE_LABELS["give"],
            "target": target,
            "canonical_task": f"Give the {target} to me",
        }

    match = re.fullmatch(rf"pick up the ({object_pattern}) to the (left|right) of the ({object_pattern})", text)
    if match:
        target, side, reference = match.groups()
        return {
            **result,
            "is_canonical": True,
            "task_type": "relational_pick",
            "scene": "relational_pick",
            "scene_label": SCENE_LABELS["relational_pick"],
            "target": target,
            "reference": reference,
            "side": side,
            "canonical_task": f"Pick up the {target} to the {side} of the {reference}",
        }

    match = re.fullmatch(rf"pick up the ({object_pattern}) on the (left|right) of the ({object_pattern})", text)
    if match:
        target, side, reference = match.groups()
        return {
            **result,
            "is_canonical": False,
            "task_type": "relational_pick",
            "scene": "relational_pick",
            "scene_label": SCENE_LABELS["relational_pick"],
            "target": target,
            "reference": reference,
            "side": side,
            "canonical_task": f"Pick up the {target} to the {side} of the {reference}",
        }

    match = re.fullmatch(rf"pick up the ({object_pattern}) on the (left|right)", text)
    if match:
        target, side = match.groups()
        return {
            **result,
            "is_canonical": True,
            "task_type": "directional_pick",
            "scene": "directional_pick",
            "scene_label": SCENE_LABELS["directional_pick"],
            "target": target,
            "side": side,
            "canonical_task": f"Pick up the {target} on the {side}",
        }

    match = re.fullmatch(rf"pick up the ({object_pattern})", text)
    if match:
        target = match.group(1)
        return {
            **result,
            "is_canonical": True,
            "task_type": "pick",
            "scene": "pick",
            "scene_label": SCENE_LABELS["pick"],
            "target": target,
            "canonical_task": f"Pick up the {target}",
        }

    if re.fullmatch(r"(place|put)( the)? object", text):
        return {
            **result,
            "is_canonical": True,
            "task_type": "place",
            "scene": "place",
            "scene_label": SCENE_LABELS["place"],
            "canonical_task": "Place object",
        }

    return result


def _find_known_object(text: str) -> str | None:
    normalized = re.sub(r"[\s_./:-]+", " ", (text or "").strip().lower())
    normalized = normalized.replace("dinasour", "dinosaur")
    for obj in CANONICAL_OBJECTS:
        if obj in normalized:
            return obj
    for alias, obj in CANONICAL_OBJECT_ALIASES.items():
        if alias in normalized:
            return obj
    return None


def infer_task_scene(task: str) -> str:
    canonical = parse_canonical_task(task)
    if canonical["is_canonical"]:
        return canonical["scene"]
    if canonical["scene"] != "unknown":
        return canonical["scene"]

    text = (task or "").lower()
    if any(token in text for token in ("give", "hand over", "pass", "递给", "交给")):
        return "give"
    is_pick = any(token in text for token in ("pick", "grasp", "take", "拿", "抓", "取"))
    if is_pick and any(token in text for token in _DIRECTION_WORDS):
        return "directional_pick"
    if is_pick:
        return "pick"
    if any(token in text for token in ("place", "put", "insert", "drop", "放", "放置")):
        return "place"
    return "unknown"


def _normalize_label(value: str) -> str:
    value = re.sub(r"[\s./:-]+", "_", value.strip().lower())
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "unknown"


def _feature_names(feature: Any) -> list[str]:
    if not isinstance(feature, dict):
        return []
    names = feature.get("names")
    if not names:
        return []
    while isinstance(names, dict) and names:
        names = next(iter(names.values()))
    return [str(name) for name in names] if isinstance(names, list) else []


def _feature_dim(feature: Any) -> int:
    if not isinstance(feature, dict):
        return 0
    shape = feature.get("shape") or []
    if isinstance(shape, int):
        return int(shape)
    return int(shape[0]) if shape else 0


def _exist_label_columns(df: pd.DataFrame, meta: Any) -> tuple[list[str], str]:
    """Return CSV columns representing the exist_label feature.

    CSV generation expands vector features either to explicit names from metadata
    or to generated names such as exist_label_0. Prefer this exact feature and
    only fall back to legacy exist* columns when exist_label is unavailable.
    """
    columns = list(df.columns)
    normalized_by_column = {_normalize_label(column): column for column in columns}
    features = getattr(meta, "features", {}) if meta is not None else {}
    feature = features.get("exist_label") if isinstance(features, dict) else None

    if feature is not None:
        explicit = []
        if "exist_label" in columns:
            explicit.append("exist_label")
        explicit.extend(
            column
            for column in columns
            if column != "exist_label" and _normalize_label(column).startswith("exist_label_")
        )
        if explicit:
            return explicit, "exist_label"

        metadata_names = _feature_names(feature)
        named_columns = [
            normalized_by_column[_normalize_label(name)]
            for name in metadata_names
            if _normalize_label(name) in normalized_by_column
        ]
        if named_columns:
            return named_columns, "exist_label"

        dim = _feature_dim(feature)
        generated = [f"exist_label_{idx}" for idx in range(dim)]
        generated_columns = [column for column in generated if column in columns]
        if generated_columns:
            return generated_columns, "exist_label"

    direct = [column for column in columns if _normalize_label(column) == "exist_label"]
    direct.extend(column for column in columns if _normalize_label(column).startswith("exist_label_"))
    if direct:
        return direct, "exist_label"

    legacy = [column for column in columns if "exist" in column.lower()]
    return legacy, "legacy_exist" if legacy else "missing"


def _exist_label_display_column(column: str, label_columns: list[str]) -> str:
    normalized = _normalize_label(column)
    if len(label_columns) == 1 and normalized in {"exist_label", "exist_label_0"}:
        return "exist_label"
    return column


def _target_from_exist_columns(label_columns: list[str]) -> str | None:
    if not label_columns:
        return None
    known_objects = []
    for column in label_columns:
        known_object = _find_known_object(column)
        if known_object:
            known_objects.append(known_object)
    unique_known = sorted(set(known_objects))
    if len(unique_known) == 1:
        return unique_known[0]
    if len(unique_known) > 1:
        return None

    cleaned = []
    for column in label_columns:
        value = re.sub(r"(^|_)exists?(_|$)", "_", column.lower())
        value = re.sub(r"(^|_)exist_label(_|$)", "_", value)
        value = re.sub(r"(^|_)is(_|$)", "_", value)
        value = _normalize_label(value)
        if value and value != "unknown":
            cleaned.append(value)
    if not cleaned:
        return None
    unique_cleaned = sorted(set(cleaned))
    return unique_cleaned[0] if len(unique_cleaned) == 1 else None


def _target_from_exist_counts(exist_counts: dict[str, dict[str, int]]) -> str | None:
    object_true_counts: Counter = Counter()
    for column, counts in exist_counts.items():
        known_object = _find_known_object(column)
        if known_object:
            object_true_counts[known_object] += int(counts.get("true", 0))
    positives = [(obj, count) for obj, count in object_true_counts.items() if count > 0]
    if len(positives) == 1:
        return positives[0][0]
    if len(positives) > 1:
        positives = sorted(positives, key=lambda item: item[1], reverse=True)
        if positives[0][1] > positives[1][1]:
            return positives[0][0]
    return None


def _positive_known_objects(exist_counts: dict[str, dict[str, int]]) -> list[str]:
    objects = []
    for column, counts in exist_counts.items():
        known_object = _find_known_object(column)
        if known_object and int(counts.get("true", 0)) > 0:
            objects.append(known_object)
    return sorted(set(objects))


def infer_target_object(task: str, exist_label_columns: list[str] | None = None) -> str:
    canonical = parse_canonical_task(task)
    if canonical["target"] != "unknown":
        return canonical["target"]

    text = (task or "").lower()
    known_object = _find_known_object(text)
    if known_object:
        return known_object

    from_exist = _target_from_exist_columns(exist_label_columns or [])
    if from_exist:
        return from_exist

    text = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff ]+", " ", text)
    tokens = [token for token in text.split() if token]
    if not tokens:
        return "unknown"

    anchors = ("pick", "grasp", "take", "place", "put", "give", "拿", "抓", "取", "放")
    start = 0
    for idx, token in enumerate(tokens):
        if token in anchors:
            start = idx + 1
            break
    candidates = []
    for token in tokens[start:]:
        if token in _TARGET_STOPWORDS or token in _DIRECTION_WORDS:
            continue
        if token.isdigit():
            continue
        candidates.append(token)
        if len(candidates) >= 2:
            break
    return _normalize_label(" ".join(candidates)) if candidates else "unknown"


def find_cached_episode_csv(static_dir: Path, episode_id: int) -> Path | None:
    csv_dir = static_dir / "csv"
    if not csv_dir.is_dir():
        return None
    candidates = list(csv_dir.glob(f"episode_{episode_id:06d}_ds*.csv"))
    if not candidates:
        return None

    def ds_value(path: Path) -> int:
        match = re.search(r"_ds(\d+)\.csv$", path.name)
        return int(match.group(1)) if match else 10**9

    return min(candidates, key=ds_value)


def _read_tasks(meta: Any, episode_id: int) -> list[str]:
    if not hasattr(meta, "episodes"):
        return []
    episode_info = meta.episodes.get(episode_id)
    if episode_info is None:
        episode_info = meta.episodes.get(str(episode_id), {})
    tasks = episode_info.get("tasks", []) if episode_info else []
    return [str(task) for task in tasks if task]


def _episode_ids(meta: Any, episodes: list[int] | None = None) -> list[int]:
    if episodes is not None:
        return sorted({int(ep) for ep in episodes})
    total = int(getattr(meta, "total_episodes", 0) or 0)
    return list(range(total))


def _duration_from_timestamps(df: pd.DataFrame, fps: float | None) -> float:
    if "timestamp" in df.columns and len(df["timestamp"]) > 0:
        timestamps = pd.to_numeric(df["timestamp"], errors="coerce").dropna()
        if len(timestamps) >= 2:
            return float(timestamps.iloc[-1] - timestamps.iloc[0])
    if fps and fps > 0:
        return float(len(df) / fps)
    return 0.0


def _stage_index(value: Any, max_stage: int) -> int | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric):
        return None
    if 0 <= numeric <= 1:
        return int(round(numeric * max_stage))
    return int(round(numeric))


def _truth_value(value: Any) -> str:
    if value is None:
        return "missing"
    try:
        if pd.isna(value):
            return "missing"
    except TypeError:
        pass
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "nan", "none", "null"}:
            return "missing"
        if normalized in {"true", "yes", "y", "1"}:
            return "true"
        if normalized in {"false", "no", "n", "0"}:
            return "false"
    try:
        return "true" if float(value) > 0.5 else "false"
    except (TypeError, ValueError):
        return "true" if bool(value) else "false"


def _counter_items(counter: Counter, total: int | float | None = None) -> list[dict]:
    denominator = float(total if total is not None else sum(counter.values()))
    items = []
    for key, count in sorted(counter.items(), key=lambda item: (-item[1], str(item[0]))):
        percent = (float(count) / denominator * 100.0) if denominator else 0.0
        items.append({"key": str(key), "count": int(count), "percent": percent})
    return items


def _stage_items(counter: Counter, total: int | float | None = None) -> list[dict]:
    denominator = float(total if total is not None else sum(counter.values()))
    items = []
    for key, count in sorted(counter.items(), key=lambda item: int(item[0])):
        percent = (float(count) / denominator * 100.0) if denominator else 0.0
        items.append({"key": str(key), "count": int(count), "percent": percent})
    return items


def _duration_bucket(duration_seconds: float) -> str:
    for low, high, label in DURATION_BUCKETS:
        if duration_seconds >= low and (high is None or duration_seconds < high):
            return label
    return DURATION_BUCKETS[-1][2]


def _review_reasons(
    *,
    canonical: dict,
    scene: str,
    target: str,
    cache_status: str,
    exist_counts: dict[str, dict[str, int]] | None = None,
    has_stage: bool = False,
) -> list[str]:
    reasons = []
    if not canonical["is_canonical"]:
        reasons.append("non_canonical_task_text")
    if scene == "unknown":
        reasons.append("unknown_scene")
    if target == "unknown":
        positive_objects = _positive_known_objects(exist_counts or {})
        if len(positive_objects) > 1:
            reasons.append("ambiguous_exist_labels")
        reasons.append("unknown_object")
    if cache_status == "missing_csv":
        reasons.append("missing_csv")
    if cache_status == "csv_read_error":
        reasons.append("csv_read_error")
    if cache_status != "missing_csv":
        if not has_stage:
            reasons.append("missing_stage")
    return reasons


def build_dataset_analysis(
    dataset_root: Path,
    meta: Any,
    static_dir: Path,
    episodes: list[int] | None = None,
) -> dict:
    """Build a read-only analysis summary from cached episode CSV files."""
    episode_ids = _episode_ids(meta, episodes)
    fps = float(getattr(meta, "fps", 0) or 0)

    scene_counts: Counter = Counter()
    target_counts: Counter = Counter()
    canonical_task_counts: Counter = Counter()
    task_type_counts: Counter = Counter()
    side_counts: Counter = Counter()
    reference_counts: Counter = Counter()
    review_reason_counts: Counter = Counter()
    duration_counts: Counter = Counter()
    stage_counts: Counter = Counter()
    stage_seconds: Counter = Counter()
    scene_stage_counts: dict[str, Counter] = defaultdict(Counter)
    target_stage_counts: dict[str, Counter] = defaultdict(Counter)
    exist_counts: dict[str, Counter] = defaultdict(Counter)
    exist_episode_true: dict[str, int] = defaultdict(int)
    episode_rows = []
    missing_csv = []
    sampled_csv = []
    all_exist_label_columns: set[str] = set()

    for episode_id in episode_ids:
        csv_path = find_cached_episode_csv(static_dir, episode_id)
        tasks = _read_tasks(meta, episode_id)
        task_text = tasks[0] if tasks else ""
        canonical = parse_canonical_task(task_text)
        scene = canonical["scene"] if canonical["is_canonical"] else infer_task_scene(task_text)
        scene_label = canonical["scene_label"] if canonical["is_canonical"] else SCENE_LABELS.get(scene, scene)

        if csv_path is None:
            target = infer_target_object(task_text)
            scene_counts[scene_label] += 1
            target_counts[target] += 1
            canonical_task_counts[canonical["canonical_task"]] += 1
            task_type_counts[canonical["task_type"] if canonical["is_canonical"] else scene] += 1
            if canonical["side"]:
                side_counts[canonical["side"]] += 1
            if canonical["reference"]:
                reference_counts[canonical["reference"]] += 1
            missing_csv.append(episode_id)
            review_reasons = _review_reasons(
                canonical=canonical,
                scene=scene,
                target=target,
                cache_status="missing_csv",
                has_stage=False,
            )
            review_reason_counts.update(review_reasons)
            episode_rows.append(
                {
                    "episode_id": episode_id,
                    "task": task_text,
                    "scene": scene,
                    "scene_label": scene_label,
                    "target": target,
                    "reference": canonical["reference"],
                    "side": canonical["side"],
                    "task_type": canonical["task_type"] if canonical["is_canonical"] else scene,
                    "canonical_task": canonical["canonical_task"],
                    "is_canonical": canonical["is_canonical"],
                    "frames": 0,
                    "duration_seconds": 0.0,
                    "duration_bucket": None,
                    "csv": None,
                    "csv_downsample": None,
                    "cache_status": "missing_csv",
                    "exist_label_source": "missing",
                    "stage_counts": {},
                    "exist_true": {},
                    "exist_counts": {},
                    "review_reasons": review_reasons,
                }
            )
            continue

        try:
            df = pd.read_csv(csv_path)
        except Exception:
            missing_csv.append(episode_id)
            target = infer_target_object(task_text)
            review_reasons = _review_reasons(
                canonical=canonical,
                scene=scene,
                target=target,
                cache_status="csv_read_error",
                has_stage=False,
            )
            review_reason_counts.update(review_reasons)
            episode_rows.append(
                {
                    "episode_id": episode_id,
                    "task": task_text,
                    "scene": scene,
                    "scene_label": scene_label,
                    "target": target,
                    "reference": canonical["reference"],
                    "side": canonical["side"],
                    "task_type": canonical["task_type"] if canonical["is_canonical"] else scene,
                    "canonical_task": canonical["canonical_task"],
                    "is_canonical": canonical["is_canonical"],
                    "frames": 0,
                    "duration_seconds": 0.0,
                    "duration_bucket": None,
                    "csv": str(csv_path.relative_to(static_dir)),
                    "csv_downsample": None,
                    "cache_status": "csv_read_error",
                    "exist_label_source": "missing",
                    "stage_counts": {},
                    "exist_true": {},
                    "exist_counts": {},
                    "review_reasons": review_reasons,
                }
            )
            continue

        exist_label_columns, exist_label_source = _exist_label_columns(df, meta)
        all_exist_label_columns.update(
            _exist_label_display_column(column, exist_label_columns) for column in exist_label_columns
        )
        duration_seconds = _duration_from_timestamps(df, fps)
        duration_bucket = _duration_bucket(duration_seconds)
        duration_counts[duration_bucket] += 1
        frame_count = int(len(df))
        seconds_per_frame = duration_seconds / frame_count if frame_count else 0.0

        ds_match = re.search(r"_ds(\d+)\.csv$", csv_path.name)
        csv_downsample = int(ds_match.group(1)) if ds_match else None
        if csv_downsample and csv_downsample > 1:
            sampled_csv.append(episode_id)

        max_stage = 5 if scene == "give" else 4
        ep_stage_counts: Counter = Counter()
        stage_column = "stage" if "stage" in df.columns else None
        if stage_column is not None:
            for value in df[stage_column]:
                stage = _stage_index(value, max_stage)
                if stage is None:
                    continue
                stage_key = str(stage)
                stage_counts[stage_key] += 1
                ep_stage_counts[stage_key] += 1
                scene_stage_counts[scene_label][stage_key] += 1
                stage_seconds[stage_key] += seconds_per_frame

        ep_exist_true = {}
        ep_exist_counts = {}
        for column in exist_label_columns:
            values = df[column].map(_truth_value)
            counts = Counter(values)
            display_column = _exist_label_display_column(column, exist_label_columns)
            exist_counts[display_column].update(counts)
            true_count = counts.get("true", 0)
            ep_exist_true[display_column] = int(true_count)
            ep_exist_counts[display_column] = {
                "true": int(counts.get("true", 0)),
                "false": int(counts.get("false", 0)),
                "missing": int(counts.get("missing", 0)),
            }
            if true_count > 0:
                exist_episode_true[display_column] += 1

        target = infer_target_object(task_text, exist_label_columns)
        if target == "unknown":
            target = _target_from_exist_counts(ep_exist_counts) or target
        review_reasons = _review_reasons(
            canonical=canonical,
            scene=scene,
            target=target,
            cache_status="sampled" if csv_downsample and csv_downsample > 1 else "cached",
            exist_counts=ep_exist_counts,
            has_stage=bool(ep_stage_counts),
        )
        review_reason_counts.update(review_reasons)
        scene_counts[scene_label] += 1
        target_counts[target] += 1
        canonical_task_counts[canonical["canonical_task"]] += 1
        task_type_counts[canonical["task_type"] if canonical["is_canonical"] else scene] += 1
        if canonical["side"]:
            side_counts[canonical["side"]] += 1
        if canonical["reference"]:
            reference_counts[canonical["reference"]] += 1
        for stage_key, count in ep_stage_counts.items():
            target_stage_counts[target][stage_key] += count

        episode_rows.append(
            {
                "episode_id": episode_id,
                "task": task_text,
                "scene": scene,
                "scene_label": scene_label,
                "target": target,
                "reference": canonical["reference"],
                "side": canonical["side"],
                "task_type": canonical["task_type"] if canonical["is_canonical"] else scene,
                "canonical_task": canonical["canonical_task"],
                "is_canonical": canonical["is_canonical"],
                "frames": frame_count,
                "duration_seconds": duration_seconds,
                "duration_bucket": duration_bucket,
                "csv": str(csv_path.relative_to(static_dir)),
                "csv_downsample": csv_downsample,
                "cache_status": "sampled" if csv_downsample and csv_downsample > 1 else "cached",
                "exist_label_source": exist_label_source,
                "stage_counts": dict(ep_stage_counts),
                "exist_true": ep_exist_true,
                "exist_counts": ep_exist_counts,
                "review_reasons": review_reasons,
            }
        )

    total_episodes = len(episode_ids)
    total_frames = sum(row["frames"] for row in episode_rows)
    total_duration = sum(row["duration_seconds"] for row in episode_rows)
    duration_episode_count = sum(duration_counts.values())
    exist_summary = []
    for column in sorted(all_exist_label_columns):
        counts = exist_counts[column]
        total_values = sum(counts.values())
        exist_summary.append(
            {
                "key": column,
                "true": int(counts.get("true", 0)),
                "false": int(counts.get("false", 0)),
                "missing": int(counts.get("missing", 0)),
                "true_percent": (counts.get("true", 0) / total_values * 100.0) if total_values else 0.0,
                "episode_true": int(exist_episode_true.get(column, 0)),
                "episode_true_percent": (
                    exist_episode_true.get(column, 0) / total_episodes * 100.0
                    if total_episodes
                    else 0.0
                ),
            }
        )

    analysis = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "dataset_root": str(dataset_root),
        "static_dir": str(static_dir),
        "canonical_objects": CANONICAL_OBJECTS,
        "canonical_directions": CANONICAL_DIRECTIONS,
        "duration_buckets": [label for _, _, label in DURATION_BUCKETS],
        "duration_episode_count": int(duration_episode_count),
        "total_episodes": total_episodes,
        "cached_episodes": total_episodes - len(missing_csv),
        "missing_csv_episodes": missing_csv,
        "sampled_csv_episodes": sampled_csv,
        "total_frames": int(total_frames),
        "total_duration_seconds": total_duration,
        "scene_distribution": _counter_items(scene_counts, total_episodes),
        "target_distribution": _counter_items(target_counts, total_episodes),
        "canonical_task_distribution": _counter_items(canonical_task_counts, total_episodes),
        "task_type_distribution": _counter_items(task_type_counts, total_episodes),
        "side_distribution": _counter_items(side_counts),
        "reference_distribution": _counter_items(reference_counts),
        "review_reason_distribution": _counter_items(review_reason_counts),
        "duration_distribution": [
            {
                "key": label,
                "count": int(duration_counts.get(label, 0)),
                "percent": (
                    duration_counts.get(label, 0) / duration_episode_count * 100.0
                    if duration_episode_count
                    else 0.0
                ),
            }
            for _, _, label in DURATION_BUCKETS
        ],
        "stage_distribution": _stage_items(stage_counts, total_frames),
        "stage_seconds": [
            {"key": str(key), "seconds": float(value), "percent": (value / total_duration * 100.0) if total_duration else 0.0}
            for key, value in sorted(stage_seconds.items(), key=lambda item: int(item[0]))
        ],
        "stage_by_scene": {key: _stage_items(value) for key, value in sorted(scene_stage_counts.items())},
        "stage_by_target": {key: _stage_items(value) for key, value in sorted(target_stage_counts.items())},
        "exist_distribution": exist_summary,
        "episodes": episode_rows,
    }
    return analysis


def analysis_cache_paths(static_dir: Path) -> tuple[Path, Path]:
    analysis_dir = static_dir / "analysis"
    return analysis_dir / "analysis_summary.json", analysis_dir / "analysis_episodes.json"


def write_analysis_cache(static_dir: Path, analysis: dict) -> None:
    summary_path, episodes_path = analysis_cache_paths(static_dir)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {key: value for key, value in analysis.items() if key != "episodes"}
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    episodes_path.write_text(json.dumps({"episodes": analysis.get("episodes", [])}, indent=2, ensure_ascii=False))


def read_analysis_cache(static_dir: Path) -> dict | None:
    summary_path, episodes_path = analysis_cache_paths(static_dir)
    if not summary_path.is_file() or not episodes_path.is_file():
        return None
    try:
        summary = json.loads(summary_path.read_text())
        episodes = json.loads(episodes_path.read_text()).get("episodes", [])
    except (json.JSONDecodeError, OSError):
        return None
    if summary.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        return None
    summary["episodes"] = episodes
    return summary

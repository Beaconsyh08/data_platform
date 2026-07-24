from __future__ import annotations

import re
from typing import Any

DEFAULT_VLM_MODEL = "qwen3.6-plus"
DEFAULT_SELECTED_TAG_NAMES = [
    "background",
    "background_color",
    "object_count",
    "prompt_action_match",
    "arm",
    "grasp_xy",
]

BACKGROUND_ALIASES = {
    "round_table": "round_table",
    "round": "round_table",
    "circular_table": "round_table",
    "circle_table": "round_table",
    "圆桌": "round_table",
    "square_table": "square_table",
    "square": "square_table",
    "方桌": "square_table",
    "long_table": "square_table",
    "rectangular_table": "square_table",
    "rectangle_table": "square_table",
    "dining_table": "square_table",
    "长桌": "square_table",
    "tv_cabinet": "tv_cabinet",
    "tv_stand": "tv_cabinet",
    "television_cabinet": "tv_cabinet",
    "media_console": "tv_cabinet",
    "电视柜": "tv_cabinet",
    "sofa": "sofa",
    "couch": "sofa",
    "沙发": "sofa",
}

TAG_DEFS = [
    {
        "name": "background",
        "dtype": "enum",
        "backend": "vlm",
        "options": ["round_table", "square_table", "tv_cabinet", "sofa"],
        "allow_empty": True,
        "prompt": (
            "Classify the dominant background furniture/supporting surface containing the task objects. "
            "Use one of: round_table, square_table, tv_cabinet, sofa."
        ),
    },
    {
        "name": "background_color",
        "dtype": "str",
        "backend": "vlm",
        "allow_empty": True,
        "prompt": "Describe the dominant color of the selected background furniture/supporting surface.",
    },
    {
        "name": "object_count",
        "dtype": "int",
        "backend": "vlm",
        "prompt": "Count visible task-relevant toy/manipulation objects. Exclude robot arms, hands, and background furniture.",
    },
    {
        "name": "prompt_action_match",
        "dtype": "enum",
        "backend": "vlm",
        "options": ["match", "mismatch", "unclear"],
        "allow_empty": True,
        "prompt": (
            "Check whether the final manipulated/grasped object matches the task prompt. "
            "Use mismatch when the robot appears to manipulate a different object."
        ),
    },
    {
        "name": "arm",
        "dtype": "enum",
        "backend": "rule",
        "options": ["left", "right", "both", "unclear"],
    },
    {
        "name": "grasp_xy",
        "dtype": "float[2]",
        "backend": "geometric",
    },
]

TAG_BY_NAME = {tag["name"]: tag for tag in TAG_DEFS}


def get_schema() -> list[dict]:
    return [dict(tag) for tag in TAG_DEFS]


def selected_tag_defs(tag_names: list[str] | None) -> list[dict]:
    if not tag_names:
        return get_schema()
    out = []
    for name in tag_names:
        if name not in TAG_BY_NAME:
            raise ValueError(f"Unknown tag: {name}")
        out.append(dict(TAG_BY_NAME[name]))
    return out


def _label_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def normalize_background_label(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("type") or value.get("name") or value.get("label")
    text = str(value).strip().lower()
    if not text or text in {"null", "none", "unknown", "unclear", "n/a"}:
        return None
    key = _label_key(text)
    return BACKGROUND_ALIASES.get(text) or BACKGROUND_ALIASES.get(key) or key


def normalize_prompt_action_match(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("prompt_action_match") or value.get("match") or value.get("status") or value.get("result")
    text = str(value).strip().lower()
    if not text or text in {"null", "none", "unknown", "n/a"}:
        return None
    key = _label_key(text)
    if key in {"match", "matched", "correct", "yes", "true", "consistent", "same"}:
        return "match"
    if key in {"mismatch", "mismatched", "incorrect", "wrong", "no", "false", "inconsistent", "different"}:
        return "mismatch"
    if "mismatch" in key or "different" in key or "wrong" in key:
        return "mismatch"
    if "match" in key or "correct" in key or "same" in key:
        return "match"
    return "unclear"


def normalize_tag_values(tags: dict | None) -> dict:
    normalized = dict(tags or {})
    if "background" in normalized:
        normalized["background"] = normalize_background_label(normalized.get("background"))
    if "prompt_action_match" in normalized:
        normalized["prompt_action_match"] = normalize_prompt_action_match(normalized.get("prompt_action_match"))
    return normalized

from __future__ import annotations

import re


SYNONYMS = {
    "brown dog": ["dog", "grey dog", "gray dog", "beige bear", "light brown bear", "brown bear"],
}

_OBJECT_ALIAS_TO_CANONICAL = {
    alias: canonical
    for canonical, aliases in SYNONYMS.items()
    for alias in [canonical, *aliases]
}

PATTERNS = [
    (r"^Give (?:the )?(.+?) to me$", "give"),
    (r"^Give me (?:the )?(.+?)$", "give"),
    (r"^Pick up (?:the )?(.+?) to the (left|right) of (?:the )?(.+?)$", "relative"),
    (r"^Pick up (?:the )?(.+?) on the (left|right) of (?:the )?(.+?)$", "relative"),
    (r"^Pick up (?:the )?(.+?) on the (left|right)$", "absolute"),
    (r"^Pick up (?:the )?(.+?) to the (left|right)$", "absolute"),
    (r"^Pick up (?:the )?(.+?)$", "single"),
]


def expand_prompts(noun: str) -> list[str]:
    return SYNONYMS.get(noun, [noun])


def normalize_object_name(name: str | None) -> str | None:
    if name is None:
        return None
    normalized = re.sub(r"\s+", " ", str(name).strip().lower())
    if not normalized or normalized in {"none", "null", "n/a"}:
        return None
    return _OBJECT_ALIAS_TO_CANONICAL.get(normalized, normalized)


def _fix_typos(name: str) -> str:
    return name.replace("dinasour", "dinosaur")


def parse_task(task_str: str) -> dict | None:
    """Parse supported Pick-up tasks into target, direction, and reference fields."""
    task = task_str.strip().rstrip(".")
    for pattern, kind in PATTERNS:
        match = re.match(pattern, task, re.IGNORECASE)
        if not match:
            continue

        target = _fix_typos(match.group(1).strip())
        if kind == "give":
            return {
                "action": "give",
                "target": target,
                "direction": None,
                "reference": None,
            }
        if kind == "relative":
            return {
                "target": target,
                "direction": match.group(2).lower(),
                "reference": _fix_typos(match.group(3).strip()),
            }
        if kind == "absolute":
            return {
                "target": target,
                "direction": match.group(2).lower(),
                "reference": None,
            }
        return {"target": target, "direction": None, "reference": None}
    return None

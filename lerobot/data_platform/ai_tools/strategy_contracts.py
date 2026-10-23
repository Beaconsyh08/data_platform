"""Closed, typed recipe language and objective promotion gates."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from lerobot.data_platform.operators import get_operator
from lerobot.data_platform.operators.schema import parameter_schema
from lerobot.data_platform.task_catalog import content_digest

OPERATORS = (
    "curation.snapshot",
    "preprocess.split",
    "preprocess.convert_v3",
    "curation.quality",
    "viewer.prepare",
)
PARAMETERS = {
    "curation.snapshot": set(),
    "preprocess.split": {"episode_range"},
    "preprocess.convert_v3": set(),
    "curation.quality": {"checks", "episodes", "video_mode"},
    "viewer.prepare": {"prepare_videos", "prepare_csv", "downsample", "max_frames"},
}
FEATURES = {"action", "observation.state", "timestamp", "episode_index", "frame_index", "index", "task_index"}


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("Unexpected strategy fields")


@dataclass(frozen=True)
class RecipeStep:
    operator_id: str
    version: str
    input: int | str
    artifacts: tuple[int, ...]
    parameters: dict[str, str]


@dataclass(frozen=True)
class Recipe:
    formats: tuple[str, ...]
    required_features: tuple[str, ...]
    steps: tuple[RecipeStep, ...]

    def to_dict(self):
        return asdict(self)


def validate_strategy(value):
    """No natural-language payload, literals, paths, code, or privilege modifiers."""
    exact(value, {"schema_version", "templates"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported strategy schema")
    templates = value["templates"]
    if not isinstance(templates, list) or not 1 <= len(templates) <= 8:
        raise ValueError("A strategy needs 1–8 templates")
    recipes = []
    for template in templates:
        exact(template, {"formats", "required_features", "steps"})
        formats, features, rows = template["formats"], template["required_features"], template["steps"]
        if not isinstance(formats, list) or not formats or any(f not in {"v2.1", "v3.0"} for f in formats):
            raise ValueError("Unsupported formats")
        if not isinstance(features, list) or any(f not in FEATURES for f in features):
            raise ValueError("Unsupported feature condition")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
            raise ValueError("A recipe needs 1–12 steps")
        steps = []
        for i, row in enumerate(rows):
            exact(row, {"operator_id", "version", "input", "artifacts", "parameters"})
            if row["operator_id"] not in OPERATORS:
                raise ValueError("Operator is outside the experiment allowlist")
            spec = get_operator(row["operator_id"], row["version"])
            source = row["input"]
            if source != "source" and (
                type(source) is not int
                or not 0 <= source < i
                or get_operator(steps[source].operator_id).output_type != "dataset"
            ):
                raise ValueError("Input must be source or an earlier dataset")
            artifacts = row["artifacts"]
            if not isinstance(artifacts, list) or any(
                type(n) is not int or not 0 <= n < i for n in artifacts
            ):
                raise ValueError("Evidence must precede its consumer")
            provided = {
                kind
                for n in artifacts
                if steps[n].input == source
                for kind in get_operator(steps[n].operator_id).provides
            }
            if not set(spec.requires) <= provided:
                raise ValueError("Missing compatible evidence")
            parameters = row["parameters"]
            allowed = PARAMETERS[spec.operator_id] & set(parameter_schema(spec)["properties"])
            if (
                not isinstance(parameters, dict)
                or set(parameters) - allowed
                or any(placeholder != "$request." + name for name, placeholder in parameters.items())
            ):
                raise ValueError("Only explicit request parameter placeholders are allowed")
            steps.append(
                RecipeStep(spec.operator_id, spec.version, source, tuple(artifacts), dict(parameters))
            )
        recipes.append(Recipe(tuple(sorted(set(formats))), tuple(sorted(set(features))), tuple(steps)))
    # JSON-normalize tuples; this is also the sole canonical digest representation.
    import json

    return json.loads(json.dumps({"schema_version": 1, "templates": [r.to_dict() for r in recipes]}))


def compatibility(model):
    import hashlib
    from pathlib import Path

    from lerobot.data_platform.ai_tools.service import SYSTEM

    root = Path(__file__).resolve().parents[1]
    implementation = content_digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "ai_tools/service.py",
                "ai_tools/strategies.py",
                "ai_tools/strategy_contracts.py",
                "ai_tools/strategy_evaluation.py",
                "ai_tools/strategy_worker.py",
                "processing/planner.py",
                "operators/builtins.py",
            )
        }
    )
    return {
        "implementation": implementation,
        "generator": 1,
        "model": model,
        "prompt": content_digest(SYSTEM),
        "operators": {name: get_operator(name).version for name in OPERATORS},
    }


@dataclass(frozen=True)
class Measurement:
    case_id: str
    repeat: int
    correct: bool
    calls: int
    tokens: int
    seconds: float
    safe: bool = True


def promotion_gate(baseline, candidate):
    """Paired measurements, not a model's opinion, decide eligibility."""

    def valid(rows):
        return (
            len(rows) == 40
            and len({(r.case_id, r.repeat) for r in rows}) == 40
            and all(
                type(r.correct) is bool
                and type(r.safe) is bool
                and r.safe
                and type(r.calls) is int
                and r.calls > 0
                and type(r.tokens) is int
                and r.tokens > 0
                and math.isfinite(r.seconds)
                and r.seconds >= 0
                and r.repeat in {0, 1}
                for r in rows
            )
            and len({r.case_id for r in rows}) == 20
        )

    if not valid(baseline) or not valid(candidate):
        return {"eligible": False, "reason": "incomplete_or_unsafe"}
    before = {(r.case_id, r.repeat): r for r in baseline}
    after = {(r.case_id, r.repeat): r for r in candidate}
    if set(before) != set(after):
        return {"eligible": False, "reason": "unpaired_cases"}
    stable_regression = any(
        all(before[(c, n)].correct and not after[(c, n)].correct for n in (0, 1)) for c, _ in before
    )

    def metrics(rows):
        return {
            "correct": sum(r.correct for r in rows),
            "denominator": len(rows),
            "calls": sum(r.calls for r in rows) / len(rows),
            "tokens": sum(r.tokens for r in rows) / len(rows),
            "p95_seconds": sorted(r.seconds for r in rows)[math.ceil(len(rows) * 0.95) - 1],
        }

    b, c = metrics(baseline), metrics(candidate)
    improvement = c["correct"] >= b["correct"] + 2 or (
        c["correct"] == b["correct"] and c["calls"] <= b["calls"] * 0.85
    )
    eligible = (
        not stable_regression
        and improvement
        and c["tokens"] <= b["tokens"] * 1.1
        and c["p95_seconds"] <= max(0.001, b["p95_seconds"]) * 1.1
    )
    return {
        "eligible": eligible,
        "reason": "passed" if eligible else "no_verified_improvement",
        "baseline": b,
        "candidate": c,
        "stable_regression": stable_regression,
    }

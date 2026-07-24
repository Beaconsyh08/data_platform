from __future__ import annotations

from collections import Counter
from pathlib import Path

from lerobot.data_platform.precompute.construction.vocab import build_vocab
from lerobot.data_platform.precompute.labeling.task_parser import parse_task
from lerobot.data_platform.precompute.tagging.review import current_tags


def vocab_venn(meta_a, meta_b) -> dict:
    a = set(build_vocab(meta_a))
    b = set(build_vocab(meta_b))
    return {
        "a_only": sorted(a - b),
        "b_only": sorted(b - a),
        "both": sorted(a & b),
        "a_count": len(a),
        "b_count": len(b),
    }


def scenario_distribution(meta) -> dict:
    counts = Counter()
    for task in getattr(meta, "tasks", {}).values():
        parsed = parse_task(task)
        if parsed is None:
            counts["unknown"] += 1
        elif parsed.get("action") == "give":
            counts["give"] += 1
        elif parsed.get("reference"):
            counts["relative_pick"] += 1
        elif parsed.get("direction"):
            counts["directional_pick"] += 1
        elif parsed.get("target"):
            counts["single_pick"] += 1
        else:
            counts["unknown"] += 1
    return dict(sorted(counts.items()))


def tag_distribution(static_a: Path, static_b: Path) -> dict:
    tags_a = current_tags(Path(static_a) / "tagging")
    tags_b = current_tags(Path(static_b) / "tagging")
    names = sorted({name for record in tags_a.values() for name in (record.get("tags") or {})} | {name for record in tags_b.values() for name in (record.get("tags") or {})})
    out = {}
    for name in names:
        ca = Counter(str((record.get("tags") or {}).get(name)) for record in tags_a.values())
        cb = Counter(str((record.get("tags") or {}).get(name)) for record in tags_b.values())
        out[name] = {"a": dict(sorted(ca.items())), "b": dict(sorted(cb.items()))}
    return {"available": bool(tags_a and tags_b), "tags": out}


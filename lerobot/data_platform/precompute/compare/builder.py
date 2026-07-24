from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lerobot.data_platform.precompute.compare.overlap import (
    scenario_distribution,
    tag_distribution,
    vocab_venn,
)
from lerobot.data_platform.precompute.compare.stats import action_stats, metadata_stats
from lerobot.data_platform.precompute.compare.visual import visual_samples
from lerobot.data_platform.precompute.embedding.review import load_points


def compare_cache_dir(static_dir_a: Path, repo_id_b: str) -> Path:
    digest = hashlib.sha1(repo_id_b.encode()).hexdigest()[:12]
    return Path(static_dir_a) / "compare" / digest


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def build_compare_cache(
    *,
    root_a: Path,
    meta_a,
    static_a: Path,
    repo_id_a: str,
    root_b: Path,
    meta_b,
    static_b: Path,
    repo_id_b: str,
    progress_callback=None,
) -> Path:
    out_dir = compare_cache_dir(static_a, repo_id_b)
    out_dir.mkdir(parents=True, exist_ok=True)
    if progress_callback:
        progress_callback({"status": "running", "current": 0, "total": 4, "message": "Building metadata compare"})
    image_keys_a = [key for key, ft in getattr(meta_a, "features", {}).items() if ft.get("dtype") == "image"]
    image_keys_b = [key for key, ft in getattr(meta_b, "features", {}).items() if ft.get("dtype") == "image"]
    stats = {
        "metadata": {"a": metadata_stats(root_a, meta_a), "b": metadata_stats(root_b, meta_b)},
        "action": {"a": action_stats(root_a, meta_a), "b": action_stats(root_b, meta_b)},
    }
    _write_json(out_dir / "stats.json", stats)

    if progress_callback:
        progress_callback({"status": "running", "current": 1, "total": 4, "message": "Building overlap compare"})
    overlap = {
        "vocab": vocab_venn(meta_a, meta_b),
        "scenarios": {"a": scenario_distribution(meta_a), "b": scenario_distribution(meta_b)},
        "tags": tag_distribution(static_a, static_b),
    }
    _write_json(out_dir / "overlap.json", overlap)

    if progress_callback:
        progress_callback({"status": "running", "current": 2, "total": 4, "message": "Building visual samples"})
    visual = {
        "a": visual_samples(repo_id_a, meta_a, image_keys_a),
        "b": visual_samples(repo_id_b, meta_b, image_keys_b),
    }
    _write_json(out_dir / "visual.json", visual)

    if progress_callback:
        progress_callback({"status": "running", "current": 3, "total": 4, "message": "Building embedding compare"})
    points_a = load_points(static_a, meta_a)
    points_b = load_points(static_b, meta_b)
    embedding = {"available": bool(points_a and points_b), "a_count": len(points_a), "b_count": len(points_b)}

    summary = {
        "repo_id_a": repo_id_a,
        "repo_id_b": repo_id_b,
        "ready": True,
        "panels": {
            "metadata": {"available": True},
            "action": {"available": stats["action"]["a"].get("available") and stats["action"]["b"].get("available")},
            "tag": {"available": overlap["tags"]["available"]},
            "embedding": embedding,
            "visual": {"available": bool(visual["a"] and visual["b"])},
        },
    }
    _write_json(out_dir / "summary.json", summary)
    _write_json(
        out_dir / "source.json",
        {"repo_id_a": repo_id_a, "root_a": str(root_a), "repo_id_b": repo_id_b, "root_b": str(root_b)},
    )
    if progress_callback:
        progress_callback({"status": "done", "current": 4, "total": 4, "message": "Compare cache complete"})
    return out_dir


def load_compare_json(static_a: Path, repo_id_b: str, name: str) -> dict:
    path = compare_cache_dir(static_a, repo_id_b) / f"{name}.json"
    if not path.is_file():
        return {"ready": False}
    return json.loads(path.read_text())


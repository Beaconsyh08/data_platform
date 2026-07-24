from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from lerobot.data_platform.precompute.analysis import SCENE_LABELS, infer_task_scene, parse_canonical_task
from lerobot.data_platform.precompute.tagging.review import current_tags


def embedding_dir(static_dir: Path) -> Path:
    return Path(static_dir) / "embedding"


def load_points(static_dir: Path, meta=None) -> list[dict]:
    emb_dir = embedding_dir(static_dir)
    coords_path = emb_dir / "coords_2d.npz"
    if not coords_path.is_file():
        return []
    data = np.load(coords_path)
    episode_indices = data["episode_index"].astype(int).tolist()
    coords = data["coords"].astype(float)
    tags = current_tags(Path(static_dir) / "tagging")
    out = []
    for idx, (episode_index, xy) in enumerate(zip(episode_indices, coords, strict=False)):
        episode = getattr(meta, "episodes", {}).get(episode_index) if meta is not None else None
        task = (episode.get("tasks") or [""])[0] if episode else ""
        canonical = parse_canonical_task(task)
        scene = canonical["scene"] if canonical["is_canonical"] else infer_task_scene(task)
        scene_label = canonical["scene_label"] if canonical["is_canonical"] else SCENE_LABELS.get(scene, scene)
        out.append(
            {
                "episode_index": int(episode_index),
                "x": float(xy[0]),
                "y": float(xy[1]),
                "task": task,
                "task_type": canonical["task_type"] if canonical["is_canonical"] else scene,
                "scene": scene,
                "scene_label": scene_label,
                "tags": (tags.get(int(episode_index)) or {}).get("tags", {}),
                "point_index": idx,
            }
        )
    return out


def load_source(static_dir: Path) -> dict:
    path = embedding_dir(static_dir) / "source.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text())

from __future__ import annotations


def visual_samples(repo_id: str, meta, image_keys: list[str], max_samples: int = 12) -> list[dict]:
    if not image_keys:
        return []
    image_key = image_keys[0]
    out = []
    for episode_index in sorted(getattr(meta, "episodes", {}).keys())[:max_samples]:
        episode = getattr(meta, "episodes", {}).get(episode_index) or {}
        task = (episode.get("tasks") or [""])[0]
        out.append(
            {
                "episode_index": int(episode_index),
                "task": task,
                "image_url": f"/{repo_id}/image?key={image_key}&episode={episode_index}&frame=0",
            }
        )
    return out


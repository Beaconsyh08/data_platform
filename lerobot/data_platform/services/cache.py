# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Viewer cache access, metadata summaries and invalidation."""

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
from lerobot.data_platform.cache_files import (
    cached_csv_files,
    resolve_cached_csv,
)
from lerobot.data_platform.cli import get_default_output_dir
from lerobot.data_platform.precompute.construction.review import load_construction_records
from lerobot.data_platform.precompute.data_profile import (
    dataset_semantics,
    has_body_joint_dimensions,
    resolve_data_profile,
    resolve_processing_profile,
)
from lerobot.data_platform.precompute.embedding import load_source as load_embedding_source
from lerobot.data_platform.precompute.image_io import (
    cached_image_bytes,
    get_parquet_file,
    get_row_group_offsets,
)
from lerobot.data_platform.precompute.labeling.review import (
    available_label_variants,
    labels_path,
    reviewed_path,
)
from lerobot.data_platform.precompute.tagging import (
    available_tag_variants,
    current_tags,
    tags_path,
)
from lerobot.data_platform.precompute.tagging import (
    resolved_reviewed_path as tagging_resolved_reviewed_path,
)
from lerobot.data_platform.precompute.tagging import (
    resolved_tags_path as tagging_resolved_tags_path,
)
from lerobot.data_platform.precompute.tagging import (
    reviewed_path as tagging_reviewed_path,
)
from lerobot.data_platform.precompute.tagging import (
    source_path as tagging_source_path,
)
from lerobot.data_platform.precompute.timeseries import (
    DATA_VERSION_DVT1,
)
from lerobot.data_platform.precompute.viewer_manifest import (
    load_viewer_manifest,
    manifest_episode_ids,
    write_viewer_manifest,
)
from lerobot.data_platform.services.dataset_paths import (
    _find_any_cached_csv,
    _is_dataset_root,
    _normalize_data_version,
    _repo_id_from_key,
    get_columns_info,
)
from lerobot.data_platform.services.state import ConsoleState


@dataclass
class DatasetCacheService:
    state: ConsoleState

    def ttl_get(self, cache: dict, key, builder, ttl: float | None = None):
        now = time.time()
        item = cache.get(key)
        ttl = self.state.status_cache_ttl_s if ttl is None else ttl
        if item is not None and now - item[0] <= ttl:
            return item[1]
        value = builder()
        cache[key] = (now, value)
        return value

    def file_count_signature(self, path: Path) -> tuple[float, int]:
        try:
            stat = Path(path).stat()
            return stat.st_mtime, stat.st_size
        except OSError:
            return 0.0, -1

    def normalize_episode_list(self, values) -> list[int]:
        episodes: set[int] = set()
        for value in values or []:
            try:
                episodes.add(int(value))
            except (TypeError, ValueError):
                continue
        return sorted(episodes)

    def fallback_manifest_from_cache(self, static_dir: Path, repo_id: str) -> dict | None:
        static_dir = Path(static_dir)
        inventory = self.viewer_cache_inventory(static_dir)
        episode_ids: set[int] = set(inventory["episode_ids"])
        image_keys = list(inventory["image_keys"])
        if not episode_ids:
            return None
        episodes = [
            {"episode_index": episode_id, "length": 0, "tasks": []} for episode_id in sorted(episode_ids)
        ]
        return {
            "version": 0,
            "repo_id": repo_id,
            "root": "",
            "data_version": DATA_VERSION_DVT1,
            "fps": 0,
            "total_episodes": len(episodes),
            "total_frames": 0,
            "episodes": episodes,
            "features": {},
            "image_keys": image_keys,
            "video_keys": [],
            "downsample": 1,
        }

    def dir_signature(self, path: Path) -> tuple[str, int, int]:
        path = Path(path)
        try:
            stat = path.stat()
            return str(path), int(stat.st_mtime_ns), int(stat.st_size)
        except OSError:
            return str(path), 0, -1

    def viewer_cache_inventory(self, static_dir: Path) -> dict:
        static_dir = Path(static_dir).expanduser()
        csv_dir = static_dir / "csv"
        videos_dir = static_dir / "videos"
        image_dir_sigs: list[tuple[str, int, int]] = []
        if videos_dir.is_dir():
            try:
                image_dirs = sorted(path for path in videos_dir.iterdir() if path.is_dir())
            except OSError:
                image_dirs = []
            image_dir_sigs = [self.dir_signature(path) for path in image_dirs]
        else:
            image_dirs = []
        signature = (
            self.dir_signature(static_dir),
            self.dir_signature(csv_dir),
            self.dir_signature(videos_dir),
            tuple(image_dir_sigs),
        )
        cached = self.state.viewer_cache_inventory_cache.get(static_dir)
        if cached is not None and cached[0] == signature:
            return cached[1]

        csv_episode_ids: set[int] = set()
        if csv_dir.is_dir():
            for csv_path in cached_csv_files(csv_dir):
                match = re.match(r"episode_(\d+)_ds\d+\.csv(?:\.gz)?$", csv_path.name)
                if match:
                    csv_episode_ids.add(int(match.group(1)))

        video_episode_ids_by_key: dict[str, set[int]] = {}
        for image_dir in image_dirs:
            ids: set[int] = set()
            for video_path in image_dir.glob("episode_*_h264.mp4"):
                match = re.match(r"episode_(\d+)_h264\.mp4$", video_path.name)
                if match:
                    ids.add(int(match.group(1)))
            video_episode_ids_by_key[image_dir.name] = ids

        episode_ids = set(csv_episode_ids)
        for ids in video_episode_ids_by_key.values():
            episode_ids.update(ids)

        inventory = {
            "episode_ids": sorted(episode_ids),
            "csv_episode_ids": sorted(csv_episode_ids),
            "csv_episode_id_set": set(csv_episode_ids),
            "image_keys": sorted(video_episode_ids_by_key),
            "video_episode_ids_by_key": {key: sorted(ids) for key, ids in video_episode_ids_by_key.items()},
        }
        self.state.viewer_cache_inventory_cache[static_dir] = (signature, inventory)
        return inventory

    def root_has_cache_manifest(self, root_path: Path, output_dir: Path) -> bool:
        ds_static = Path(output_dir).expanduser() / "static"
        return (ds_static / "viewer_manifest.json").is_file() or self.fallback_manifest_from_cache(
            ds_static, ""
        ) is not None

    def is_internal_remote_cache_entry(self, entry: dict) -> bool:
        if bool(entry.get("internal_remote_cache", False)):
            return True
        root_path = Path(entry["root"]).expanduser()
        output_dir = Path(entry["output_dir"]).expanduser()
        return (
            root_path.name == "remote_source"
            and not _is_dataset_root(root_path)
            and self.root_has_cache_manifest(root_path, output_dir)
        )

    def static_has_viewer_cache(self, static_dir: Path) -> bool:
        static_dir = Path(static_dir).expanduser()
        if (static_dir / "viewer_manifest.json").is_file():
            return True
        return bool(self.viewer_cache_inventory(static_dir)["episode_ids"])

    def root_has_viewer_cache(self, root_path: Path, output_dir: Path | None = None) -> bool:
        if output_dir is not None:
            return self.static_has_viewer_cache(Path(output_dir).expanduser() / "static")
        return self.cache_only_root_and_output(root_path) is not None

    def cache_only_root_and_output(self, path: Path) -> tuple[Path, Path] | None:
        path = Path(path).expanduser()
        if path.name == "static" and self.static_has_viewer_cache(path):
            output_dir = path.parent
            if output_dir.name == "_console" and output_dir.parent.name == "vis":
                return output_dir.parent.parent, output_dir
            return output_dir, output_dir
        if self.static_has_viewer_cache(path / "static"):
            if path.name == "_console" and path.parent.name == "vis":
                return path.parent.parent, path
            return path, path
        output_dir = get_default_output_dir(path)
        if self.static_has_viewer_cache(output_dir / "static"):
            return path, output_dir
        return None

    def dataset_episode_ids(self, dataset_obj, dataset_key: tuple[str, str]) -> list[int]:
        episode_ids = self.state.episodes_by_key.get(dataset_key)
        if episode_ids is not None:
            return episode_ids
        total_episodes = (
            dataset_obj.num_episodes
            if isinstance(dataset_obj, LeRobotDataset)
            else getattr(dataset_obj, "total_episodes", 0)
        )
        episode_ids = list(range(total_episodes))
        self.state.episodes_by_key[dataset_key] = episode_ids
        return episode_ids

    def viewer_columns_info(self, dataset_obj, dataset_key: tuple[str, str]):
        features = getattr(dataset_obj, "features", {}) or {}
        signature = (
            dataset_key,
            tuple(
                sorted(
                    (
                        str(name),
                        str((feature or {}).get("dtype")),
                        repr((feature or {}).get("shape")),
                        repr((feature or {}).get("names")),
                    )
                    for name, feature in features.items()
                )
            ),
        )
        cached = self.state.columns_info_cache.get(signature)
        if cached is not None:
            return cached
        result = get_columns_info(dataset_obj)
        if len(self.state.columns_info_cache) > 64:
            self.state.columns_info_cache.clear()
        self.state.columns_info_cache[signature] = result
        return result

    def dataset_image_keys(self, dataset_obj) -> list[str]:
        return [
            key
            for key, ft in getattr(dataset_obj, "features", {}).items()
            if ft.get("dtype") in {"image", "video"}
        ]

    def data_profile_for_dataset(self, dataset_obj):
        return resolve_data_profile(
            Path(dataset_obj.root),
            getattr(dataset_obj, "features", {}) or {},
        )

    def ensure_viewer_manifest(
        self, dataset_obj, ds_static: Path, selected_episodes: list[int] | None = None
    ) -> None:
        if (ds_static / "viewer_manifest.json").is_file():
            return
        if not hasattr(dataset_obj, "meta"):
            return
        try:
            videos_dir = ds_static / "videos"
            cache_inventory = {} if videos_dir.is_symlink() else self.viewer_cache_inventory(ds_static)
            cached_episodes = list(cache_inventory.get("episode_ids") or [])
            episodes = selected_episodes or cached_episodes
            if not episodes:
                episodes = sorted(int(ep) for ep in getattr(dataset_obj.meta, "episodes", {}))
            image_keys = list(cache_inventory.get("image_keys") or []) or self.dataset_image_keys(dataset_obj)
            repo_id = str(
                getattr(dataset_obj, "repo_id", None)
                or f"local/{Path(getattr(dataset_obj, 'root', 'dataset')).name or 'dataset'}"
            )
            data_version = resolve_processing_profile(
                Path(dataset_obj.root), getattr(dataset_obj, "features", {}) or {}
            ).legacy_data_version
            write_viewer_manifest(
                root=Path(dataset_obj.root),
                repo_id=repo_id,
                meta=dataset_obj.meta,
                episodes=sorted(int(ep) for ep in episodes),
                image_keys=image_keys,
                static_dir=ds_static,
                data_version=data_version,
                downsample=self.state.server_state.get("downsample"),
            )
            self.state.viewer_cache_inventory_cache.pop(ds_static, None)
        except Exception:
            logging.exception("Could not write viewer manifest to %s", ds_static)

    def ensure_dataset_static(
        self, dataset_obj, ds_static: Path, selected_episodes: list[int] | None = None
    ) -> None:
        ds_static.mkdir(parents=True, exist_ok=True)
        videos_dir = ds_static / "videos"
        if videos_dir.is_symlink() and not videos_dir.exists():
            try:
                videos_dir.unlink()
            except OSError:
                logging.exception("Could not remove broken videos symlink: %s", videos_dir)
        if videos_dir.exists():
            self.ensure_viewer_manifest(dataset_obj, ds_static, selected_episodes)
            return
        if (
            hasattr(dataset_obj, "meta")
            and not getattr(dataset_obj.meta, "is_v3", False)
            and len(getattr(dataset_obj.meta, "video_keys", [])) > 0
        ):
            source_videos = dataset_obj.root / "videos"
            if source_videos.exists():
                videos_dir.symlink_to(source_videos.resolve().as_posix())
                self.ensure_viewer_manifest(dataset_obj, ds_static, selected_episodes)
                return
        videos_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_viewer_manifest(dataset_obj, ds_static, selected_episodes)

    def empty_cache_status(self, status: str = "not_loaded") -> dict:
        return {
            "status": status,
            "videos": {"cached": 0, "total": 0, "status": status},
            "csv": {"cached": 0, "total": 0, "status": status},
        }

    def editable_vector_features(self, features: dict) -> list[dict]:
        result = []
        for key in ("action", "state"):
            feature = (features or {}).get(key)
            if not isinstance(feature, dict):
                continue
            shape = feature.get("shape") or []
            if not shape:
                continue
            try:
                dimension = int(shape[-1])
            except (TypeError, ValueError):
                continue
            if dimension <= 0:
                continue
            raw_names = feature.get("names")
            names = [str(name) for name in raw_names] if isinstance(raw_names, list) else []
            result.append(
                {
                    "key": key,
                    "label": "Action" if key == "action" else "State",
                    "dimension": dimension,
                    "names": names if len(names) == dimension else [],
                }
            )
        return result

    def read_light_dataset_info(self, root_path: Path, output_dir: Path | None = None) -> dict | None:
        info_path = root_path / "meta" / "info.json"
        try:
            info = json.loads(info_path.read_text())
        except (json.JSONDecodeError, OSError):
            if output_dir is None:
                return None
            ds_static = Path(output_dir).expanduser() / "static"
            manifest = load_viewer_manifest(ds_static) or self.fallback_manifest_from_cache(ds_static, "")
            if not manifest:
                return None
            cached_image_keys = list(self.viewer_cache_inventory(ds_static).get("image_keys") or [])
            image_keys = cached_image_keys or list(manifest.get("image_keys") or [])
            manifest_features = manifest.get("features") or {}
            manifest_profile = manifest.get("data_profile") or {}
            return {
                **dataset_semantics(manifest),
                "total_episodes": int(manifest.get("total_episodes") or len(manifest_episode_ids(manifest))),
                "image_keys": image_keys,
                "editable_features": [],
                "data_version": _normalize_data_version(manifest.get("data_version")),
                "robot_profile": str(manifest_profile.get("robot_profile") or ""),
                "signal_schema": str(manifest_profile.get("signal_schema") or ""),
                "stage_profile": str(manifest_profile.get("stage_profile") or ""),
                "gripper_encoding": str(manifest_profile.get("gripper_encoding") or ""),
                "profile_confirmed": bool(manifest_profile.get("confirmed", False)),
                "has_body_joints": has_body_joint_dimensions(manifest_features),
                "dataset_format_version": str(manifest.get("codebase_version") or ""),
            }
        total_episodes = int(info.get("total_episodes") or 0)
        features = info.get("features") or {}
        data_profile = resolve_data_profile(root_path, features)
        dataset_format_version = str(info.get("codebase_version") or "")
        is_v3 = dataset_format_version.lower().removeprefix("v").startswith("3.")
        image_keys = [
            key
            for key, feature in features.items()
            if isinstance(feature, dict)
            and (feature.get("dtype") == "image" or (is_v3 and feature.get("dtype") == "video"))
        ]
        return {
            **dataset_semantics(info, data_profile),
            "total_episodes": total_episodes,
            "image_keys": image_keys,
            "editable_features": self.editable_vector_features(features),
            "merge_signal_features": {
                k: features[k] for k in ("action", "state", "observation.state") if k in features
            },
            "data_version": data_profile.legacy_data_version,
            "robot_profile": data_profile.robot_profile,
            "signal_schema": data_profile.signal_schema,
            "stage_profile": data_profile.stage_profile,
            "gripper_encoding": data_profile.gripper_encoding,
            "profile_confirmed": data_profile.confirmed,
            "has_body_joints": has_body_joint_dimensions(features),
            "dataset_format_version": dataset_format_version,
        }

    def light_episode_ids(self, root_path: Path, output_dir: Path) -> list[int]:
        if _is_dataset_root(root_path):
            info = self.read_light_dataset_info(root_path, output_dir) or {}
            total = int(info.get("total_episodes") or 0)
            return list(range(total))
        manifest = load_viewer_manifest(Path(output_dir).expanduser() / "static")
        if manifest is None:
            manifest = self.fallback_manifest_from_cache(Path(output_dir).expanduser() / "static", "")
        return manifest_episode_ids(manifest) if manifest else []

    def cached_csv_episode_count(self, static_dir: Path, episode_ids: list[int]) -> int:
        allowed = {int(episode_id) for episode_id in episode_ids}
        ids = self.viewer_cache_inventory(static_dir)["csv_episode_ids"]
        return sum(1 for episode_id in ids if int(episode_id) in allowed)

    def cached_video_count(self, static_dir: Path, image_keys: list[str], episode_ids: list[int]) -> int:
        if not image_keys:
            return 0
        allowed = {int(episode_id) for episode_id in episode_ids}
        by_key = self.viewer_cache_inventory(static_dir)["video_episode_ids_by_key"]
        cached = 0
        for image_key in image_keys:
            cached += sum(1 for episode_id in by_key.get(image_key, []) if int(episode_id) in allowed)
        return cached

    def light_cache_status(self, root_path: Path, output_dir: Path) -> dict:
        info = self.read_light_dataset_info(root_path, output_dir)
        if info is None:
            return self.empty_cache_status("unknown")

        episode_ids = self.light_episode_ids(root_path, output_dir)
        total_episodes = len(episode_ids) or int(info["total_episodes"])
        if not episode_ids:
            episode_ids = list(range(total_episodes))
        image_keys = info["image_keys"]
        ds_static = output_dir / "static"
        video_total = total_episodes * len(image_keys)
        video_cached = self.cached_video_count(ds_static, image_keys, episode_ids)
        csv_total = total_episodes
        csv_cached = self.cached_csv_episode_count(ds_static, episode_ids)

        videos_ok = video_total == 0 or video_cached == video_total
        csv_ok = csv_total == 0 or csv_cached == csv_total
        return {
            "status": "cached" if videos_ok and csv_ok else "missing",
            "videos": {
                "cached": video_cached,
                "total": video_total,
                "status": "cached" if videos_ok else "missing",
            },
            "csv": {"cached": csv_cached, "total": csv_total, "status": "cached" if csv_ok else "missing"},
        }

    def cached_light_cache_status(self, root_path: Path, output_dir: Path) -> dict:
        root_path = Path(root_path).expanduser()
        output_dir = Path(output_dir).expanduser()
        return self.ttl_get(
            self.state.light_cache_status_cache,
            (str(root_path), str(output_dir)),
            lambda: self.light_cache_status(root_path, output_dir),
        )

    def invalidate_light_cache_status(self, root_path: Path, output_dir: Path) -> None:
        self.state.light_cache_status_cache.pop(
            (str(Path(root_path).expanduser()), str(Path(output_dir).expanduser())),
            None,
        )
        self.state.viewer_cache_inventory_cache.pop(Path(output_dir).expanduser() / "static", None)

    def clear_episode_dependent_caches(self, dataset_key: tuple[str, str] | None = None) -> None:
        for cached_fn in [get_parquet_file, get_row_group_offsets, cached_image_bytes]:
            if hasattr(cached_fn, "cache_clear"):
                cached_fn.cache_clear()
        if dataset_key is None:
            self.state.task_caches.clear()
        else:
            self.state.task_caches.pop(dataset_key, None)
        self.state.viewer_tag_map_cache.clear()
        self.state.viewer_cache_inventory_cache.clear()
        self.state.issue_episodes_cache.clear()
        self.state.flagged_episodes_cache.clear()
        self.state.annotation_issues_by_episode_cache.clear()
        self.state.flag_sidecar_json_cache.clear()
        self.state.columns_info_cache.clear()
        self.state.construction_status_cache.clear()
        self.state.tagging_status_cache.clear()
        self.state.embedding_count_cache.clear()

    def refresh_dataset_after_episode_delete(
        self,
        dataset_key: tuple[str, str],
        dataset_obj,
        ds_static: Path,
    ) -> list[int]:
        self.clear_episode_dependent_caches(dataset_key)
        self.invalidate_light_cache_status(Path(dataset_obj.root), Path(ds_static).parent)
        remaining_ids = sorted(int(ep) for ep in getattr(dataset_obj.meta, "episodes", {}))
        if not remaining_ids:
            remaining_total = int(getattr(dataset_obj, "total_episodes", 0) or 0)
            remaining_ids = list(range(remaining_total))
        self.state.episodes_by_key[dataset_key] = remaining_ids
        return remaining_ids

    def manifest_or_cache_episode_ids(self, manifest: dict | None, static_dir: Path) -> list[int]:
        if manifest:
            ids = manifest_episode_ids(manifest)
            if ids:
                return ids
        return list(self.viewer_cache_inventory(static_dir)["episode_ids"])

    def cache_status(self, dataset_obj, ds_static: Path, episode_ids: list[int]) -> dict:
        image_keys = self.dataset_image_keys(dataset_obj)
        video_total = len(episode_ids) * len(image_keys)
        video_cached = 0
        for episode_id in episode_ids:
            for image_key in image_keys:
                video_path = ds_static / "videos" / image_key / f"episode_{episode_id:06d}_h264.mp4"
                if video_path.is_file() and video_path.stat().st_size > 0:
                    video_cached += 1

        csv_total = len(episode_ids)
        csv_cached = 0
        csv_dir = ds_static / "csv"
        if csv_dir.is_dir():
            for episode_id in episode_ids:
                if _find_any_cached_csv(csv_dir, episode_id) is not None:
                    csv_cached += 1

        videos_ok = video_total == 0 or video_cached == video_total
        csv_ok = csv_total == 0 or csv_cached == csv_total
        return {
            "status": "cached" if videos_ok and csv_ok else "missing",
            "videos": {
                "cached": video_cached,
                "total": video_total,
                "status": "cached" if videos_ok else "missing",
            },
            "csv": {"cached": csv_cached, "total": csv_total, "status": "cached" if csv_ok else "missing"},
        }

    def episode_cache_status(self, dataset_obj, ds_static: Path, episode_id: int) -> dict:
        """Lightweight cache check for viewer navigation.

        `_cache_status(..., [episode_id])` has to glob `episode_*_ds*.csv`, which
        becomes expensive in large cache directories. For opening a single
        episode we only need a yes/no check, so prefer direct path checks and
        fall back to the cached inventory.
        """
        image_keys = self.dataset_image_keys(dataset_obj)
        video_total = len(image_keys)
        video_cached = 0
        for image_key in image_keys:
            video_path = ds_static / "videos" / image_key / f"episode_{episode_id:06d}_h264.mp4"
            if video_path.is_file() and video_path.stat().st_size > 0:
                video_cached += 1

        csv_dir = ds_static / "csv"
        csv_cached = 0
        ds = (
            self.state.server_state["downsample"]
            if self.state.server_state["downsample"] and self.state.server_state["downsample"] > 1
            else 1
        )
        preferred_csv = resolve_cached_csv(csv_dir / f"episode_{episode_id:06d}_ds{ds}.csv")
        default_csv = resolve_cached_csv(csv_dir / f"episode_{episode_id:06d}_ds1.csv")
        if preferred_csv.is_file() or default_csv.is_file():
            csv_cached = 1
        elif csv_dir.is_dir():
            inventory = self.viewer_cache_inventory(ds_static)
            csv_ids = inventory.get("csv_episode_id_set") or set(inventory.get("csv_episode_ids") or [])
            if int(episode_id) in csv_ids:
                csv_cached = 1

        videos_ok = video_total == 0 or video_cached == video_total
        csv_ok = csv_cached == 1
        return {
            "status": "cached" if videos_ok and csv_ok else "missing",
            "videos": {
                "cached": video_cached,
                "total": video_total,
                "status": "cached" if videos_ok else "missing",
            },
            "csv": {"cached": csv_cached, "total": 1, "status": "cached" if csv_ok else "missing"},
        }

    def count_jsonl(self, path: Path) -> int:
        path = Path(path)
        signature = self.file_count_signature(path)
        cached = self.state.jsonl_count_cache.get(path)
        if cached is not None and cached[1] == signature:
            return cached[2]
        if not path.is_file():
            self.state.jsonl_count_cache[path] = (time.time(), signature, 0)
            return 0
        try:
            with path.open() as f:
                count = sum(1 for line in f if line.strip())
        except OSError:
            return 0
        self.state.jsonl_count_cache[path] = (time.time(), signature, count)
        return count

    def labeling_status(self, dataset_key: tuple[str, str], ds_static: Path) -> dict:
        repo_id = _repo_id_from_key(dataset_key)
        labeling_dir = ds_static / "labeling"
        labels_file = labels_path(labeling_dir)
        reviewed_file = reviewed_path(labeling_dir)
        variants = available_label_variants(labeling_dir)
        active = variants[0] if variants else None
        return {
            "status": "ready" if variants or labels_file.is_file() else "missing",
            "labels_path": str(active["labels_path"]) if active else str(labels_file),
            "reviewed_path": str(active["reviewed_path"]) if active else str(reviewed_file),
            "labels_count": active["labels_count"] if active else self.count_jsonl(labels_file),
            "reviewed_count": active["reviewed_count"] if active else self.count_jsonl(reviewed_file),
            "variants": variants,
            "review_url": f"/{repo_id}/labeling",
            "merge_url": f"/api/labeling/{repo_id}/merge",
        }

    def construction_status(self, dataset_key: tuple[str, str], dataset_obj, ds_static: Path) -> dict:
        def _build() -> dict:
            repo_id = _repo_id_from_key(dataset_key)
            plan_path = (
                Path(dataset_obj.root) / "meta" / "construction_plan.json"
                if hasattr(dataset_obj, "root")
                else None
            )
            records = load_construction_records(dataset_obj.root) if plan_path and plan_path.is_file() else []
            rejected = sum(1 for record in records if record.get("rejected"))
            labels_file = labels_path(ds_static / "labeling")
            return {
                "status": "ready" if records else "missing",
                "object_labeling_ready": labels_file.is_file(),
                "records_count": len(records),
                "rejected_count": rejected,
                "review_url": f"/{repo_id}/construction",
            }

        root = str(Path(getattr(dataset_obj, "root", ds_static)).expanduser())
        return self.ttl_get(
            self.state.construction_status_cache, (_repo_id_from_key(dataset_key), root), _build
        )

    def tagging_status(self, dataset_key: tuple[str, str], ds_static: Path) -> dict:
        def _build() -> dict:
            repo_id = _repo_id_from_key(dataset_key)
            tagging_dir = ds_static / "tagging"
            tags_file = tags_path(tagging_dir)
            reviewed_file = tagging_reviewed_path(tagging_dir)
            heatmap_file = tagging_dir / "grasp_heatmap.png"
            variants = available_tag_variants(tagging_dir)
            active = variants[0] if variants else None
            active_variant = active["id"] if active and active["id"] != "latest" else None
            review_url = f"/{repo_id}/tagging" + (f"?variant={active_variant}" if active_variant else "")
            return {
                "status": "ready" if active else "missing",
                "tags_path": active["tags_path"] if active else str(tags_file),
                "reviewed_path": active["reviewed_path"] if active else str(reviewed_file),
                "tags_count": active["tags_count"] if active else 0,
                "reviewed_count": active["reviewed_count"] if active else 0,
                "variants": variants,
                "review_url": review_url,
                "merge_url": f"/api/tagging/{repo_id}/merge"
                + (f"?variant={active_variant}" if active_variant else ""),
                "heatmap_url": f"/api/tagging/{repo_id}/heatmap" if heatmap_file.is_file() else None,
            }

        return self.ttl_get(
            self.state.tagging_status_cache,
            (_repo_id_from_key(dataset_key), str(Path(ds_static).expanduser())),
            _build,
        )

    def active_tag_variant(self, tagging_dir: Path, variant: str | None = None) -> str | None:
        variant = variant or None
        if variant:
            return variant
        variants = available_tag_variants(tagging_dir)
        if not variants:
            return None
        active_id = variants[0].get("id")
        return None if active_id == "latest" else active_id

    def embedding_status(self, dataset_key: tuple[str, str], ds_static: Path) -> dict:
        repo_id = _repo_id_from_key(dataset_key)
        coords_path = Path(ds_static) / "embedding" / "coords_2d.npz"

        def _count_points() -> int:
            if not coords_path.is_file():
                return 0
            try:
                data = np.load(coords_path)
                try:
                    return int(len(data["episode_index"]))
                finally:
                    close = getattr(data, "close", None)
                    if close is not None:
                        close()
            except Exception:
                return 0

        points_count = self.ttl_get(self.state.embedding_count_cache, str(coords_path), _count_points)
        source = load_embedding_source(ds_static)
        return {
            "status": "ready" if points_count else "missing",
            "points_count": points_count,
            "scatter_url": f"/{repo_id}/embedding",
            "source": source,
        }

    def compare_status(self, ds_static: Path) -> dict:
        compare_dir = ds_static / "compare"
        count = len([path for path in compare_dir.iterdir() if path.is_dir()]) if compare_dir.is_dir() else 0
        return {"status": "ready" if count else "missing", "cache_count": count}

    def load_smoothing_meta(self, root: Path) -> dict | None:
        meta_path = Path(root) / "meta" / "preprocess_smooth_action.json"
        if not meta_path.is_file():
            return None
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def smoothing_created_sort_key(self, root: Path, meta: dict | None) -> str:
        if meta and meta.get("created_at"):
            return str(meta["created_at"])
        try:
            return f"{(Path(root) / 'meta' / 'preprocess_smooth_action.json').stat().st_mtime:.6f}"
        except OSError:
            return ""

    def find_derived_smoothing(self, root: Path) -> dict | None:
        source_root = str(Path(root).expanduser())
        matches = []
        for dataset_key, entry in self.state.datasets_index.items():
            candidate_root = Path(entry["root"]).expanduser()
            meta = self.load_smoothing_meta(candidate_root)
            if not meta:
                continue
            if str(Path(str(meta.get("source_root", ""))).expanduser()) != source_root:
                continue
            candidate_repo_id = _repo_id_from_key(dataset_key)
            matches.append(
                {
                    "repo_id": candidate_repo_id,
                    "root": str(candidate_root),
                    "report_url": f"/{candidate_repo_id}/smoothing",
                    "created_at": meta.get("created_at"),
                    "fields": meta.get("fields") or [meta.get("field") or "action"],
                    "_sort": self.smoothing_created_sort_key(candidate_root, meta),
                }
            )
        if not matches:
            return None
        matches.sort(key=lambda item: item["_sort"], reverse=True)
        latest = dict(matches[0])
        latest.pop("_sort", None)
        return latest

    def viewer_tag_value_key(self, name: str, value) -> str | None:
        if value in (None, ""):
            return None
        if name == "object_count":
            try:
                count = int(value)
            except (TypeError, ValueError):
                return None
            return "4+" if count >= 4 else str(count)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float, str)):
            return str(value)
        return None

    def file_signature(self, path: Path) -> tuple[str, int, int]:
        path = Path(path)
        try:
            stat = path.stat()
            return str(path), int(stat.st_mtime_ns), int(stat.st_size)
        except OSError:
            return str(path), 0, -1

    def fast_active_tag_variant(self, tagging_dir: Path, variant: str | None = None) -> str | None:
        """Pick the active tagging variant without loading/counting JSONL contents."""
        variant = variant or None
        if variant:
            return variant
        tagging_dir = Path(tagging_dir)
        candidates: list[tuple[float, int, str, str | None]] = []

        latest_path = tags_path(tagging_dir)
        if latest_path.is_file():
            try:
                modified_at = latest_path.stat().st_mtime
            except OSError:
                modified_at = 0.0
            candidates.append((modified_at, 0, "latest", None))

        for path in tagging_dir.glob("tags_*.jsonl"):
            if path.name.startswith("tags_reviewed"):
                continue
            variant_id = path.stem.removeprefix("tags_")
            if not variant_id:
                continue
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                modified_at = 0.0
            preferred = 1 if variant_id == "trial" else 99
            candidates.append((modified_at, preferred, variant_id, variant_id))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        return candidates[0][3]

    def viewer_tag_map_signature(self, ds_static: Path, episode_ids: list[int], dataset_obj=None) -> tuple:
        ds_static = Path(ds_static).expanduser()
        tagging_dir = ds_static / "tagging"
        active_variant = self.fast_active_tag_variant(tagging_dir)
        tag_file = tagging_resolved_tags_path(tagging_dir, active_variant)
        reviewed_file = tagging_resolved_reviewed_path(tagging_dir, active_variant)
        source_file = tagging_source_path(tagging_dir, active_variant)
        try:
            static_key = str(ds_static.resolve())
        except OSError:
            static_key = str(ds_static)
        episode_sig = (
            len(episode_ids),
            int(episode_ids[0]) if episode_ids else -1,
            int(episode_ids[-1]) if episode_ids else -1,
            sum(int(ep) for ep in episode_ids),
        )
        meta_sig = None
        if dataset_obj is not None and hasattr(dataset_obj, "meta"):
            episodes_meta = getattr(dataset_obj.meta, "episodes", {})
            meta_sig = (id(episodes_meta), len(episodes_meta))
        return (
            static_key,
            episode_sig,
            active_variant,
            self.file_signature(tag_file),
            self.file_signature(reviewed_file),
            self.file_signature(source_file),
            meta_sig,
        )

    def viewer_tag_episode_map(
        self, ds_static: Path, episode_ids: list[int], dataset_obj=None
    ) -> dict[str, dict[str, list[int]]]:
        signature = self.viewer_tag_map_signature(ds_static, episode_ids, dataset_obj)
        cached = self.state.viewer_tag_map_cache.get(signature)
        if cached is not None:
            return cached

        started_at = time.time()
        tag_records = {}
        try:
            tagging_dir = Path(ds_static) / "tagging"
            active_variant = self.fast_active_tag_variant(tagging_dir)
            tag_records = (
                current_tags(tagging_dir, active_variant)
                if active_variant or tags_path(tagging_dir).is_file()
                else {}
            )
        except Exception:
            logging.exception("Failed to load current tagging output for viewer tag map")
            tag_records = {}

        tag_episode_map: dict[str, dict[str, list[int]]] = {}
        for ep_id in episode_ids:
            record = tag_records.get(int(ep_id)) or {}
            tags = record.get("tags") or {}
            if not tags and dataset_obj is not None and hasattr(dataset_obj, "meta"):
                episodes_meta = getattr(dataset_obj.meta, "episodes", {})
                ep_info = episodes_meta.get(ep_id) or episodes_meta.get(str(ep_id)) or {}
                tags = ep_info.get("tags") or {}
            if not isinstance(tags, dict):
                continue
            for name, value in tags.items():
                key = self.viewer_tag_value_key(str(name), value)
                if key is None:
                    continue
                tag_episode_map.setdefault(str(name), {}).setdefault(key, []).append(int(ep_id))
        if len(self.state.viewer_tag_map_cache) > 32:
            self.state.viewer_tag_map_cache.clear()
        self.state.viewer_tag_map_cache[signature] = tag_episode_map
        elapsed = time.time() - started_at
        if elapsed > 0.5:
            tag_values = sum(len(values) for values in tag_episode_map.values())
            logging.info(
                "Built viewer tag map for %s (%s episodes, %s tag values) in %.2fs",
                Path(ds_static),
                len(episode_ids),
                tag_values,
                elapsed,
            )
        return tag_episode_map

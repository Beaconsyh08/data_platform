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

"""Legacy dataset response and navigation contracts."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from lerobot.data_platform.precompute.data_profile import (
    dataset_semantics,
    has_body_joint_dimensions,
)
from lerobot.data_platform.precompute.labeling.review import (
    labels_path,
)
from lerobot.data_platform.precompute.timeseries import (
    DATA_VERSION_DVT1,
)
from lerobot.data_platform.services.cache import DatasetCacheService
from lerobot.data_platform.services.catalog import DatasetCatalog
from lerobot.data_platform.services.dataset_paths import (
    _is_dataset_root,
    _normalize_data_version,
    _repo_id_from_key,
    _repo_key,
)
from lerobot.data_platform.services.state import ConsoleState


@dataclass
class DatasetPresentation:
    state: ConsoleState
    cache: DatasetCacheService
    catalog: DatasetCatalog
    allowed_tabs: set[str]
    allowed_open_links: set[str]

    def tab_enabled(self, tab: str) -> bool:
        return tab in self.allowed_tabs

    def open_link_enabled(self, key: str) -> bool:
        return key in self.allowed_open_links

    def smoothing_status(self, root: Path, repo_id: str) -> dict:
        root = Path(root).expanduser()
        meta = self.cache.load_smoothing_meta(root)
        if meta is not None:
            return {
                "status": "ready",
                "report_url": f"/{repo_id}/smoothing",
                "root": str(root),
                "source_root": meta.get("source_root"),
                "fields": meta.get("fields") or [meta.get("field") or "action"],
                "created_at": meta.get("created_at"),
            }
        derived = self.cache.find_derived_smoothing(root)
        if derived:
            return {
                "status": "ready",
                "report_url": derived["report_url"],
                "root": derived["root"],
                "source_root": str(root),
                "derived_repo_id": derived["repo_id"],
                "fields": derived.get("fields", []),
                "created_at": derived.get("created_at"),
            }
        return {
            "status": "missing",
            "report_url": f"/{repo_id}/smoothing",
        }

    def serialize_dataset(self, dataset_key: tuple[str, str]) -> dict:
        dataset_obj, ds_static = self.state.datasets_registry[dataset_key]
        episode_ids = self.cache.dataset_episode_ids(dataset_obj, dataset_key)
        repo_id = _repo_id_from_key(dataset_key)
        data_profile = self.cache.data_profile_for_dataset(dataset_obj)
        return {
            "key": repo_id,
            "repo_id": repo_id,
            "loaded": True,
            **dataset_semantics(dataset_obj.meta.info, data_profile),
            "cache_only": False,
            "root": str(getattr(dataset_obj, "root", "")),
            "output_dir": str(ds_static.parent),
            "static_dir": str(ds_static),
            "episodes": episode_ids,
            "episode_count": len(episode_ids),
            "image_keys": self.cache.dataset_image_keys(dataset_obj),
            "editable_features": self.cache.editable_vector_features(dataset_obj.features),
            "merge_signal_features": {
                k: dataset_obj.features[k]
                for k in ("action", "state", "observation.state")
                if k in dataset_obj.features
            },
            "data_version": data_profile.legacy_data_version,
            "robot_profile": data_profile.robot_profile,
            "signal_schema": data_profile.signal_schema,
            "stage_profile": data_profile.stage_profile,
            "gripper_encoding": data_profile.gripper_encoding,
            "profile_confirmed": data_profile.confirmed,
            "has_body_joints": has_body_joint_dimensions(dataset_obj.features),
            "dataset_format_version": str(dataset_obj.meta.info.get("codebase_version") or ""),
            "cache": self.cache.cached_light_cache_status(Path(dataset_obj.root), ds_static.parent),
            "labeling": self.cache.labeling_status(dataset_key, ds_static),
            "construction": self.cache.construction_status(dataset_key, dataset_obj, ds_static),
            "tagging": self.cache.tagging_status(dataset_key, ds_static),
            "embedding": self.cache.embedding_status(dataset_key, ds_static),
            "compare": self.cache.compare_status(ds_static),
            "smoothing": self.smoothing_status(Path(dataset_obj.root), repo_id),
            "viewer_url": f"/{repo_id}/episode_{episode_ids[0] if episode_ids else 0}",
            "analysis_url": f"/{repo_id}/analysis",
            **self.catalog.dataset_protection_payload(dataset_key),
        }

    def serialize_dataset_light(self, dataset_key: tuple[str, str]) -> dict:
        entry = self.state.datasets_index[dataset_key]
        repo_id = _repo_id_from_key(dataset_key)
        root_path = Path(entry["root"]).expanduser()
        output_dir = Path(entry["output_dir"]).expanduser()
        info = self.cache.read_light_dataset_info(root_path, output_dir) or {}
        episode_ids = self.cache.light_episode_ids(root_path, output_dir)
        episode_count = len(episode_ids) or int(info.get("total_episodes") or 0)
        image_keys = list(info.get("image_keys") or [])
        cache_only = not _is_dataset_root(root_path)
        labeling_status = self.cache.labeling_status(dataset_key, output_dir / "static")
        if cache_only:
            labeling_status = {**labeling_status, "review_url": "", "merge_url": ""}
        return {
            "key": repo_id,
            "repo_id": repo_id,
            "loaded": False,
            "cache_only": cache_only,
            "root": str(root_path),
            "output_dir": str(output_dir),
            "static_dir": str(output_dir / "static"),
            "episodes": episode_ids if episode_ids else list(range(episode_count)),
            "episode_count": episode_count or None,
            "image_keys": image_keys,
            **{
                key: info.get(key)
                for key in ("robot_type", "data_profile", "operation_capabilities", "data_profile_protocol")
            },
            "editable_features": list(info.get("editable_features") or []),
            "merge_signal_features": info.get("merge_signal_features") or {},
            "data_version": info.get("data_version"),
            "robot_profile": info.get("robot_profile", ""),
            "signal_schema": info.get("signal_schema", ""),
            "stage_profile": info.get("stage_profile", ""),
            "gripper_encoding": info.get("gripper_encoding", ""),
            "profile_confirmed": bool(info.get("profile_confirmed", False)),
            "has_body_joints": bool(info.get("has_body_joints", False)),
            "dataset_format_version": info.get("dataset_format_version", ""),
            "cache": self.cache.cached_light_cache_status(root_path, output_dir),
            "labeling": labeling_status,
            "construction": {
                "status": "missing",
                "object_labeling_ready": labels_path(output_dir / "static" / "labeling").is_file(),
                "records_count": 0,
                "rejected_count": 0,
                "review_url": f"/{repo_id}/construction",
            },
            "tagging": self.cache.tagging_status(dataset_key, output_dir / "static"),
            "embedding": self.cache.embedding_status(dataset_key, output_dir / "static"),
            "compare": self.cache.compare_status(output_dir / "static"),
            "smoothing": self.smoothing_status(root_path, repo_id),
            "viewer_url": f"/{repo_id}/episode_{episode_ids[0] if episode_ids else 0}",
            "analysis_url": f"/{repo_id}/analysis",
            **self.catalog.dataset_protection_payload(dataset_key),
        }

    def serialize_dataset_fast_detail(self, dataset_key: tuple[str, str]) -> dict:
        """Return enough detail for the home console without constructing LeRobotDatasetMetadata."""
        entry = self.state.datasets_index[dataset_key]
        repo_id = _repo_id_from_key(dataset_key)
        root_path = Path(entry["root"]).expanduser()
        output_dir = Path(entry["output_dir"]).expanduser()
        ds_static = output_dir / "static"
        info = self.cache.read_light_dataset_info(root_path, output_dir) or {}
        episode_ids = self.cache.light_episode_ids(root_path, output_dir)
        total_episodes = len(episode_ids) or int(info.get("total_episodes") or 0)
        image_keys = list(info.get("image_keys") or [])
        plan_path = root_path / "meta" / "construction_plan.json"
        labels_file = labels_path(ds_static / "labeling")
        cache_only = not _is_dataset_root(root_path)
        labeling_status = self.cache.labeling_status(dataset_key, ds_static)
        if cache_only:
            labeling_status = {**labeling_status, "review_url": "", "merge_url": ""}
        return {
            "key": repo_id,
            "repo_id": repo_id,
            "loaded": True,
            "lazy_loaded": True,
            "cache_only": cache_only,
            "root": str(root_path),
            "output_dir": str(output_dir),
            "static_dir": str(ds_static),
            "episodes": episode_ids if episode_ids else list(range(total_episodes)),
            "episode_count": total_episodes,
            "image_keys": image_keys,
            **{
                key: info.get(key)
                for key in ("robot_type", "data_profile", "operation_capabilities", "data_profile_protocol")
            },
            "editable_features": list(info.get("editable_features") or []),
            "data_version": info.get("data_version"),
            "dataset_format_version": info.get("dataset_format_version", ""),
            "cache": self.cache.cached_light_cache_status(root_path, output_dir),
            "labeling": labeling_status,
            "construction": {
                "status": "ready" if plan_path.is_file() else "missing",
                "object_labeling_ready": labels_file.is_file(),
                "records_count": None,
                "rejected_count": None,
                "review_url": f"/{repo_id}/construction",
            },
            "tagging": self.cache.tagging_status(dataset_key, ds_static),
            "embedding": self.cache.embedding_status(dataset_key, ds_static),
            "compare": self.cache.compare_status(ds_static),
            "smoothing": self.smoothing_status(root_path, repo_id),
            "viewer_url": f"/{repo_id}/episode_{episode_ids[0] if episode_ids else 0}",
            "analysis_url": f"/{repo_id}/analysis",
            **self.catalog.dataset_protection_payload(dataset_key),
        }

    def home_url(self, repo_id: str | None = None, tab: str | None = None) -> str:
        params = []
        if repo_id:
            params.append(f"select={quote(repo_id, safe='')}")
        if tab:
            params.append(f"tab={quote(tab, safe='')}")
        return "/" + (f"?{'&'.join(params)}" if params else "")

    def dataset_nav(
        self,
        repo_id: str,
        first_episode: int = 0,
        active: str = "",
        dataset_obj=None,
        ds_static: Path | None = None,
        cache_only: bool = False,
        manifest: dict | None = None,
    ) -> dict:
        dataset_key = _repo_key(repo_id)
        if (dataset_obj is None or ds_static is None) and dataset_key in self.state.datasets_registry:
            dataset_obj, ds_static = self.state.datasets_registry[dataset_key]
        viewer_ready = active == "viewer"
        labeling_ready = active == "labeling"
        construction_ready = active == "construction"
        tagging_ready = active == "tagging"
        embedding_ready = active == "embedding"
        smoothing_ready = active == "smoothing"
        smoothing_href = f"/{repo_id}/smoothing"
        data_version = (
            _normalize_data_version((manifest or {}).get("data_version")) if manifest else DATA_VERSION_DVT1
        )
        tagging_status = {"review_url": f"/{repo_id}/tagging"}
        if cache_only and ds_static is not None:
            viewer_ready = True
            labeling_status = self.cache.labeling_status(dataset_key, Path(ds_static))
            labeling_ready = labeling_ready or labeling_status["status"] == "ready"
            tagging_status = self.cache.tagging_status(dataset_key, Path(ds_static))
            tagging_ready = tagging_ready or tagging_status["status"] == "ready"
            embedding_ready = embedding_ready or (Path(ds_static) / "embedding" / "coords_2d.npz").is_file()
            construction_ready = construction_ready or (Path(ds_static) / "construction").exists()
        if dataset_obj is not None and ds_static is not None:
            try:
                viewer_ready = (
                    viewer_ready
                    or self.cache.cached_light_cache_status(Path(dataset_obj.root), Path(ds_static).parent)[
                        "status"
                    ]
                    == "cached"
                )
            except Exception:
                viewer_ready = viewer_ready
            data_version = self.cache.data_profile_for_dataset(dataset_obj).legacy_data_version
            labeling_ready = labeling_ready or labels_path(ds_static / "labeling").is_file()
            tagging_status = self.cache.tagging_status(dataset_key, ds_static)
            tagging_ready = tagging_ready or tagging_status["status"] == "ready"
            embedding_ready = embedding_ready or (ds_static / "embedding" / "coords_2d.npz").is_file()
            try:
                construction_ready = (
                    construction_ready
                    or self.cache.construction_status(dataset_key, dataset_obj, ds_static)["status"]
                    == "ready"
                )
            except Exception:
                construction_ready = construction_ready
            smoothing_status = self.smoothing_status(Path(dataset_obj.root), repo_id)
            smoothing_ready = smoothing_ready or smoothing_status["status"] == "ready"
            smoothing_href = smoothing_status.get("report_url") or smoothing_href
        inspect_href = f"/{repo_id}/inspect"
        if (self.state.datasets_index.get(dataset_key) or {}).get("internal_remote_cache"):
            inspect_href = (
                f"/remote/{Path(self.state.datasets_index[dataset_key]['output_dir']).name}/inspect"
            )
        links = [
            {
                "key": "inspect",
                "label": "Data Inspect",
                "href": inspect_href,
                "enabled": True,
                "hint": "Preview a small window of source records or Viewer CSV.",
            },
            {
                "key": "viewer",
                "label": "Viewer",
                "href": f"/{repo_id}/episode_{first_episode}?data_version={quote(data_version or '', safe='')}",
                "enabled": viewer_ready,
                "hint": "Prepare video and CSV cache first.",
            },
            {
                "key": "analysis",
                "label": "Analysis",
                "href": f"/{repo_id}/analysis",
                "enabled": True,
                "hint": "",
            },
            {
                "key": "labeling",
                "label": "Labeling",
                "href": f"/{repo_id}/labeling",
                "enabled": labeling_ready,
                "hint": "Run Object Labeling first.",
            },
            {
                "key": "construction",
                "label": "Construction",
                "href": f"/{repo_id}/construction",
                "enabled": construction_ready,
                "hint": "Run Data Construction first.",
            },
            {
                "key": "tagging",
                "label": "Tagging",
                "href": tagging_status.get("review_url", f"/{repo_id}/tagging")
                if ds_static is not None
                else f"/{repo_id}/tagging",
                "enabled": tagging_ready,
                "hint": "Run Auto-tagging first.",
            },
            {
                "key": "embedding",
                "label": "Embedding",
                "href": f"/{repo_id}/embedding",
                "enabled": embedding_ready,
                "hint": "Run Embedding first.",
            },
            {
                "key": "smoothing",
                "label": "Smoothing",
                "href": smoothing_href,
                "enabled": smoothing_ready,
                "hint": "Run Smooth action first.",
            },
            {
                "key": "compare",
                "label": "Compare",
                "href": self.home_url(repo_id, "compare"),
                "enabled": True,
                "hint": "",
            },
        ]
        links = [link for link in links if self.open_link_enabled(link["key"])]
        return {
            "home_url": self.home_url(repo_id),
            "nav_active": active,
            "nav_links": links,
            "task_configuration_enabled": any(
                self.tab_enabled(tab) for tab in ("versions", "curation_manifest")
            ),
        }

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

"""Dataset registration, loading, source protection and result association."""

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from lerobot.data_platform.cli import get_default_output_dir
from lerobot.data_platform.data_protection import (
    evaluate_dataset_protection,
    infer_dataset_stage,
    normalized_path,
)
from lerobot.data_platform.lifecycle import LifecycleStore
from lerobot.data_platform.precompute.viewer_manifest import (
    load_viewer_manifest,
    manifest_episode_ids,
)
from lerobot.data_platform.services.cache import DatasetCacheService
from lerobot.data_platform.services.dataset_paths import _is_dataset_root, _repo_id_from_key, _repo_key
from lerobot.data_platform.services.state import ConsoleState


@dataclass
class DatasetCatalog:
    state: ConsoleState
    cache: DatasetCacheService

    def protected_source_roots(self) -> list[Path]:
        return [Path(value) for value in sorted(self.state.registry_state["protected_source_roots"])]

    def dataset_stage(self, root_path: Path, declared_stage: str | None = None) -> str:
        return infer_dataset_stage(root_path, declared_stage)

    def dataset_protection_for_key(self, dataset_key: tuple[str, str]) -> dict:
        entry = self.state.datasets_index.get(dataset_key)
        if entry is None:
            raise KeyError(f"dataset is not registered: {_repo_id_from_key(dataset_key)}")
        root_path = Path(entry["root"]).expanduser()
        return evaluate_dataset_protection(
            root_path,
            source_roots=self.protected_source_roots(),
            manual_source=bool(entry.get("manual_source", False)),
            manual_reason=str(entry.get("manual_source_reason") or ""),
            stage=self.dataset_stage(root_path, str(entry.get("stage") or "")),
        ).to_dict()

    def dataset_is_protected(self, dataset_key: tuple[str, str]) -> bool:
        return bool(self.dataset_protection_for_key(dataset_key)["protected"])

    def dataset_protection_payload(self, dataset_key: tuple[str, str]) -> dict:
        protection = self.dataset_protection_for_key(dataset_key)
        return {
            "stage": protection["stage"],
            "source_protected": protection["protected"],
            "retention_class": protection["retention_class"],
            "protection": protection,
        }

    def cache_only_pair_for_key(self, dataset_key: tuple[str, str]) -> tuple[Path, Path] | None:
        root_dir = self.state.registry_state.get("datasets_root")
        if root_dir is None:
            return None
        root_dir = Path(root_dir).expanduser()
        repo_id = _repo_id_from_key(dataset_key)
        namespace, name = dataset_key
        candidates = [
            root_dir / name,
            root_dir / repo_id,
            root_dir / namespace / name,
            root_dir / "vis" / name,
            root_dir / "vis" / "_console" / name,
        ]
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            try:
                pair = self.cache.cache_only_root_and_output(candidate)
            except Exception:
                pair = None
            if pair is not None:
                return pair
        return None

    def static_dir_for_key(self, dataset_key: tuple[str, str]) -> Path | None:
        entry = self.state.datasets_registry.get(dataset_key)
        if entry is not None:
            return Path(entry[1])
        index_entry = self.state.datasets_index.get(dataset_key)
        if index_entry is not None:
            return Path(index_entry["output_dir"]).expanduser() / "static"
        cache_pair = self.cache_only_pair_for_key(dataset_key)
        if cache_pair is not None:
            return Path(cache_pair[1]).expanduser() / "static"
        return self.state.static_folder if dataset_key == self.state.primary_key else None

    def dataset_root_for_key(self, dataset_key: tuple[str, str]) -> Path | None:
        entry = self.state.datasets_registry.get(dataset_key)
        if entry is not None:
            dataset_obj = entry[0]
            root = getattr(dataset_obj, "root", None)
            return Path(root).expanduser() if root is not None else None
        index_entry = self.state.datasets_index.get(dataset_key)
        if index_entry is not None and index_entry.get("root"):
            return Path(index_entry["root"]).expanduser()
        cache_pair = self.cache_only_pair_for_key(dataset_key)
        if cache_pair is not None:
            return Path(cache_pair[0]).expanduser()
        if (
            dataset_key == self.state.primary_key
            and self.state.dataset is not None
            and getattr(self.state.dataset, "root", None) is not None
        ):
            return Path(self.state.dataset.root).expanduser()
        return None

    def manifest_for_key(self, dataset_key: tuple[str, str]) -> tuple[dict | None, Path | None]:
        ds_static = self.static_dir_for_key(dataset_key)
        if ds_static is None:
            return None, None
        manifest = load_viewer_manifest(ds_static)
        if manifest is None:
            manifest = self.cache.fallback_manifest_from_cache(ds_static, _repo_id_from_key(dataset_key))
        return manifest, ds_static

    def upsert_dataset_index(self, repo_id: str, root_path: Path, output_dir: Path) -> tuple[str, str]:
        from lerobot.data_platform.environment import validate_dev_path

        validate_dev_path(root_path)
        validate_dev_path(output_dir)
        root_path = normalized_path(root_path)
        output_dir = normalized_path(output_dir)
        dataset_key = _repo_key(repo_id)
        existing_for_key = self.state.datasets_index.get(dataset_key)
        if existing_for_key is not None and normalized_path(existing_for_key["root"]) != root_path:
            suffix = uuid.uuid5(uuid.NAMESPACE_URL, str(root_path)).hex[:8]
            dataset_key = dataset_key[0], f"{dataset_key[1]}-{suffix}"
        existing = self.state.datasets_index.get(dataset_key) or {}
        self.state.datasets_index[dataset_key] = {
            "repo_id": _repo_id_from_key(dataset_key),
            "root": str(root_path),
            "output_dir": str(output_dir),
            "stage": str(existing.get("stage") or infer_dataset_stage(root_path)),
            "manual_source": bool(existing.get("manual_source", False)),
            "manual_source_reason": str(existing.get("manual_source_reason") or ""),
        }
        return dataset_key

    def register_remote_cache(self, location: dict, cache_output_dir: Path) -> str:
        cache_output_dir = Path(cache_output_dir).expanduser().resolve()
        manifest = load_viewer_manifest(cache_output_dir / "static")
        if manifest is None:
            raise FileNotFoundError(cache_output_dir / "static" / "viewer_manifest.json")
        placeholder_root = cache_output_dir / "remote_source"
        previous_key = _repo_key(str(location["dataset_key"]))
        if (self.state.datasets_index.get(previous_key) or {}).get("internal_remote_cache"):
            self.state.datasets_index[previous_key]["root"] = str(placeholder_root)
            self.state.datasets_registry.pop(previous_key, None)
            self.state.episodes_by_key.pop(previous_key, None)
            self.cache.clear_episode_dependent_caches(previous_key)
        dataset_key = self.upsert_dataset_index(
            str(location["dataset_key"]),
            placeholder_root,
            cache_output_dir,
        )
        self.state.datasets_index[dataset_key]["internal_remote_cache"] = True
        self.save_registry()
        episode_ids = manifest_episode_ids(manifest)
        first_episode = episode_ids[0] if episode_ids else 0
        return f"/{_repo_id_from_key(dataset_key)}/episode_{first_episode}"

    def restore_remote_cache_routes(self) -> None:
        if self.state.control_plane_store is None:
            return
        for location in self.state.control_plane_store.list_locations():
            metadata = dict(location.get("metadata") or {})
            cache_root = str(metadata.get("cache_root") or "").strip()
            if not metadata.get("viewer_ready") or not cache_root:
                continue
            try:
                from lerobot.data_platform.dataset_results import migrate_cache

                cache_base = (
                    Path(self.state.remote_cache_root).expanduser()
                    if self.state.remote_cache_root is not None
                    else Path(self.state.registry_state["path"]).parent.parent / "remote_cache"
                ).resolve()
                if not Path(cache_root).resolve().is_relative_to(cache_base):
                    raise ValueError("Registered cache is outside the configured cache root")
                cache_root = str(migrate_cache(Path(cache_root), cache_base / location["location_id"]))
                viewer_url = self.register_remote_cache(location, Path(cache_root))
            except FileNotFoundError:
                logging.warning(
                    "Remote viewer cache is missing for %s; marking it stale",
                    location.get("dataset_key") or location.get("location_id"),
                )
                self.state.control_plane_store.mark_viewer_stale(location["location_id"])
            except Exception:
                logging.exception(
                    "Could not restore remote viewer route for %s",
                    location.get("dataset_key") or location.get("location_id"),
                )
            else:
                if viewer_url != metadata.get("viewer_url") or cache_root != metadata.get("cache_root"):
                    self.state.control_plane_store.mark_viewer_ready(
                        location["location_id"],
                        viewer_url=viewer_url,
                        cache_root=cache_root,
                    )

    def dataset_index_uses_v3_adapter(self, dataset_key: tuple[str, str]) -> bool:
        entry = self.state.datasets_index.get(dataset_key)
        if entry is None:
            return False
        info = (
            self.cache.read_light_dataset_info(
                Path(entry["root"]).expanduser(),
                Path(entry["output_dir"]).expanduser(),
            )
            or {}
        )
        version = str(info.get("dataset_format_version") or "").lower().removeprefix("v")
        return version == "3" or version.startswith("3.")

    def ensure_dataset_loaded(self, dataset_key: tuple[str, str]) -> tuple[object, Path]:
        entry = self.state.datasets_registry.get(dataset_key)
        if entry is not None:
            if not Path(entry[0].root).exists() and not (Path(entry[1]) / "viewer_manifest.json").is_file():
                self.state.datasets_registry.pop(dataset_key, None)
                raise KeyError(_repo_id_from_key(dataset_key))
            return entry
        index_entry = self.state.datasets_index.get(dataset_key)
        if index_entry is None:
            raise KeyError(_repo_id_from_key(dataset_key))

        root_path = Path(index_entry["root"]).expanduser()
        output_dir = Path(index_entry["output_dir"]).expanduser()
        if not _is_dataset_root(root_path) and self.cache.root_has_cache_manifest(root_path, output_dir):
            raise KeyError(f"{_repo_id_from_key(dataset_key)} is cache-only")
        repo_id = index_entry["repo_id"]
        dataset_obj = self.state.metadata_factory(repo_id, root=root_path)
        ds_static = output_dir / "static"
        self.cache.ensure_dataset_static(dataset_obj, ds_static)
        self.state.datasets_registry[dataset_key] = (dataset_obj, ds_static)
        self.state.episodes_by_key[dataset_key] = self.cache.dataset_episode_ids(dataset_obj, dataset_key)
        self.state.task_caches.pop(dataset_key, None)
        return self.state.datasets_registry[dataset_key]

    def register_dataset(
        self,
        dataset_obj,
        output_dir: Path,
        selected_episodes: list[int] | None = None,
        persist: bool = True,
    ) -> tuple[str, str]:
        dataset_key = _repo_key(dataset_obj.repo_id)
        ds_static = Path(output_dir) / "static"
        self.upsert_dataset_index(_repo_id_from_key(dataset_key), Path(dataset_obj.root), Path(output_dir))
        self.cache.ensure_dataset_static(dataset_obj, ds_static, selected_episodes)
        self.state.datasets_registry[dataset_key] = (dataset_obj, ds_static)
        if selected_episodes is not None:
            self.state.episodes_by_key[dataset_key] = selected_episodes
        else:
            self.state.episodes_by_key[dataset_key] = self.cache.dataset_episode_ids(dataset_obj, dataset_key)
        self.state.task_caches.pop(dataset_key, None)
        if persist:
            self.save_registry()
        return dataset_key

    def registry_path_for_root(self, root_dir: Path | None) -> Path:
        if self.state.control_plane_store is not None or root_dir is None:
            return self.state.static_folder / "datasets_registry.json"
        return root_dir / "vis" / "_console" / "static" / "datasets_registry.json"

    def set_registry_root(self, root_dir: Path | None, load: bool = True) -> None:
        root_dir = Path(root_dir).expanduser() if root_dir is not None else None
        old_path = self.state.registry_state["path"]
        self.state.registry_state["datasets_root"] = root_dir
        self.state.registry_state["path"] = self.registry_path_for_root(root_dir)
        if load:
            self.load_registry(clear_loaded=self.state.registry_state["path"] != old_path)

    def lifecycle_store(self) -> LifecycleStore:
        return LifecycleStore(Path(self.state.registry_state["path"]).parent / "lifecycle")

    def save_registry(self) -> None:
        if os.environ.get("DATA_PLATFORM_INTERNAL_EXECUTION"):
            return
        registry_path = self.state.registry_state["path"]
        entries = []
        for dataset_key, entry in sorted(self.state.datasets_index.items()):
            root_path = Path(entry["root"]).expanduser()
            output_dir = Path(entry["output_dir"]).expanduser()
            if not _is_dataset_root(root_path) and not self.cache.root_has_cache_manifest(
                root_path, output_dir
            ):
                continue
            entries.append(
                {
                    "repo_id": _repo_id_from_key(dataset_key),
                    "root": str(root_path),
                    "output_dir": str(output_dir),
                    "stage": str(entry.get("stage") or infer_dataset_stage(root_path)),
                    "manual_source": bool(entry.get("manual_source", False)),
                    "manual_source_reason": str(entry.get("manual_source_reason") or ""),
                    "internal_remote_cache": self.cache.is_internal_remote_cache_entry(entry),
                }
            )
        try:
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "datasets": entries,
                        "protected_source_roots": sorted(self.state.registry_state["protected_source_roots"]),
                    },
                    indent=2,
                )
            )
        except OSError as exc:
            logging.warning("Could not save dataset registry to %s: %s", registry_path, exc)

    def load_registry(self, clear_loaded: bool = False) -> None:
        self.state.datasets_index.clear()
        self.state.registry_state["protected_source_roots"] = set(
            self.state.registry_state["configured_source_roots"]
        )
        if clear_loaded:
            self.state.datasets_registry.clear()
            self.state.episodes_by_key.clear()
            self.state.task_caches.clear()
        registry_path = self.state.registry_state["path"]
        if not registry_path.is_file():
            return
        try:
            data = json.loads(registry_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logging.warning("Could not load dataset registry from %s: %s", registry_path, exc)
            return
        persisted_source_roots = {
            str(normalized_path(value))
            for value in data.get("protected_source_roots", [])
            if str(value).strip()
        }
        self.state.registry_state["protected_source_roots"] = (
            set(self.state.registry_state["configured_source_roots"]) | persisted_source_roots
        )
        for entry in data.get("datasets", []):
            root_path = Path(str(entry.get("root", ""))).expanduser()
            repo_id = str(entry.get("repo_id") or f"local/{root_path.name or 'dataset'}")
            output_dir = Path(entry.get("output_dir") or get_default_output_dir(root_path)).expanduser()
            if not _is_dataset_root(root_path) and not self.cache.root_has_cache_manifest(
                root_path, output_dir
            ):
                continue
            dataset_key = self.upsert_dataset_index(repo_id, root_path, output_dir)
            self.state.datasets_index[dataset_key].update(
                {
                    "stage": str(entry.get("stage") or infer_dataset_stage(root_path)),
                    "manual_source": bool(entry.get("manual_source", False)),
                    "manual_source_reason": str(entry.get("manual_source_reason") or ""),
                    "internal_remote_cache": bool(entry.get("internal_remote_cache", False))
                    or root_path.name == "remote_source",
                }
            )

    def visible_dataset_keys(self, user: dict | None = None) -> list[tuple[str, str]]:
        allowed = self.state.control_plane_store.restricted_dataset_keys(user["user_id"]) if user else None
        return [
            dataset_key
            for dataset_key in sorted(self.state.datasets_index)
            if (allowed is None or _repo_id_from_key(dataset_key) in allowed)
            and not self.cache.is_internal_remote_cache_entry(self.state.datasets_index[dataset_key])
            and (
                _is_dataset_root(Path(self.state.datasets_index[dataset_key]["root"]).expanduser())
                or self.cache.root_has_cache_manifest(
                    Path(self.state.datasets_index[dataset_key]["root"]).expanduser(),
                    Path(self.state.datasets_index[dataset_key]["output_dir"]).expanduser(),
                )
            )
        ]

    def dataset_candidates(self, root_dir: Path | None = None, *, discover: bool = True) -> list[dict]:
        if root_dir is None:
            return []
        root_dir = Path(root_dir).expanduser()
        if not root_dir.exists():
            return []

        roots: list[Path] = []
        if not discover:
            base = normalized_path(root_dir)
            roots = [
                normalized_path(entry["root"])
                for entry in self.state.datasets_index.values()
                if not self.cache.is_internal_remote_cache_entry(entry)
                and normalized_path(entry["root"]).is_relative_to(base)
            ]
        elif _is_dataset_root(root_dir):
            roots.append(root_dir)
        else:
            stack = [root_dir]
            while stack:
                current = stack.pop()
                if current != root_dir and _is_dataset_root(current):
                    roots.append(current)
                    continue
                try:
                    children = sorted(current.iterdir(), key=lambda item: item.name, reverse=True)
                except OSError:
                    continue
                for child in children:
                    if not child.is_dir():
                        continue
                    if child.name.startswith(".") or child.name in {"meta", "data", "videos"}:
                        continue
                    stack.append(child)
        normalized_roots: dict[Path, Path | None] = {}
        for root in roots:
            if _is_dataset_root(root):
                normalized_roots.setdefault(root, None)
        roots = sorted(normalized_roots, key=lambda item: item.as_posix())

        candidates = []
        registered_by_root = {
            str(normalized_path(entry["root"])): _repo_id_from_key(dataset_key)
            for dataset_key, entry in self.state.datasets_index.items()
        }
        output_by_root = {
            str(normalized_path(entry["root"])): entry["output_dir"]
            for entry in self.state.datasets_index.values()
        }
        for dataset_root in roots:
            dataset_root = normalized_path(dataset_root)
            repo_id = f"local/{dataset_root.name or 'dataset'}"
            registered_key = registered_by_root.get(str(dataset_root))
            default_output_dir = normalized_roots.get(dataset_root) or get_default_output_dir(dataset_root)
            output_dir = Path(output_by_root.get(str(dataset_root), str(default_output_dir)))
            info = self.cache.read_light_dataset_info(dataset_root, output_dir) or {}
            protection = evaluate_dataset_protection(
                dataset_root,
                source_roots=self.protected_source_roots(),
                stage=self.dataset_stage(dataset_root),
            ).to_dict()
            candidates.append(
                {
                    "name": dataset_root.name,
                    "root": str(dataset_root),
                    "repo_id": registered_key or repo_id,
                    "output_dir": str(output_dir),
                    "registered": registered_key is not None,
                    "registered_key": registered_key,
                    "data_version": info.get("data_version"),
                    "dataset_format_version": info.get("dataset_format_version", ""),
                    "cache_only": False,
                    "cache": self.cache.cached_light_cache_status(dataset_root, output_dir),
                    "stage": protection["stage"],
                    "source_protected": protection["protected"],
                    "retention_class": protection["retention_class"],
                    "protection": protection,
                }
            )
        return candidates

    def unregister_missing_dataset(self, location):
        cache = Path(self.state.remote_cache_root) / location["location_id"]
        local_source = location["node_id"] in {
            node["node_id"]
            for node in self.state.control_plane_store.list_nodes()
            if node["name"].startswith("local-")
        }
        for key, entry in list(self.state.datasets_index.items()):
            source, output = Path(entry["root"]), Path(entry["output_dir"])
            cached = source.is_relative_to(cache) or output.is_relative_to(cache)
            if not cached and not (local_source and str(source) == location["root"]):
                continue
            self.state.datasets_index.pop(key, None)
            self.state.datasets_registry.pop(key, None)
            self.state.episodes_by_key.pop(key, None)
            self.cache.clear_episode_dependent_caches(key)
            self.cache.invalidate_light_cache_status(source, output)
        self.save_registry()

    def complete_local_execution(self, result):
        if result.get("local_preprocess"):
            from lerobot.data_platform.processing.lineage import register_local_preprocess

            register_local_preprocess(self.lifecycle_store(), result["local_preprocess"])
        for entry in result.get("local_registrations") or []:
            root = Path(entry["root"])
            if not _is_dataset_root(root):
                continue
            refreshed = self.state.metadata_factory(entry["repo_id"], root=root)
            key = self.register_dataset(refreshed, Path(entry["output_dir"]))
            self.cache.refresh_dataset_after_episode_delete(
                key, refreshed, Path(entry["output_dir"]) / "static"
            )
        self.cache.clear_episode_dependent_caches()

    def unregister_dataset(self, dataset_key: tuple[str, str]) -> None:
        entry = self.state.datasets_index.pop(dataset_key, None)
        self.state.datasets_registry.pop(dataset_key, None)
        self.state.episodes_by_key.pop(dataset_key, None)
        self.cache.clear_episode_dependent_caches(dataset_key)
        if entry:
            self.cache.invalidate_light_cache_status(Path(entry["root"]), Path(entry["output_dir"]))
        self.save_registry()

    def register_path(self, root: Path, *, repo_id: str = "", output_dir: str = "") -> tuple[str, str]:
        root = root.expanduser()
        if not root.exists():
            raise ValueError(f"dataset root does not exist: {root}")
        if not _is_dataset_root(root):
            raise ValueError(
                "cache-only registration is temporarily unavailable; "
                f"a dataset requires {root / 'meta' / 'info.json'}"
            )
        return self.upsert_dataset_index(
            repo_id or f"local/{root.name or 'dataset'}",
            root,
            Path(output_dir or get_default_output_dir(root)).expanduser(),
        )

    def register_precomputed(self, result) -> tuple[str, str]:
        dataset_key = self.upsert_dataset_index(result.repo_id, result.root, result.output_dir)
        self.state.datasets_registry.pop(dataset_key, None)
        self.state.episodes_by_key[dataset_key] = result.episodes
        self.state.task_caches.pop(dataset_key, None)
        self.cache.invalidate_light_cache_status(result.root, result.output_dir)
        self.save_registry()
        return dataset_key

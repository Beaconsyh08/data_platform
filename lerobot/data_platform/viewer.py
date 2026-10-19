#!/usr/bin/env python

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
"""Visualize episodes from a LeRobotDataset.

The last recorded frame does not necessarily represent a terminal state. A dataset stores
observations paired with actions, and it may stop after the action that reaches the final state
without recording another transition from that state.

The viewer displays the data used for training. Image modalities may contain lossy compression
artifacts because frames can be decoded from compressed MP4 videos.

Example of usage:

- Visualize data stored on a local machine:
```bash
local$ python -m lerobot.data_platform.viewer \
    --repo-id lerobot/pusht

local$ open http://localhost:9091
```

- Visualize a local dataset by its root path:
```bash
local$ python -m lerobot.data_platform.viewer \
    --root /path/to/dataset

local$ open http://localhost:9091
```

- Visualize data stored on a distant machine with a local viewer:
```bash
distant$ python -m lerobot.data_platform.viewer \
    --repo-id lerobot/pusht

local$ ssh -L 9091:localhost:9091 distant  # create a ssh tunnel
local$ open http://localhost:9091
```

- Select episodes to visualize:
```bash
python -m lerobot.data_platform.viewer \
    --repo-id lerobot/pusht \
    --episodes 7 3 5 1 4
```
"""

import argparse
import csv
import gc
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
    url_for,
)
from werkzeug.serving import WSGIRequestHandler

from lerobot import available_datasets
from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
from lerobot.common.datasets.utils import IterableNamespace
from lerobot.common.utils.utils import init_logging
from lerobot.data_platform.admin_auth import ADMIN_SESSION_COOKIE, AdminAuthStore
from lerobot.data_platform.cache_files import (
    cached_csv_files,
    logical_csv_path,
    open_cached_csv,
    resolve_cached_csv,
)
from lerobot.data_platform.cli import run_precompute
from lerobot.data_platform.data_protection import (
    normalized_path,
)
from lerobot.data_platform.dataset_metadata import MetaOnlyDataset
from lerobot.data_platform.operation_log import (
    append_operation_event,
    local_actor,
    read_operation_events,
    sanitize_for_log,
    should_audit_agent_request,
    summarize_location_sync_request,
)
from lerobot.data_platform.precompute.analysis import (
    build_dataset_analysis,
    read_analysis_cache,
    write_analysis_cache,
)
from lerobot.data_platform.precompute.data_profile import (
    STAGE_PROFILE_EQUAL_TIME,
    dataset_semantics,
    has_body_joint_dimensions,
    has_legacy_flag_dimension,
    resolve_processing_profile,
)
from lerobot.data_platform.precompute.dataset_io import (
    is_v3_dataset,
    is_v3_metadata,
    load_episode_records,
    read_episode_table,
    upsert_episode_column,
)
from lerobot.data_platform.precompute.image_io import (
    cached_image_bytes,
    get_parquet_file,
    get_row_group_offsets,
)
from lerobot.data_platform.precompute.labeling import (
    get_capabilities as get_labeling_capabilities,
)
from lerobot.data_platform.precompute.labeling import (
    run_labeling,
)
from lerobot.data_platform.precompute.labeling.review import (
    available_label_variants,
    load_labels_jsonl,
    merge_reviewed_labels_to_metadata,
    read_first_frame_jpeg,
    read_frame_image,
    remove_reviewed_record_for_variant,
    resolved_labels_path,
    resolved_reviewed_path,
    save_reviewed_record_for_variant,
    source_path,
)
from lerobot.data_platform.precompute.labeling.review import (
    load_episode_record as load_labeling_episode_record,
)
from lerobot.data_platform.precompute.labeling.review import (
    reason as labeling_reason,
)
from lerobot.data_platform.precompute.labeling.review import (
    uncertainty as labeling_uncertainty,
)
from lerobot.data_platform.precompute.mutations import (
    fix_episode_indices,
    update_episode_stats_for_subtask_state,
    update_info_features,
)
from lerobot.data_platform.precompute.preprocess.delete_episodes import (
    delete_episodes_inplace,
    reindex_static_after_episode_delete,
)
from lerobot.data_platform.precompute.preprocess.flag_fixes import trim_v3_episode_inplace
from lerobot.data_platform.precompute.preprocess.quality_flags import (
    apply_task_assignment_choice,
    list_task_assignment_choices,
)
from lerobot.data_platform.precompute.signal_columns import signal_columns
from lerobot.data_platform.precompute.tagging import (
    available_tag_variants,
    current_tags,
)
from lerobot.data_platform.precompute.timeseries import (
    DATA_VERSION_DVT1,
    DATA_VERSION_DVT2,
    GRIPPER_NORMALIZE_COLUMNS,
    UMI_GRIPPER_NORMALIZE_COLUMNS,
    normalize_gripper_columns,
    normalize_gripper_csv_value,
)
from lerobot.data_platform.precompute.v3_viewer import run_v3_viewer_precompute
from lerobot.data_platform.precompute.video import encode_episode_video
from lerobot.data_platform.precompute.viewer_manifest import (
    load_viewer_manifest,
    manifest_episode_ids,
    manifest_episode_info,
    manifest_task_episode_map,
    write_viewer_manifest,
)
from lerobot.data_platform.precompute.viewer_signals import viewer_csv_labels, viewer_signal_presentation
from lerobot.data_platform.processing.console_submissions import ConsoleSubmissionService
from lerobot.data_platform.routes import (
    RouteContext,
    register_compare_routes,
    register_construction_routes,
    register_embedding_routes,
    register_inspect_routes,
    register_lifecycle_routes,
    register_preprocess_routes,
    register_remote_analysis_routes,
    register_tagging_routes,
    register_task_routes,
    register_temporal_caption_routes,
)
from lerobot.data_platform.routes.console_datasets import register_console_dataset_routes
from lerobot.data_platform.routes.console_jobs import register_console_job_routes
from lerobot.data_platform.services.cache import DatasetCacheService
from lerobot.data_platform.services.catalog import DatasetCatalog
from lerobot.data_platform.services.dataset_paths import (
    _find_any_cached_csv,
    _get_feature_shape,
    _is_dataset_root,
    _normalize_data_version,
    _repo_id_from_key,
    _repo_key,
    get_columns_info,
)
from lerobot.data_platform.services.jobs import ConsoleJobService
from lerobot.data_platform.services.options import (
    _bool_option,
    _parse_int_list,
    _parse_str_list,
)
from lerobot.data_platform.services.presentation import DatasetPresentation
from lerobot.data_platform.services.state import ConsoleState
from lerobot.data_platform.task_catalog import TaskConfigSnapshot
from lerobot.data_platform.task_text import cached_subtask_names, generate_subtask_text


def _prepare_episode_videos(
    dataset: LeRobotDataset,
    episode_id: int,
    image_keys: list[str],
    static_dir: Path,
    max_frames: int | None = None,
    make_url=None,
) -> list[dict]:
    videos_info: list[dict] = []
    if not image_keys:
        return videos_info

    for image_key in image_keys:
        out_path = encode_episode_video(
            dataset.root,
            dataset.meta,
            episode_id,
            image_key,
            static_dir,
            max_frames=max_frames,
            overwrite=False,
        )
        rel_path = Path("videos") / image_key / f"episode_{episode_id:06d}_h264.mp4"
        if out_path and out_path.is_file() and out_path.stat().st_size > 0:
            url = (
                make_url(rel_path)
                if make_url is not None
                else url_for("static", filename=rel_path.as_posix())
            )
            videos_info.append(
                {
                    "url": url,
                    "filename": image_key,
                }
            )

    return videos_info


def _find_prepared_videos(
    static_dir: Path,
    image_keys: list[str],
    episode_id: int,
    cache_buster: str = "",
    make_url=None,
) -> list[dict]:
    videos_info: list[dict] = []
    _cb = f"?_t={cache_buster}" if cache_buster else ""
    for image_key in image_keys:
        rel_path = Path("videos") / image_key / f"episode_{episode_id:06d}_h264.mp4"
        out_path = static_dir / rel_path
        if out_path.is_file() and out_path.stat().st_size > 0:
            url = (
                make_url(rel_path)
                if make_url is not None
                else url_for("static", filename=rel_path.as_posix())
            )
            videos_info.append(
                {
                    "url": url + _cb,
                    "filename": image_key,
                }
            )
    return videos_info


def _read_parquet_head(parquet_path: Path, columns: list[str], max_rows: int) -> pd.DataFrame:
    pf = get_parquet_file(str(parquet_path))
    tables = []
    remaining = max_rows
    for rg_idx in range(pf.metadata.num_row_groups):
        table = pf.read_row_group(rg_idx, columns=columns)
        if len(table) > remaining:
            table = table.slice(0, remaining)
            tables.append(table)
            break
        tables.append(table)
        remaining -= len(table)
        if remaining <= 0:
            break
    if not tables:
        return pd.DataFrame(columns=columns)
    return pa.concat_tables(tables).to_pandas()


def _get_csv_cache_path(
    cache_dir: Path, episode_id: int, downsample: int | None, precomputed_only: bool
) -> Path | None:
    ds = downsample if downsample and downsample > 1 else 1
    preferred = resolve_cached_csv(cache_dir / f"episode_{episode_id:06d}_ds{ds}.csv")
    if preferred.is_file():
        return preferred
    if precomputed_only:
        return _find_any_cached_csv(cache_dir, episode_id)
    return preferred


def _columns_from_csv_header(csv_path: Path, features: dict | None = None) -> list[dict]:
    try:
        with open_cached_csv(csv_path) as f:
            header_line = f.readline().strip()
    except OSError:
        return []
    if not header_line:
        return []
    fields = viewer_csv_labels(next(csv.reader([header_line])), features)
    fields = [field.strip() for field in fields if field.strip()]
    if not fields:
        return []
    if fields[0] == "timestamp":
        fields = fields[1:]
    generated_exist_labels = [field for field in fields if re.fullmatch(r"exist_label_\d+", field)]
    scalar_exist_label_alias = generated_exist_labels == ["exist_label_0"] and "exist_label" not in fields
    columns: dict[str, list[str]] = {}
    order: list[str] = []
    for name in fields:
        base = name
        if "_" in name:
            prefix, suffix = name.rsplit("_", 1)
            if suffix.isdigit():
                base = prefix
        if base not in columns:
            columns[base] = []
            order.append(base)
        columns[base].append("exist_label" if scalar_exist_label_alias and name == "exist_label_0" else name)
    return [{"key": key, "value": columns[key]} for key in order]


def _serve_csv_stripped(csv_path: Path, data_version: str | None = None, features: dict | None = None):
    """Serve a CSV file, stripping subtask_state* and normalizing legacy gripper values."""
    with open_cached_csv(csv_path) as stream:
        text = stream.read()
    if not text:
        return Response(text, mimetype="text/csv")

    reader = csv.reader(StringIO(text))
    rows = list(reader)
    if not rows:
        return Response(text, mimetype="text/csv")

    header_fields = viewer_csv_labels(rows[0], features)
    header_changed = header_fields != rows[0]
    rows[0] = header_fields
    drop_idx = {i for i, field in enumerate(header_fields) if field.strip().startswith("subtask_state")}
    normalize_headers = (
        GRIPPER_NORMALIZE_COLUMNS
        if str(data_version or "").upper() == DATA_VERSION_DVT2
        else UMI_GRIPPER_NORMALIZE_COLUMNS
    )
    normalize_idx = {i for i, field in enumerate(header_fields) if field.strip() in normalize_headers}
    generated_exist_labels = [
        field.strip() for field in header_fields if re.fullmatch(r"exist_label_\d+", field.strip())
    ]
    scalar_exist_label_alias = (
        generated_exist_labels == ["exist_label_0"] and "exist_label" not in header_fields
    )
    rename_idx = {
        i
        for i, field in enumerate(header_fields)
        if scalar_exist_label_alias and field.strip() == "exist_label_0"
    }
    if not drop_idx and not normalize_idx and not rename_idx and not header_changed:
        return Response(text, mimetype="text/csv")

    out = StringIO()
    writer = csv.writer(out)
    for row_idx, row in enumerate(rows):
        output_row = []
        for col_idx, value in enumerate(row):
            if col_idx in drop_idx:
                continue
            if row_idx == 0 and col_idx in rename_idx:
                value = "exist_label"
            if row_idx > 0 and col_idx in normalize_idx:
                value = normalize_gripper_csv_value(header_fields[col_idx], value, data_version)
            output_row.append(value)
        writer.writerow(output_row)
    return Response(out.getvalue(), mimetype="text/csv")


def _video_rank(filename: str) -> int:
    from lerobot.data_platform.camera_layout import camera_rank

    return camera_rank(filename)


def _sort_videos_info(videos_info: list[dict]) -> list[dict]:
    # Stable sort: keep original order inside each rank bucket.
    return sorted(videos_info, key=lambda info: _video_rank(info.get("filename", "")))


@dataclass
class JobState:
    """Represents the state of a background job (trim or delete)."""

    job_type: str  # "trim" or "delete"
    episode_id: int
    dataset_key: tuple  # (ns, name)
    status: str  # "running", "done", "error"
    step: int = 0
    message: str = ""
    error: str | None = None


CONSOLE_MODE_FULL = "full"
CONSOLE_MODE_VISUALIZE = "visualize"

_CONSOLE_GROUP_DEFS = [
    {
        "key": "data_platform",
        "label": "Data Platform",
        "pages": [
            {"key": "datasets", "label": "Datasets", "always": True, "tabs": []},
            {
                "key": "mcap_import",
                "label": "Data Import",
                "tabs": [{"key": "mcap_import", "label": "MCAP → LeRobot"}],
            },
            {
                "key": "preprocessing",
                "label": "Preprocessing",
                "tabs": [
                    {"key": "cache", "label": "Cache"},
                    {"key": "standardize", "label": "Standardize"},
                    {"key": "transform", "label": "Transform"},
                    {"key": "split_merge", "label": "Materialize"},
                    {"key": "pipelines", "label": "Pipeline"},
                ],
            },
            {
                "key": "versions",
                "label": "Versions",
                "tabs": [{"key": "versions", "label": "Versions & Lineage"}],
            },
            {"key": "runs", "label": "Pipeline Runs", "always": True, "tabs": []},
        ],
    },
    {
        "key": "data_curation",
        "label": "Data Curation",
        "pages": [
            {
                "key": "explore",
                "label": "Explore",
                "tabs": [
                    {"key": "explore_overview", "label": "Overview"},
                    {"key": "embedding", "label": "Embedding"},
                    {"key": "compare", "label": "Compare"},
                ],
                "open_links": ["viewer", "analysis", "inspect"],
            },
            {
                "key": "quality",
                "label": "Quality",
                "tabs": [
                    {"key": "quality_flags", "label": "Quality Review"},
                    {"key": "quality_report", "label": "Data Report"},
                ],
            },
            {
                "key": "annotation",
                "label": "Annotation",
                "tabs": [
                    {"key": "stage_subtask", "label": "Stage & Subtask"},
                    {"key": "temporal_caption", "label": "Temporal Caption"},
                    {"key": "labeling", "label": "Object Labeling"},
                    {"key": "tagging", "label": "Auto-tagging"},
                ],
            },
            {
                "key": "dataset_build",
                "label": "Dataset Build",
                "tabs": [
                    {"key": "construction", "label": "Data Construction"},
                    {"key": "curation_manifest", "label": "Cohorts & Dataset Build"},
                ],
            },
        ],
    },
]

_LEGACY_CONSOLE_GROUP = {
    "key": "legacy_admin",
    "label": "Admin",
    "pages": [
        {
            "key": "admin_operations",
            "label": "Admin Operations",
            "tabs": [
                {"key": "data_modification", "label": "Data Modification"},
                {"key": "dataset_ops", "label": "In-place Dataset Ops"},
            ],
        },
    ],
}

_CONSOLE_MODE_ALLOWED_TABS = {
    CONSOLE_MODE_FULL: {
        "mcap_import",
        "pipelines",
        "cache",
        "explore_overview",
        "stage_subtask",
        "temporal_caption",
        "quality_flags",
        "quality_report",
        "data_modification",
        "standardize",
        "transform",
        "dataset_ops",
        "split_merge",
        "labeling",
        "tagging",
        "construction",
        "embedding",
        "compare",
        "versions",
        "curation_manifest",
    },
    CONSOLE_MODE_VISUALIZE: {"cache", "explore_overview", "quality_flags", "dataset_ops"},
}

_CONSOLE_MODE_ALLOWED_OPEN_LINKS = {
    CONSOLE_MODE_FULL: {
        "viewer",
        "analysis",
        "inspect",
        "labeling",
        "construction",
        "tagging",
        "embedding",
        "smoothing",
        "compare",
    },
    CONSOLE_MODE_VISUALIZE: {"viewer", "analysis", "inspect"},
}


def _normalize_console_mode(mode: str | None) -> str:
    value = str(mode or CONSOLE_MODE_FULL).strip().lower()
    if value not in _CONSOLE_MODE_ALLOWED_TABS:
        raise ValueError(f"Unsupported console_mode: {mode}")
    return value


def _console_groups_for_tabs(
    allowed_tabs: set[str],
    *,
    allowed_open_links: set[str] | None = None,
    legacy_mutations_enabled: bool = False,
) -> list[dict]:
    allowed_open_links = allowed_open_links or set()
    groups = []
    definitions = list(_CONSOLE_GROUP_DEFS)
    if legacy_mutations_enabled:
        definitions.append(_LEGACY_CONSOLE_GROUP)
    for group in definitions:
        pages = []
        for page in group["pages"]:
            tabs = [tab for tab in page.get("tabs", []) if tab["key"] in allowed_tabs]
            open_links = [key for key in page.get("open_links", []) if key in allowed_open_links]
            if page.get("always") or tabs or open_links:
                pages.append(
                    {
                        "key": page["key"],
                        "label": page["label"],
                        "tabs": tabs,
                        "open_links": open_links,
                    }
                )
        if pages:
            groups.append({"key": group["key"], "label": group["label"], "pages": pages})
    return groups


def run_server(
    dataset: LeRobotDataset | IterableNamespace | MetaOnlyDataset | None,
    episodes: list[int] | None,
    max_frames: int | None,
    prepare_videos: bool,
    downsample: int | None,
    precompute_csv: bool,
    precomputed_only: bool,
    host: str,
    port: str,
    static_folder: Path,
    template_folder: Path,
    annotate: bool = False,
    datasets_root: Path | None = None,
    data_version: str | None = None,
    console_mode: str = CONSOLE_MODE_FULL,
    legacy_mutations_enabled: bool = False,
    protected_source_roots: list[Path] | None = None,
    database_url: str | None = None,
    allow_registration: bool = False,
    remote_cache_root: Path | None = None,
    start_server: bool = True,
) -> Flask:
    console_mode = _normalize_console_mode(console_mode)
    allowed_tabs = set(_CONSOLE_MODE_ALLOWED_TABS[console_mode])
    allowed_open_links = set(_CONSOLE_MODE_ALLOWED_OPEN_LINKS[console_mode])
    control_plane_store = None
    if not database_url:
        allowed_tabs.discard("quality_report")
        allowed_tabs.discard("pipelines")
    if database_url:
        from lerobot.data_platform.control_plane import ControlPlaneStore

        control_plane_store = ControlPlaneStore(database_url)

    def _tab_enabled(tab: str) -> bool:
        return tab in allowed_tabs

    def _open_link_enabled(key: str) -> bool:
        return key in allowed_open_links

    # Mutable container for runtime viewer options controlled by the home console.
    server_state = {
        "annotate": annotate,
        "max_frames": max_frames,
        "downsample": downsample,
        "prepare_videos": prepare_videos,
        "precomputed_only": precomputed_only,
        "data_version": _normalize_data_version(data_version),
    }

    # Multi-dataset registry: datasets_index is lightweight; datasets_registry is loaded metadata.
    datasets_index: dict[tuple[str, str], dict] = {}
    datasets_registry: dict[tuple[str, str], tuple[object, Path]] = {}
    primary_key = tuple(dataset.repo_id.split("/", 1)) if dataset else None
    episodes_by_key: dict[tuple, list] = {}
    _task_caches: dict[tuple, dict] = {}
    jobs_registry: dict[str, dict] = {}
    _jobs_lock = threading.Lock()
    _status_cache_ttl_s = 15.0
    _jsonl_count_cache: dict[Path, tuple[float, tuple[float, int], int]] = {}
    _light_cache_status_cache: dict[tuple[str, str], tuple[float, dict]] = {}
    _construction_status_cache: dict[tuple[str, str], tuple[float, dict]] = {}
    _tagging_status_cache: dict[tuple[str, str], tuple[float, dict]] = {}
    _embedding_count_cache: dict[str, tuple[float, int]] = {}
    _viewer_tag_map_cache: dict[tuple, dict[str, dict[str, list[int]]]] = {}
    _viewer_cache_inventory_cache: dict[Path, tuple[tuple, dict]] = {}
    _issue_episodes_cache: dict[Path, tuple[tuple[str, int, int], set[int]]] = {}
    _flagged_episodes_cache: dict[Path, tuple[tuple[str, int, int], list[int]]] = {}
    _annotation_issues_by_episode_cache: dict[Path, tuple[tuple[str, int, int], dict[int, list[dict]]]] = {}
    _flag_sidecar_json_cache: dict[Path, tuple[tuple[str, int, int], dict]] = {}
    _columns_info_cache: dict[tuple, tuple[list[dict], list[str], list[str]]] = {}
    initial_datasets_root = Path(datasets_root).expanduser() if datasets_root else None
    configured_source_roots = {str(normalized_path(path)) for path in (protected_source_roots or [])}
    registry_state = {
        "datasets_root": initial_datasets_root,
        "path": (
            static_folder / "datasets_registry.json"
            if control_plane_store is not None or initial_datasets_root is None
            else initial_datasets_root / "vis" / "_console" / "static" / "datasets_registry.json"
        ),
        "configured_source_roots": configured_source_roots,
        "protected_source_roots": set(configured_source_roots),
    }

    console_state = ConsoleState(
        light_cache_status_cache=_light_cache_status_cache,
        viewer_tag_map_cache=_viewer_tag_map_cache,
        issue_episodes_cache=_issue_episodes_cache,
        flagged_episodes_cache=_flagged_episodes_cache,
        annotation_issues_by_episode_cache=_annotation_issues_by_episode_cache,
        flag_sidecar_json_cache=_flag_sidecar_json_cache,
        construction_status_cache=_construction_status_cache,
        tagging_status_cache=_tagging_status_cache,
        embedding_count_cache=_embedding_count_cache,
        jsonl_count_cache=_jsonl_count_cache,
        columns_info_cache=_columns_info_cache,
        status_cache_ttl_s=_status_cache_ttl_s,
        task_caches=_task_caches,
        viewer_cache_inventory_cache=_viewer_cache_inventory_cache,
        control_plane_store=control_plane_store,
        dataset=dataset,
        datasets_index=datasets_index,
        datasets_registry=datasets_registry,
        episodes_by_key=episodes_by_key,
        primary_key=primary_key,
        registry_state=registry_state,
        remote_cache_root=remote_cache_root,
        server_state=server_state,
        static_folder=static_folder,
        metadata_factory=MetaOnlyDataset,
    )
    job_service = ConsoleJobService(control_plane_store)
    cache_service = DatasetCacheService(console_state)
    catalog_service = DatasetCatalog(console_state, cache_service)
    presentation_service = DatasetPresentation(
        console_state, cache_service, catalog_service, allowed_tabs, allowed_open_links
    )

    def _admin_auth_store() -> AdminAuthStore:
        registry_path = Path(registry_state["path"])
        return AdminAuthStore(registry_path.parent.parent / "admin_auth.json")

    def _admin_session_token() -> str | None:
        return request.cookies.get(ADMIN_SESSION_COOKIE)

    def _admin_authenticated() -> bool:
        control_user = getattr(g, "control_plane_user", None)
        if control_plane_store is not None:
            return isinstance(control_user, dict) and control_user.get("role") == "admin"
        return _admin_auth_store().verify_session(_admin_session_token())

    # Background job state for non-blocking trim/delete
    _job_state = {"current": None}  # mutable container for JobState | None
    _job_lock = threading.Lock()

    _dataset_protection_for_key = catalog_service.dataset_protection_for_key

    _dataset_is_protected = catalog_service.dataset_is_protected

    def _get_ctx(ns: str, name: str):
        """Get (dataset_obj, static_dir) for given namespace/name, or 404."""
        try:
            return _ensure_dataset_loaded((ns, name))
        except KeyError:
            abort(404)

    _static_dir_for_key = catalog_service.static_dir_for_key

    _dataset_root_for_key = catalog_service.dataset_root_for_key

    _normalize_episode_list = cache_service.normalize_episode_list

    _fallback_manifest_from_cache = cache_service.fallback_manifest_from_cache

    _viewer_cache_inventory = cache_service.viewer_cache_inventory

    _manifest_for_key = catalog_service.manifest_for_key

    _root_has_cache_manifest = cache_service.root_has_cache_manifest

    _dataset_episode_ids = cache_service.dataset_episode_ids

    _viewer_columns_info = cache_service.viewer_columns_info

    _dataset_image_keys = cache_service.dataset_image_keys

    _data_profile_for_dataset = cache_service.data_profile_for_dataset

    _register_remote_cache = catalog_service.register_remote_cache

    _restore_remote_cache_routes = catalog_service.restore_remote_cache_routes

    _dataset_index_uses_v3_adapter = catalog_service.dataset_index_uses_v3_adapter

    _clear_episode_dependent_caches = cache_service.clear_episode_dependent_caches

    _refresh_dataset_after_episode_delete = cache_service.refresh_dataset_after_episode_delete

    _ensure_dataset_loaded = catalog_service.ensure_dataset_loaded

    _register_dataset = catalog_service.register_dataset

    def _asset_url(dataset_namespace: str, dataset_name: str, rel_path: Path | str) -> str:
        return url_for(
            "dataset_static",
            dataset_namespace=dataset_namespace,
            dataset_name=dataset_name,
            filename=Path(rel_path).as_posix(),
        )

    def _static_context_for_key(
        dataset_key: tuple[str, str],
    ) -> tuple[object | None, Path, dict | None, bool]:
        """Return source-dataset context or fall back to cache-only viewer context."""
        try:
            dataset_obj, ds_static = _get_ctx(dataset_key[0], dataset_key[1])
            return dataset_obj, Path(ds_static), None, False
        except Exception:
            manifest, manifest_static = _manifest_for_key(dataset_key)
            if manifest is None or manifest_static is None:
                raise
            return None, Path(manifest_static), manifest, True

    def _cached_video_path(static_dir: Path, image_key: str | None, episode_id: int) -> Path | None:
        static_dir = Path(static_dir)
        image_keys = [image_key] if image_key else []
        if not image_keys:
            videos_dir = static_dir / "videos"
            image_keys = (
                sorted(path.name for path in videos_dir.iterdir() if path.is_dir())
                if videos_dir.is_dir()
                else []
            )
        for candidate_key in image_keys:
            candidate = static_dir / "videos" / candidate_key / f"episode_{int(episode_id):06d}_h264.mp4"
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _jpeg_from_cached_video(video_path: Path, frame_index: int = 0) -> bytes:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCV is required to extract frames from cached video.") from exc
        cap = cv2.VideoCapture(str(video_path))
        try:
            if not cap.isOpened():
                raise RuntimeError(f"Could not open cached video: {video_path}")
            frame_index = max(0, int(frame_index))
            if frame_index:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError(f"Could not read frame {frame_index} from cached video: {video_path}")
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if not ok:
                raise RuntimeError(f"Could not encode frame {frame_index} from cached video: {video_path}")
            return encoded.tobytes()
        finally:
            cap.release()

    _manifest_or_cache_episode_ids = cache_service.manifest_or_cache_episode_ids

    _episode_cache_status = cache_service.episode_cache_status

    _active_tag_variant = cache_service.active_tag_variant

    _serialize_dataset_light = presentation_service.serialize_dataset_light

    _home_url = presentation_service.home_url

    _dataset_nav = presentation_service.dataset_nav

    _job_timing_snapshot = job_service.job_timing_snapshot

    def _serialize_job(job, *, persistent=None):
        from flask import has_request_context

        actor = getattr(g, "control_plane_user", None) if has_request_context() else None
        return job_service.serialize_job(job, persistent=persistent, actor=actor)

    _append_job_log = job_service.append_job_log

    def _append_operation_log(
        static_dir: Path | list[Path] | tuple[Path, ...] | None,
        op: str,
        *,
        dataset_key: tuple[str, str] | str | list[str] | None = None,
        dataset_root: Path | str | list[Path | str] | None = None,
        episode_ids: list[int] | tuple[int, ...] | set[int] | None = None,
        status: str = "success",
        details: dict | None = None,
        phase: str = "result",
        source: str = "web",
        actor: dict | None = None,
        client: dict | None = None,
        event_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> None:
        log_dirs = list(static_dir) if isinstance(static_dir, (list, tuple)) else [static_dir]
        log_dirs.append(static_folder)
        if isinstance(dataset_key, tuple):
            dataset_keys = [_repo_id_from_key(dataset_key)]
        elif isinstance(dataset_key, list):
            dataset_keys = dataset_key
        else:
            dataset_keys = [dataset_key] if dataset_key else []
        if isinstance(dataset_root, list):
            dataset_roots = dataset_root
        else:
            dataset_roots = [dataset_root] if dataset_root is not None else []
        try:
            append_operation_event(
                log_dirs,
                op,
                status=status,
                phase=phase,
                source=source,
                dataset_keys=dataset_keys,
                dataset_roots=dataset_roots,
                episode_ids=episode_ids,
                details=details,
                actor=actor,
                client=client,
                event_id=event_id,
                parent_event_id=parent_event_id,
            )
        except Exception as exc:
            logging.warning("Could not append operation log for %s: %s", op, exc)

    def _audit_identity() -> tuple[dict, dict]:
        local = local_actor()
        control_user = getattr(g, "control_plane_user", None)
        username = (
            (control_user or {}).get("username")
            or request.headers.get("X-Forwarded-User")
            or request.headers.get("X-Remote-User")
            or request.environ.get("REMOTE_USER")
            or local["username"]
        )
        actor = {**local, "username": str(username)}
        if isinstance(control_user, dict):
            actor.update(
                {
                    "user_id": control_user.get("user_id"),
                    "role": control_user.get("role"),
                }
            )
        client = {
            "remote_addr": request.headers.get("X-Forwarded-For") or request.remote_addr,
            "user_agent": request.user_agent.string,
        }
        return actor, client

    def _audit_dataset_keys(body: dict | None = None) -> list[str]:
        body = body or {}
        options = body.get("options") if isinstance(body.get("options"), dict) else {}
        keys = []
        view_args = request.view_args or {}
        namespace = view_args.get("dataset_namespace") or view_args.get("ns")
        name = view_args.get("dataset_name") or view_args.get("name")
        if namespace and name:
            keys.append(f"{namespace}/{name}")
        target = body.get("target") if isinstance(body.get("target"), dict) else {}
        for source in (body, options, target):
            for field in (
                "dataset_key",
                "repo_id",
                "base_key",
                "dataset_key_a",
                "dataset_key_b",
                "compare_with",
            ):
                value = source.get(field)
                if isinstance(value, str) and "/" in value:
                    keys.append(value)
            raw_keys = source.get("src_keys") or []
            if isinstance(raw_keys, str):
                raw_keys = [item.strip() for item in raw_keys.replace("\n", ",").split(",")]
            keys.extend(str(value) for value in raw_keys if isinstance(value, str) and "/" in value)
        return list(dict.fromkeys(keys))

    def _audit_targets(dataset_keys: list[str]) -> tuple[list[Path], list[Path]]:
        static_dirs = []
        roots = []
        for repo_id in dataset_keys:
            try:
                key = _repo_key(repo_id)
            except (KeyError, ValueError):
                continue
            entry = datasets_index.get(key)
            if entry is None:
                continue
            static_dirs.append(Path(entry["output_dir"]).expanduser() / "static")
            roots.append(Path(entry["root"]).expanduser())
        return static_dirs, roots

    def _audit_episode_ids(body: dict | None = None) -> list[int]:
        body = body or {}
        options = body.get("options") if isinstance(body.get("options"), dict) else {}
        values = []
        for source in (request.view_args or {}, body, options):
            for field in ("episode_id", "episode_index", "new_idx"):
                value = source.get(field)
                if value not in (None, ""):
                    with suppress(TypeError, ValueError):
                        values.append(int(value))
            for field in ("episodes", "episode_ids", "delete_episode_ids"):
                with suppress(ValueError):
                    values.extend(_parse_int_list(source.get(field)) or [])
        return sorted(set(values))

    def _audit_job(job: dict, status: str, exc: Exception | None = None) -> None:
        marker = f"{status}:{job.get('finished_at') or job.get('updated_at')}"
        if job.get("_audit_result_marker") == marker:
            return
        job["_audit_result_marker"] = marker
        audit_context = job.get("_audit_context") or {}
        dataset_keys = list(job.get("related_dataset_keys") or [])
        output_dataset_key = job.get("output_dataset_key")
        if output_dataset_key:
            dataset_keys.append(str(output_dataset_key))
        dataset_key = str(job.get("dataset_key") or "")
        if "/" in dataset_key and "," not in dataset_key:
            dataset_keys.append(dataset_key)
        dataset_keys = list(dict.fromkeys(dataset_keys))
        static_dirs, roots = _audit_targets(dataset_keys)
        output_root = job.get("output_root")
        if output_root:
            roots.append(Path(output_root).expanduser())
        details = {
            "job_id": job.get("id"),
            "message": job.get("message"),
            "error": str(exc) if exc is not None else job.get("error"),
            "progress": job.get("progress"),
            "current": job.get("current"),
            "total": job.get("total"),
            "elapsed_seconds": job.get("elapsed_seconds"),
            "output_root": output_root,
            "parameters": audit_context.get("parameters") or {},
        }
        _append_operation_log(
            static_dirs,
            str(job.get("job_type") or "background_job"),
            dataset_key=dataset_keys,
            dataset_root=roots,
            status=status,
            details=details,
            phase="result",
            actor=audit_context.get("actor"),
            client=audit_context.get("client"),
            parent_event_id=audit_context.get("request_event_id"),
        )

    _lifecycle_store = catalog_service.lifecycle_store

    _load_registry = catalog_service.load_registry

    def _visible_dataset_keys():
        return catalog_service.visible_dataset_keys(getattr(g, "control_plane_user", None))

    def _check_edit_permission(static_dir: Path, episode_id):
        """Check if editing is allowed for the given episode."""
        if not server_state["annotate"] and episode_id not in _load_issue_episodes(static_dir):
            return jsonify({"status": "readonly"}), 403
        return None

    class QuietRequestHandler(WSGIRequestHandler):
        def log_request(self, code="-", size="-"):
            return

    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.setLevel(logging.INFO)
    app = Flask(__name__, static_folder=static_folder.resolve(), template_folder=template_folder.resolve())
    app.logger.setLevel(logging.ERROR)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # specifying not to cache
    if control_plane_store is not None:
        from lerobot.data_platform.routes.control_plane import register_control_plane_auth_routes

        register_control_plane_auth_routes(
            app,
            control_plane_store,
            bootstrap_token=os.environ.get("DATA_PLATFORM_BOOTSTRAP_TOKEN", ""),
            allow_registration=allow_registration,
        )
    _load_registry()
    _restore_remote_cache_routes()

    def _admin_login_response(token: str):
        response = jsonify({"status": "ok", "configured": True, "authenticated": True})
        response.set_cookie(
            ADMIN_SESSION_COOKIE,
            token,
            httponly=True,
            secure=request.is_secure,
            samesite="Strict",
        )
        return response

    @app.route("/api/admin/status", methods=["GET"])
    def api_admin_status():
        if control_plane_store is not None:
            user = getattr(g, "control_plane_user", None)
            return jsonify(
                {
                    "configured": True,
                    "authenticated": isinstance(user, dict) and user.get("role") == "admin",
                    "central_account": True,
                }
            )
        store = _admin_auth_store()
        return jsonify(
            {
                "configured": store.is_configured(),
                "authenticated": store.verify_session(_admin_session_token()),
            }
        )

    @app.route("/api/admin/setup", methods=["POST"])
    def api_admin_setup():
        if control_plane_store is not None:
            return jsonify({"error": "Admin access is managed by central platform accounts"}), 409
        body = request.get_json(silent=True) or {}
        password = str(body.get("password") or "")
        if password != str(body.get("confirm_password") or ""):
            return jsonify({"error": "password confirmation does not match"}), 400
        try:
            return _admin_login_response(_admin_auth_store().setup_password(password))
        except ValueError as exc:
            status = 409 if "already configured" in str(exc) else 400
            return jsonify({"error": str(exc)}), status
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/admin/login", methods=["POST"])
    def api_admin_login():
        if control_plane_store is not None:
            return jsonify({"error": "Sign in with a central administrator account"}), 409
        body = request.get_json(silent=True) or {}
        try:
            return _admin_login_response(_admin_auth_store().authenticate(str(body.get("password") or "")))
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/admin/logout", methods=["POST"])
    def api_admin_logout():
        if control_plane_store is not None:
            return jsonify({"error": "Sign out from the central platform account"}), 409
        _admin_auth_store().logout(_admin_session_token())
        response = jsonify({"status": "ok", "configured": _admin_auth_store().is_configured()})
        response.delete_cookie(ADMIN_SESSION_COOKIE, samesite="Strict")
        return response

    @app.route("/api/admin/password", methods=["POST"])
    def api_admin_change_password():
        if control_plane_store is not None:
            return jsonify({"error": "Central account passwords are not managed by Admin Mode"}), 409
        if not _admin_authenticated():
            return jsonify({"error": "Admin Mode is locked"}), 403
        body = request.get_json(silent=True) or {}
        new_password = str(body.get("new_password") or "")
        if new_password != str(body.get("confirm_password") or ""):
            return jsonify({"error": "password confirmation does not match"}), 400
        try:
            token = _admin_auth_store().change_password(
                str(body.get("current_password") or ""),
                new_password,
            )
            return _admin_login_response(token)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500

    @app.before_request
    def _start_operation_audit():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = request.form.to_dict(flat=True) if request.form else {}
        dataset_keys = _audit_dataset_keys(body)
        static_dirs, roots = _audit_targets(dataset_keys)
        actor, client = _audit_identity()
        g.operation_audit = {
            "event_id": uuid.uuid4().hex,
            "started_at": time.perf_counter(),
            "body": summarize_location_sync_request(body)
            if request.path == "/api/agents/locations/sync"
            else sanitize_for_log(body),
            "dataset_keys": dataset_keys,
            "static_dirs": static_dirs,
            "dataset_roots": roots,
            "episode_ids": _audit_episode_ids(body),
            "actor": actor,
            "client": client,
        }
        return None

    @app.after_request
    def _finish_operation_audit(response):
        audit = getattr(g, "operation_audit", None)
        if audit is None:
            return response
        try:
            response_payload = response.get_json(silent=True) if response.is_json else None
            if not should_audit_agent_request(
                request.path,
                response.status_code,
                response_payload,
                catalog_changed=getattr(g, "operation_catalog_changed", True),
            ):
                return response
            job_payload = response_payload.get("job") if isinstance(response_payload, dict) else None
            job_id = (
                (job_payload.get("id") or job_payload.get("job_id"))
                if isinstance(job_payload, dict)
                else None
            )
            audit_context = {
                "actor": audit["actor"],
                "client": audit["client"],
                "parameters": audit["body"],
                "request_event_id": audit["event_id"],
            }
            if job_id:
                with _jobs_lock:
                    job = jobs_registry.get(str(job_id))
                    if job is not None:
                        job["_audit_context"] = audit_context
                        completed_status = job.get("status")
                if job is not None and completed_status in {"done", "error"}:
                    job.pop("_audit_result_marker", None)
                    _audit_job(job, "success" if completed_status == "done" else "failed")
            if response.status_code >= 400:
                status = "failed"
            elif job_id or response.is_streamed:
                status = "accepted"
            else:
                status = "success"
            response_summary = {}
            if isinstance(response_payload, dict):
                for field in ("status", "error", "message", "dataset_key"):
                    if field in response_payload:
                        response_summary[field] = response_payload[field]
            _append_operation_log(
                audit["static_dirs"],
                request.endpoint or f"{request.method} {request.path}",
                dataset_key=audit["dataset_keys"],
                dataset_root=audit["dataset_roots"],
                episode_ids=audit["episode_ids"],
                status=status,
                phase="request",
                details={
                    "method": request.method,
                    "path": request.path,
                    "query": request.args.to_dict(flat=False),
                    "parameters": audit["body"],
                    "status_code": response.status_code,
                    "job_id": job_id,
                    "response": response_summary,
                    "duration_ms": round((time.perf_counter() - audit["started_at"]) * 1000),
                },
                actor=audit["actor"],
                client=audit["client"],
                event_id=audit["event_id"],
            )
        except Exception:
            logging.exception("Failed to record operation audit event")
        return response

    @app.before_request
    def _enforce_console_mode():
        if console_mode == CONSOLE_MODE_FULL:
            return None

        path = request.path
        blocked_prefixes = []
        if not _tab_enabled("labeling"):
            blocked_prefixes.append("/api/labeling")
        if not _tab_enabled("construction"):
            blocked_prefixes.append("/api/construction")
        if not _tab_enabled("tagging"):
            blocked_prefixes.append("/api/tagging")
        if not _tab_enabled("embedding"):
            blocked_prefixes.append("/api/embedding")
        if not _tab_enabled("compare"):
            blocked_prefixes.append("/api/compare")
        if not any(
            _tab_enabled(tab)
            for tab in (
                "standardize",
                "transform",
                "dataset_ops",
                "split_merge",
                "quality_flags",
                "data_modification",
            )
        ):
            blocked_prefixes.append("/api/preprocess")
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in blocked_prefixes):
            abort(404)

        if console_mode == CONSOLE_MODE_VISUALIZE and path.startswith("/api/preprocess"):
            allow_visualize_preprocess = (
                path == "/api/preprocess/capabilities"
                or path == "/api/preprocess/flag_fixes/start"
                or path == "/api/preprocess/quality_flags/start"
                or path == "/api/preprocess/lowercase_prompts/start"
                or path == "/api/preprocess/clear_flags/start"
                or path == "/api/preprocess/delete_episodes/start"
            )
            if not allow_visualize_preprocess:
                abort(404)

        blocked_pages = []
        if not _tab_enabled("labeling"):
            blocked_pages.append("labeling")
        if not _tab_enabled("construction"):
            blocked_pages.append("construction")
        if not _tab_enabled("tagging"):
            blocked_pages.append("tagging")
        if not _tab_enabled("embedding"):
            blocked_pages.append("embedding")
        if not _tab_enabled("compare"):
            blocked_pages.append("compare")
        if not _open_link_enabled("smoothing"):
            blocked_pages.append("smoothing")
        if blocked_pages and re.match(r"^/[^/]+/[^/]+/(" + "|".join(blocked_pages) + r")(/|$)", path):
            abort(404)

        return None

    def _request_is_legacy_mutation(path: str, body: dict) -> bool:
        blocked_exact = {
            "/api/preprocess/apply_prompt_assignments/start",
            "/api/preprocess/delete_episodes/start",
            "/api/preprocess/fix_prompt_prepositions/start",
            "/api/preprocess/flag_fixes/start",
            "/api/preprocess/lowercase_prompts/start",
            "/api/preprocess/repair_v3_video_timestamps/start",
            "/api/preprocess/rewrite_prompts/start",
        }
        blocked_suffixes = (
            "/delete_episode",
            "/finalize",
            "/merge",
            "/subtask_merge_parquet",
            "/trim_merge",
        )
        should_block = path in blocked_exact or path.endswith(blocked_suffixes)
        if path == "/api/preprocess/value_edit/start":
            options = body.get("options") or body
            should_block = str(options.get("output_mode") or "new_dataset") == "in_place"
        if path == "/api/precompute/start":
            options = body.get("options") or body
            mutation_keys = {
                "fix_episode_indices",
                "write_parquet",
                "write_subtask",
                "overwrite_parquet",
                "overwrite_subtask_text",
            }
            should_block = any(bool(options.get(key)) for key in mutation_keys)
        return should_block

    def _request_dataset_key(path: str, body: dict) -> tuple[str, str] | None:
        value = str(body.get("dataset_key") or body.get("repo_id") or "").strip()
        if value:
            return _repo_key(value)
        api_match = re.match(r"^/api/(?:construction|labeling|tagging)/([^/]+)/([^/]+)(?:/|$)", path)
        if api_match:
            return api_match.group(1), api_match.group(2)
        viewer_match = re.match(r"^/([^/]+)/([^/]+)(?:/|$)", path)
        if viewer_match and viewer_match.group(1) != "api":
            return viewer_match.group(1), viewer_match.group(2)
        return None

    @app.before_request
    def _guard_legacy_in_place_mutations():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        path = request.path
        body = request.get_json(silent=True) or {}
        if _request_is_legacy_mutation(path, body) and not _admin_authenticated():
            if control_plane_store is not None:
                message = "An administrator account is required for in-place or delete operations."
            else:
                message = (
                    "Admin Mode is locked. Unlock it from the page with the configured "
                    "administrator password before running an in-place mutation."
                )
            return (
                jsonify({"error": message}),
                403,
            )
        return None

    @app.before_request
    def _guard_protected_source_mutations():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        path = request.path
        body = request.get_json(silent=True) or {}
        if not _request_is_legacy_mutation(path, body):
            return None
        dataset_key = _request_dataset_key(path, body)
        if dataset_key is None or dataset_key not in datasets_index:
            return None
        protection = _dataset_protection_for_key(dataset_key)
        if protection["protected"]:
            reason_text = ", ".join(
                str(item.get("value") or item.get("type")) for item in protection["reasons"]
            )
            return (
                jsonify(
                    {
                        "error": (
                            f"Protected source dataset cannot be modified in place: "
                            f"{_repo_id_from_key(dataset_key)} ({reason_text}). "
                            "Create a sibling dataset version instead."
                        ),
                        "protection": protection,
                    }
                ),
                403,
            )
        return None

    # Register primary dataset if provided
    if dataset is not None and primary_key:
        _register_dataset(dataset, static_folder.parent, episodes)

    @app.route("/assets/<string:dataset_namespace>/<string:dataset_name>/<path:filename>")
    def dataset_static(dataset_namespace, dataset_name, filename):
        ds_static = _static_dir_for_key((dataset_namespace, dataset_name))
        if ds_static is None:
            abort(404)
        if filename.startswith("csv/") and filename.endswith(".csv"):
            from werkzeug.utils import safe_join

            joined = safe_join(str(ds_static), filename)
            if joined is None:
                abort(404)
            path = resolve_cached_csv(Path(joined))
            if not path.is_file() or not path.resolve().is_relative_to(ds_static.resolve()):
                abort(404)
            with open_cached_csv(path) as stream:
                return Response(stream.read(), mimetype="text/csv")
        return send_from_directory(ds_static, filename)

    @app.route("/")
    def hommepage(dataset=dataset):
        dataset_param, episode_param = None, None
        all_params = request.args
        if "dataset" in all_params:
            dataset_param = all_params["dataset"]
        if "episode" in all_params:
            episode_param = int(all_params["episode"])

        if dataset_param:
            dataset_namespace, dataset_name = dataset_param.split("/", 1)
            return redirect(
                url_for(
                    "show_episode",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                    episode_id=episode_param if episode_param is not None else 0,
                )
            )

        return render_template(
            "visualize_dataset_homepage.html",
            initial_datasets=[_serialize_dataset_light(key) for key in _visible_dataset_keys()],
            datasets_root=str(registry_state["datasets_root"]) if registry_state["datasets_root"] else "",
            lerobot_datasets=available_datasets,
            initial_selected_dataset=all_params.get("select", ""),
            initial_page=all_params.get("page", ""),
            initial_tab=all_params.get("tab", ""),
            console_mode=console_mode,
            console_groups=_console_groups_for_tabs(
                allowed_tabs,
                allowed_open_links=allowed_open_links,
                legacy_mutations_enabled=True,
            ),
            allowed_tabs=sorted(allowed_tabs),
            allowed_open_links=sorted(allowed_open_links),
            legacy_mutations_enabled=True,
            admin_authenticated=_admin_authenticated(),
            remote_source_mutations_enabled=legacy_mutations_enabled,
            dataset_access_restricted=bool(
                control_plane_store is not None
                and control_plane_store.restricted_dataset_keys(g.control_plane_user["user_id"]) is not None
            ),
            control_plane_enabled=control_plane_store is not None,
            control_plane_user=getattr(g, "control_plane_user", None),
        )

    @app.route("/api/labeling/capabilities")
    def api_labeling_capabilities():
        return jsonify(get_labeling_capabilities())

    @app.route("/api/jobs")
    def api_jobs():
        if control_plane_store is not None:
            from lerobot.data_platform.local_execution import local_job_payload

            rows = [
                row
                for row in control_plane_store.list_jobs(
                    limit=200, actor=getattr(g, "control_plane_user", None) or {}
                )
                if row["operation"].startswith("local.request.")
                or row.get("options", {}).get("local_compute")
            ]
            rows = control_plane_store.job_manager.decorate_many(
                rows[:20], getattr(g, "control_plane_user", None)
            )
            return jsonify(
                {
                    "jobs": [
                        _serialize_job(
                            {**local_job_payload(control_plane_store, row), "persistent_job_id": None},
                            persistent=row,
                        )
                        for row in rows
                    ]
                }
            )
        with _jobs_lock:
            jobs = sorted(jobs_registry.values(), key=lambda item: item["created_at"], reverse=True)
            return jsonify({"jobs": [_serialize_job(job) for job in jobs[:20]]})

    @app.route("/api/jobs/<string:job_id>")
    def api_job(job_id):
        if control_plane_store is not None:
            from lerobot.data_platform.local_execution import local_job_payload

            try:
                row = control_plane_store.get_job(job_id)
                if row["operation"].startswith("local.request.") or row.get("options", {}).get(
                    "local_compute"
                ):
                    return jsonify(
                        {
                            "job": _serialize_job(
                                {**local_job_payload(control_plane_store, row), "persistent_job_id": None}
                            )
                        }
                    )
            except KeyError:
                pass
        with _jobs_lock:
            job = jobs_registry.get(job_id)
            if job is None:
                return jsonify({"error": "job not found"}), 404
            return jsonify({"job": _serialize_job(job)})

    @app.route("/api/viewer/<string:dataset_namespace>/<string:dataset_name>/task_map")
    def api_viewer_task_map(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        try:
            dataset_obj, _ = _ensure_dataset_loaded(dataset_key)
        except KeyError:
            manifest, _ = _manifest_for_key(dataset_key)
            if manifest:
                return jsonify({"task_episode_map": manifest_task_episode_map(manifest)})
            return jsonify({"error": f"dataset is not registered: {_repo_id_from_key(dataset_key)}"}), 404
        except Exception as exc:
            manifest, _ = _manifest_for_key(dataset_key)
            if manifest:
                return jsonify({"task_episode_map": manifest_task_episode_map(manifest)})
            logging.exception("Failed to load task map for %s", _repo_id_from_key(dataset_key))
            return jsonify({"error": str(exc)}), 400

        task_episode_map = _task_caches.setdefault(dataset_key, {})
        if not task_episode_map and hasattr(dataset_obj, "meta") and hasattr(dataset_obj.meta, "episodes"):
            for ep_id in _dataset_episode_ids(dataset_obj, dataset_key):
                ep_info = dataset_obj.meta.episodes.get(ep_id, {})
                for task in ep_info.get("tasks", []):
                    task_episode_map.setdefault(task, []).append(ep_id)
        return jsonify({"task_episode_map": task_episode_map})

    _file_signature = cache_service.file_signature

    _viewer_tag_episode_map = cache_service.viewer_tag_episode_map

    @app.route("/api/viewer/<string:dataset_namespace>/<string:dataset_name>/tag_map")
    def api_viewer_tag_map(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        try:
            dataset_obj, ds_static = _ensure_dataset_loaded(dataset_key)
        except KeyError:
            manifest, ds_static = _manifest_for_key(dataset_key)
            if not manifest or ds_static is None:
                return jsonify({"error": f"dataset is not registered: {_repo_id_from_key(dataset_key)}"}), 404
            dataset_obj = None
            episode_ids = manifest_episode_ids(manifest)
        except Exception as exc:
            manifest, ds_static = _manifest_for_key(dataset_key)
            if not manifest or ds_static is None:
                logging.exception("Failed to load tag map for %s", _repo_id_from_key(dataset_key))
                return jsonify({"error": str(exc)}), 400
            dataset_obj = None
            episode_ids = manifest_episode_ids(manifest)
        else:
            episode_ids = _dataset_episode_ids(dataset_obj, dataset_key)

        tag_episode_map = _viewer_tag_episode_map(Path(ds_static), episode_ids, dataset_obj)
        return jsonify({"tag_episode_map": tag_episode_map})

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/labeling")
    def show_labeling(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        dataset_obj, ds_static, manifest, cache_only = _static_context_for_key(dataset_key)
        repo_id = _repo_id_from_key(dataset_key)
        episode_ids = (
            _dataset_episode_ids(dataset_obj, dataset_key)
            if dataset_obj is not None
            else _manifest_or_cache_episode_ids(manifest, ds_static)
        )
        labeling_dir = ds_static / "labeling"
        label_variants = available_label_variants(labeling_dir)
        requested_variant = request.args.get("variant") or None
        active_variant = requested_variant
        if label_variants and active_variant not in {variant.get("id") for variant in label_variants}:
            active_variant = label_variants[0].get("id") or None

        initial_labeling_episodes = []
        prompt_options = []
        counts = {"hi": 0, "md": 0, "lo": 0, "reviewed": 0, "total": 0, "skipped": 0}
        labels_file = resolved_labels_path(labeling_dir, active_variant)
        reviewed_file = resolved_reviewed_path(labeling_dir, active_variant)
        reviewed = load_labels_jsonl(reviewed_file)
        originals_count = 0
        if labels_file.is_file() or reviewed:
            originals = load_labels_jsonl(labels_file)
            if not originals and reviewed:
                originals = reviewed
            originals_count = len(originals)
            prompt_set = set()
            for episode_index, original in originals.items():
                uncertainty = labeling_uncertainty(original)
                if uncertainty < 0:
                    counts["skipped"] += 1
                    continue
                if uncertainty >= 90:
                    level = "hi"
                elif uncertainty >= 50:
                    level = "md"
                else:
                    level = "lo"
                current = reviewed.get(episode_index, original)
                task = original.get("task") or ""
                prompt_set.add(task)
                counts[level] += 1
                counts["total"] += 1
                if episode_index in reviewed:
                    counts["reviewed"] += 1
                initial_labeling_episodes.append(
                    {
                        "episode_index": episode_index,
                        "task": task,
                        "uncertainty": uncertainty,
                        "reason": labeling_reason(original),
                        "level": level,
                        "reviewed": episode_index in reviewed,
                        "manual": bool(current.get("manual")),
                    }
                )
            initial_labeling_episodes.sort(
                key=lambda item: (
                    {"hi": 0, "md": 1, "lo": 2}.get(item["level"], 9),
                    item["episode_index"],
                )
            )
            prompt_options = sorted(prompt_set)
        labeling_debug = {
            "labeling_dir": str(labeling_dir),
            "active_variant": active_variant or "latest",
            "labels_path": str(labels_file),
            "labels_exists": labels_file.is_file(),
            "labels_count": originals_count,
            "reviewed_path": str(reviewed_file),
            "reviewed_exists": reviewed_file.is_file(),
            "reviewed_count": len(reviewed),
            "variants": label_variants,
        }
        return render_template(
            "visualize_dataset_labeling.html",
            dataset_namespace=dataset_namespace,
            dataset_name=dataset_name,
            dataset_key=repo_id,
            labeling_variants=label_variants,
            labeling_active_variant=active_variant or "",
            labeling_initial_episodes=initial_labeling_episodes,
            labeling_prompt_options=prompt_options,
            labeling_counts=counts,
            labeling_debug=labeling_debug,
            cache_only=cache_only,
            flagged_url=url_for(
                "get_flagged_episodes",
                dataset_namespace=dataset_namespace,
                dataset_name=dataset_name,
            ),
            task_assignment_save_url=url_for(
                "save_viewer_task_assignment",
                dataset_namespace=dataset_namespace,
                dataset_name=dataset_name,
            ),
            tagging_api_base_url=f"/api/tagging/{repo_id}",
            legacy_mutations_enabled=(_admin_authenticated() and not _dataset_is_protected(dataset_key)),
            viewer_url=f"/{repo_id}/episode_{episode_ids[0] if episode_ids else 0}",
            **_dataset_nav(
                repo_id,
                episode_ids[0] if episode_ids else 0,
                "labeling",
                dataset_obj,
                ds_static,
                cache_only=cache_only,
                manifest=manifest,
            ),
        )

    @app.route("/api/labeling/<string:dataset_namespace>/<string:dataset_name>/episodes")
    def api_labeling_episodes(dataset_namespace, dataset_name):
        _, ds_static, _, _ = _static_context_for_key((dataset_namespace, dataset_name))
        labeling_dir = ds_static / "labeling"
        variant = request.args.get("variant") or None
        labels_file = resolved_labels_path(labeling_dir, variant)
        reviewed = load_labels_jsonl(resolved_reviewed_path(labeling_dir, variant))
        if not labels_file.is_file() and not reviewed:
            return jsonify({"error": f"labels not found: {labels_file}"}), 404
        originals = load_labels_jsonl(labels_file)
        if not originals and reviewed:
            originals = reviewed
        out = []
        for episode_index, original in originals.items():
            current = reviewed.get(episode_index, original)
            out.append(
                {
                    "episode_index": episode_index,
                    "task": original.get("task"),
                    "uncertainty": labeling_uncertainty(original),
                    "reason": labeling_reason(original),
                    "reviewed": episode_index in reviewed,
                    "manual": bool(current.get("manual")),
                }
            )
        out.sort(key=lambda item: (-item["uncertainty"], item["episode_index"]))
        return jsonify(out)

    @app.route("/api/labeling/<string:dataset_namespace>/<string:dataset_name>/variants")
    def api_labeling_variants(dataset_namespace, dataset_name):
        _, ds_static, _, _ = _static_context_for_key((dataset_namespace, dataset_name))
        variants = available_label_variants(ds_static / "labeling")
        return jsonify({"variants": variants, "default_variant": variants[0]["id"] if variants else None})

    def _labeling_image_key(ds_static: Path, variant: str | None = None) -> str | None:
        source_file = source_path(ds_static / "labeling", variant)
        if not source_file.is_file():
            source_file = source_path(ds_static / "labeling")
        if not source_file.is_file():
            return None
        try:
            return json.loads(source_file.read_text()).get("image_key") or None
        except (json.JSONDecodeError, OSError, AttributeError):
            return None

    @app.route("/api/labeling/<string:dataset_namespace>/<string:dataset_name>/debug")
    def api_labeling_debug(dataset_namespace, dataset_name):
        _, ds_static, _, _ = _static_context_for_key((dataset_namespace, dataset_name))
        labeling_dir = ds_static / "labeling"
        variant = request.args.get("variant") or None
        labels_file = resolved_labels_path(labeling_dir, variant)
        reviewed_file = resolved_reviewed_path(labeling_dir, variant)
        originals = load_labels_jsonl(labels_file)
        reviewed = load_labels_jsonl(reviewed_file)
        return jsonify(
            {
                "labeling_dir": str(labeling_dir),
                "variant": variant or "latest",
                "labels_path": str(labels_file),
                "labels_exists": labels_file.is_file(),
                "labels_count": len(originals),
                "reviewed_path": str(reviewed_file),
                "reviewed_exists": reviewed_file.is_file(),
                "reviewed_count": len(reviewed),
                "variants": available_label_variants(labeling_dir),
                "files": sorted(path.name for path in labeling_dir.glob("labels*.jsonl")),
            }
        )

    @app.route("/api/labeling/<string:dataset_namespace>/<string:dataset_name>/episode/<int:episode_index>")
    def api_labeling_episode(dataset_namespace, dataset_name, episode_index):
        dataset_obj, ds_static, manifest, _ = _static_context_for_key((dataset_namespace, dataset_name))
        labeling_dir = ds_static / "labeling"
        variant = request.args.get("variant") or None
        record = load_labeling_episode_record(labeling_dir, episode_index, variant=variant)
        if record is None:
            return jsonify({"error": "not found"}), 404
        image_key = _labeling_image_key(ds_static, variant)
        episode_info = {}
        if dataset_obj is not None:
            episode_info = getattr(dataset_obj.meta, "episodes", {}).get(episode_index)
            if episode_info is None:
                episode_info = getattr(dataset_obj.meta, "episodes", {}).get(str(episode_index))
        elif manifest is not None:
            episode_info = manifest_episode_info(manifest, episode_index)
        try:
            record["episode_length"] = int((episode_info or {}).get("length") or 1)
        except (TypeError, ValueError):
            record["episode_length"] = 1
        record["image_key"] = image_key
        try:
            if dataset_obj is not None:
                record["fps"] = float(
                    getattr(dataset_obj.meta, "fps", None) or dataset_obj.meta.info.get("fps") or 30
                )
            else:
                record["fps"] = float((manifest or {}).get("fps") or 30)
        except (TypeError, ValueError, AttributeError):
            record["fps"] = 30.0
        video_args = {
            "dataset_namespace": dataset_namespace,
            "dataset_name": dataset_name,
            "episode_index": episode_index,
        }
        if variant:
            video_args["variant"] = variant
        record["video_url"] = url_for("api_labeling_video", **video_args)
        if _bool_option(request.args, "compare", False):
            compared = []
            for item in available_label_variants(labeling_dir):
                candidate = load_labeling_episode_record(labeling_dir, episode_index, variant=item["id"])
                if candidate is None:
                    continue
                compared.append(
                    {
                        "id": item["id"],
                        "label": item["label"],
                        "is_latest": item.get("is_latest", False),
                        "reviewed": candidate.get("reviewed", False),
                        "reason": candidate.get("reason"),
                        "original": candidate["original"],
                        "current": candidate["current"],
                    }
                )
            record["variants"] = compared
        return jsonify(record)

    @app.route("/api/labeling/<string:dataset_namespace>/<string:dataset_name>/image/<int:episode_index>")
    def api_labeling_image(dataset_namespace, dataset_name, episode_index):
        dataset_obj, ds_static, manifest, _ = _static_context_for_key((dataset_namespace, dataset_name))
        try:
            frame_index = max(0, int(request.args.get("frame", 0)))
        except (TypeError, ValueError):
            frame_index = 0
        variant = request.args.get("variant") or None
        image_key = _labeling_image_key(ds_static, variant)
        try:
            if dataset_obj is None:
                if not image_key:
                    image_keys = list((manifest or {}).get("image_keys") or [])
                    image_key = image_keys[0] if image_keys else None
                video_path = _cached_video_path(ds_static, image_key, episode_index)
                if video_path is None:
                    return "Cached labeling video not found.", 404
                image_bytes = _jpeg_from_cached_video(video_path, frame_index)
            elif frame_index <= 0:
                image_bytes = read_first_frame_jpeg(
                    dataset_obj.root,
                    dataset_obj.meta,
                    episode_index,
                    image_key=image_key,
                )
            else:
                image_pil, _ = read_frame_image(
                    dataset_obj.root,
                    dataset_obj.meta,
                    episode_index,
                    frame_index,
                    image_key=image_key,
                )
                out = BytesIO()
                image_pil.save(out, format="JPEG", quality=92)
                out.seek(0)
                image_bytes = out.getvalue()
        except Exception as exc:
            logging.exception("Failed to serve object labeling image for episode %s", episode_index)
            return str(exc), 404
        return send_file(BytesIO(image_bytes), mimetype="image/jpeg")

    @app.route("/api/labeling/<string:dataset_namespace>/<string:dataset_name>/video/<int:episode_index>")
    def api_labeling_video(dataset_namespace, dataset_name, episode_index):
        dataset_obj, ds_static, manifest, _ = _static_context_for_key((dataset_namespace, dataset_name))
        variant = request.args.get("variant") or None
        image_key = _labeling_image_key(ds_static, variant)
        if not image_key:
            if dataset_obj is not None:
                image_keys = [
                    key
                    for key, feature in getattr(dataset_obj.meta, "features", {}).items()
                    if feature.get("dtype") == "image"
                ]
            else:
                image_keys = list((manifest or {}).get("image_keys") or [])
            image_key = image_keys[0] if image_keys else None
        if not image_key:
            return "No image key available for labeling video.", 404
        cached_video = _cached_video_path(ds_static, image_key, episode_index)
        if cached_video is not None:
            return redirect(_asset_url(dataset_namespace, dataset_name, cached_video.relative_to(ds_static)))
        if dataset_obj is None:
            return "Cached labeling video not found.", 404
        try:
            out_path = encode_episode_video(
                dataset_obj.root,
                dataset_obj.meta,
                int(episode_index),
                image_key,
                ds_static,
                max_frames=None,
                overwrite=False,
            )
        except Exception as exc:
            logging.exception("Failed to encode object labeling video for episode %s", episode_index)
            return str(exc), 404
        if out_path is None or not Path(out_path).is_file():
            return "Could not prepare labeling video.", 404
        try:
            rel_path = Path(out_path).relative_to(ds_static)
        except ValueError:
            logging.error("Labeling video path is outside static dir: %s", out_path)
            return "Invalid labeling video path.", 404
        return redirect(_asset_url(dataset_namespace, dataset_name, rel_path))

    @app.route(
        "/api/labeling/<string:dataset_namespace>/<string:dataset_name>/save/<int:episode_index>",
        methods=["POST"],
    )
    def api_labeling_save(dataset_namespace, dataset_name, episode_index):
        _, ds_static, _, _ = _static_context_for_key((dataset_namespace, dataset_name))
        labeling_dir = ds_static / "labeling"
        variant = request.args.get("variant") or None
        originals = load_labels_jsonl(resolved_labels_path(labeling_dir, variant))
        existing_reviewed = load_labels_jsonl(resolved_reviewed_path(labeling_dir, variant))
        original = originals.get(episode_index) or existing_reviewed.get(episode_index)
        if original is None:
            return jsonify({"error": "not found"}), 404
        body = request.get_json(force=True)
        new_record = dict(original)
        new_record["selected"] = body.get("selected")
        new_record["selected_target"] = body.get("selected")
        new_record["manual"] = bool(body.get("manual"))
        new_record["reviewed"] = True
        if isinstance(body.get("detections_all_target"), list):
            new_record["detections_all_target"] = body["detections_all_target"]
            new_record["detections_target"] = body["detections_all_target"]
        elif isinstance(body.get("detections_target"), list):
            new_record["detections_target"] = body["detections_target"]
            new_record["detections_all_target"] = body["detections_target"]
        if isinstance(body.get("detections_ref"), list):
            new_record["detections_ref"] = body["detections_ref"]
        if body.get("relation_satisfied") is not None:
            new_record["relation_satisfied"] = body["relation_satisfied"]
        save_reviewed_record_for_variant(labeling_dir, episode_index, new_record, variant=variant)
        return jsonify({"ok": True})

    @app.route(
        "/api/labeling/<string:dataset_namespace>/<string:dataset_name>/reset/<int:episode_index>",
        methods=["POST"],
    )
    def api_labeling_reset(dataset_namespace, dataset_name, episode_index):
        _, ds_static, _, _ = _static_context_for_key((dataset_namespace, dataset_name))
        remove_reviewed_record_for_variant(
            ds_static / "labeling",
            episode_index,
            variant=request.args.get("variant") or None,
        )
        return jsonify({"ok": True})

    @app.route(
        "/api/labeling/<string:dataset_namespace>/<string:dataset_name>/merge",
        methods=["POST"],
    )
    def api_labeling_merge(dataset_namespace, dataset_name):
        dataset_obj, ds_static = _get_ctx(dataset_namespace, dataset_name)
        body = request.get_json(silent=True) or {}
        if _bool_option(body, "annotate", False):
            server_state["annotate"] = True

        labeling_dir = ds_static / "labeling"
        variant = body.get("variant") or request.args.get("variant") or None
        reviewed_file = resolved_reviewed_path(labeling_dir, variant)
        reviewed = load_labels_jsonl(reviewed_file)
        if not reviewed:
            return jsonify({"error": f"reviewed labels not found: {reviewed_file}"}), 400
        for episode_index in sorted(reviewed):
            denied = _check_edit_permission(ds_static, episode_index)
            if denied:
                return denied

        try:
            result = merge_reviewed_labels_to_metadata(dataset_obj.root, labeling_dir, variant=variant)
            refreshed = MetaOnlyDataset(dataset_obj.repo_id, root=dataset_obj.root)
            _register_dataset(refreshed, ds_static.parent)
        except Exception as exc:
            logging.exception("Failed to merge object labels for %s/%s", dataset_namespace, dataset_name)
            return jsonify({"error": str(exc)}), 500

        return jsonify({"status": "ok", **result})

    def _dataset_key_from_body(body: dict) -> tuple[str, str]:
        dataset_key_value = str(body.get("dataset_key") or body.get("repo_id") or "").strip()
        if dataset_key_value:
            return _repo_key(dataset_key_value)
        namespace = str(body.get("ns") or body.get("namespace") or "").strip()
        name = str(body.get("name") or "").strip()
        if namespace and name:
            return namespace, name
        raise ValueError("dataset_key is required")

    _unregister_missing_dataset = catalog_service.unregister_missing_dataset

    route_context = RouteContext(
        datasets_index=datasets_index,
        jobs_registry=jobs_registry,
        jobs_lock=_jobs_lock,
        tagging_status_cache=_tagging_status_cache,
        repo_id_from_key=_repo_id_from_key,
        repo_key=_repo_key,
        get_ctx=_get_ctx,
        ensure_dataset_loaded=_ensure_dataset_loaded,
        register_dataset=_register_dataset,
        dataset_episode_ids=_dataset_episode_ids,
        dataset_image_keys=_dataset_image_keys,
        dataset_nav=_dataset_nav,
        serialize_dataset_light=_serialize_dataset_light,
        serialize_job=_serialize_job,
        append_job_log=_append_job_log,
        job_timing_snapshot=_job_timing_snapshot,
        parse_int_list=_parse_int_list,
        parse_str_list=_parse_str_list,
        bool_option=_bool_option,
        dataset_key_from_body=_dataset_key_from_body,
        active_tag_variant=_active_tag_variant,
        meta_only_dataset_cls=MetaOnlyDataset,
        append_operation_log=_append_operation_log,
        audit_job=_audit_job,
        clear_dataset_caches=_clear_episode_dependent_caches,
        refresh_dataset_after_episode_delete=_refresh_dataset_after_episode_delete,
        static_dir_for_key=_static_dir_for_key,
        lifecycle_store=_lifecycle_store,
        control_plane_store=control_plane_store,
        legacy_mutations_enabled=True,
        dataset_is_protected=_dataset_is_protected,
        unregister_missing_dataset=_unregister_missing_dataset,
    )
    route_context.console_submissions = ConsoleSubmissionService(
        route_context,
        catalog_service,
        cache_service,
        console_mode,
        _tab_enabled,
        run_precompute,
        run_v3_viewer_precompute,
        run_labeling,
        delete_episodes_inplace,
        threading.Thread,
        episodes,
    )
    route_context.dataset_catalog = catalog_service
    route_context.dataset_presentation = presentation_service
    route_context.admin_authenticated = _admin_authenticated
    register_console_dataset_routes(app, route_context)
    register_console_job_routes(app, route_context)
    app.extensions["data_platform_console_state"] = console_state
    app.extensions["data_platform_catalog"] = catalog_service
    app.extensions["data_platform_route_context"] = route_context
    app.config["DATA_PLATFORM_CONSOLE_MODE"] = console_mode
    app.config["DATA_PLATFORM_LEGACY_MUTATIONS"] = legacy_mutations_enabled
    if control_plane_store is not None:
        from lerobot.data_platform.routes.dataset_scans import register_dataset_scan_routes

        register_dataset_scan_routes(app, route_context)
        from lerobot.data_platform.routes.processing import register_processing_routes

        register_processing_routes(app, route_context)

    _complete_local_execution = catalog_service.complete_local_execution

    app.extensions["data_platform_complete_local"] = _complete_local_execution
    if control_plane_store is not None:
        from lerobot.data_platform.routes.control_plane import register_control_plane_routes

        cache_root = (
            Path(remote_cache_root).expanduser()
            if remote_cache_root is not None
            else Path(registry_state["path"]).parent.parent / "remote_cache"
        )
        register_control_plane_routes(
            app,
            control_plane_store,
            enrollment_token=os.environ.get("DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN", ""),
            remote_cache_root=cache_root,
            register_remote_cache=_register_remote_cache,
            legacy_mutations_enabled=legacy_mutations_enabled,
            task_catalog_store=_lifecycle_store().tasks,
        )
        console_state.remote_cache_root = cache_root
        route_context.remote_cache_root = cache_root
        from lerobot.data_platform.routes import register_source_cleanup_routes

        register_source_cleanup_routes(app, route_context)
    if _open_link_enabled("inspect"):
        register_inspect_routes(app, route_context)
    if console_mode == CONSOLE_MODE_FULL:
        from lerobot.data_platform.routes.curation import register_curation_routes

        register_curation_routes(app, route_context)
    if _tab_enabled("temporal_caption"):
        register_temporal_caption_routes(app, route_context)
    if any(_tab_enabled(tab) for tab in ("versions", "curation_manifest")):
        register_lifecycle_routes(app, route_context)
        register_task_routes(app, route_context)
    if any(
        _tab_enabled(tab)
        for tab in (
            "standardize",
            "transform",
            "dataset_ops",
            "split_merge",
            "quality_flags",
            "data_modification",
        )
    ):
        register_preprocess_routes(app, route_context)
    if _tab_enabled("construction"):
        register_construction_routes(app, route_context)
    if _tab_enabled("tagging"):
        register_tagging_routes(app, route_context)
    if _tab_enabled("embedding"):
        register_embedding_routes(app, route_context)
    if _tab_enabled("compare"):
        register_compare_routes(app, route_context)

    def _analysis_for_dataset(dataset_key: tuple[str, str], refresh: bool = False) -> dict:
        from lerobot.data_platform.precompute.analysis import (
            AnalysisMetadata,
            analysis_input_digest,
            load_analysis_metadata,
        )

        desired = _lifecycle_store().tasks.snapshot(_repo_id_from_key(dataset_key)).to_dict()
        index_entry = datasets_index.get(dataset_key)
        if index_entry and not _is_dataset_root(Path(index_entry["root"]).expanduser()):
            ds_static = Path(index_entry["output_dir"]).expanduser() / "static"
            manifest = load_viewer_manifest(ds_static) or _fallback_manifest_from_cache(
                ds_static, _repo_id_from_key(dataset_key)
            )
            if manifest is None:
                raise ValueError("dataset metadata is unavailable")
            meta = AnalysisMetadata.from_report(manifest)
            root = Path(index_entry["root"])
            episodes = None
        else:
            dataset_obj, ds_static = _ensure_dataset_loaded(dataset_key)
            root = Path(dataset_obj.root)
            meta = load_analysis_metadata(root)
            episodes = _dataset_episode_ids(dataset_obj, dataset_key)
            if set(episodes) == {int(index) for index in dataset_obj.meta.episodes}:
                episodes = None
            manifest = load_viewer_manifest(ds_static) or {}

        recorded = TaskConfigSnapshot.from_dict(manifest.get("task_config")).to_dict()
        input_digest = analysis_input_digest(meta, ds_static, episodes)
        analysis = None if refresh else read_analysis_cache(ds_static)
        if analysis is not None and (
            analysis.get("task_config", {}).get("digest") != desired["digest"]
            or analysis.get("input_digest") != input_digest
        ):
            analysis = None
        if analysis is None:
            analysis = build_dataset_analysis(
                root, meta, ds_static, episodes, task_config=desired, cached_task_config=recorded
            )
            write_analysis_cache(ds_static, analysis)
        # Classification always uses the current catalog. Incompatible cached stages are omitted separately.
        analysis["task_config_stale"] = False
        return _analysis_with_live_tags(dataset_key, analysis, ds_static)

    def _analysis_with_live_tags(dataset_key: tuple[str, str], analysis: dict, ds_static: Path) -> dict:
        repo_id = _repo_id_from_key(dataset_key)
        tagging_dir = Path(ds_static) / "tagging"
        variants = available_tag_variants(tagging_dir)
        active = variants[0] if variants else None
        active_variant = _active_tag_variant(tagging_dir)
        records = current_tags(tagging_dir, active_variant) if active else {}

        episodes_payload = []
        tagged_episodes = 0
        reviewed_episodes = 0
        for row in analysis.get("episodes", []):
            copied = dict(row)
            record = records.get(int(row.get("episode_id", -1)))
            tags = dict(record.get("tags") or {}) if record else {}
            copied["tags"] = tags
            copied["tags_reviewed"] = bool(record and record.get("reviewed"))
            if tags:
                tagged_episodes += 1
            if copied["tags_reviewed"]:
                reviewed_episodes += 1
            episodes_payload.append(copied)

        out = {key: value for key, value in analysis.items() if key != "episodes"}
        out["episodes"] = episodes_payload
        out["tagging"] = {
            "status": "ready" if active else "missing",
            "active_variant": active["id"] if active else None,
            "tags_count": active["tags_count"] if active else 0,
            "reviewed_count": active["reviewed_count"] if active else 0,
            "tagged_episodes": tagged_episodes,
            "reviewed_episodes": reviewed_episodes,
            "variants": variants,
            "review_url": (
                f"/{repo_id}/tagging" + (f"?variant={active_variant}" if active_variant else "")
                if active
                else f"/{repo_id}/tagging"
            ),
        }
        return out

    if control_plane_store is not None:
        route_context.analysis_with_live_tags = _analysis_with_live_tags
        register_remote_analysis_routes(app, route_context)

    def _analysis_summary_payload(analysis: dict) -> dict:
        return {key: value for key, value in analysis.items() if key != "episodes"}

    def _analysis_tag_value_key(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _filter_analysis_episodes(episodes_payload: list[dict]) -> list[dict]:
        scene = request.args.get("scene", "").strip()
        target = request.args.get("target", "").strip()
        stage = request.args.get("stage", "").strip()
        exist_label = request.args.get("exist_label", "").strip()
        tag_name = request.args.get("tag_name", "").strip()
        tag_value = request.args.get("tag_value", "").strip()
        only_missing_csv = request.args.get("missing_csv", "").strip().lower() in {"1", "true", "yes"}

        result = []
        for row in episodes_payload:
            if scene and row.get("scene") != scene and row.get("scene_label") != scene:
                continue
            if target and row.get("target") != target:
                continue
            if stage and str(stage) not in row.get("stage_counts", {}):
                continue
            if exist_label and row.get("exist_true", {}).get(exist_label, 0) <= 0:
                continue
            if tag_name:
                tags = row.get("tags") or {}
                if tag_name not in tags:
                    continue
                if tag_value and _analysis_tag_value_key(tags.get(tag_name)) != tag_value:
                    continue
            if only_missing_csv and row.get("cache_status") != "missing_csv":
                continue
            result.append(row)
        return result

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/analysis")
    def show_analysis(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        repo_id = _repo_id_from_key(dataset_key)
        manifest, manifest_static = _manifest_for_key(dataset_key)
        if (
            dataset_key in datasets_index
            and dataset_key not in datasets_registry
            and manifest
            and manifest_static is not None
        ):
            episode_ids = manifest_episode_ids(manifest)
            first_episode = episode_ids[0] if episode_ids else 0
            return render_template(
                "visualize_dataset_analysis.html",
                dataset_namespace=dataset_namespace,
                dataset_name=dataset_name,
                dataset_key=repo_id,
                viewer_url=f"/{repo_id}/episode_{first_episode}",
                **_dataset_nav(
                    repo_id,
                    first_episode,
                    "analysis",
                    dataset_obj=None,
                    ds_static=manifest_static,
                    cache_only=True,
                    manifest=manifest,
                ),
            )
        dataset_obj, ds_static = _get_ctx(dataset_namespace, dataset_name)
        episode_ids = _dataset_episode_ids(dataset_obj, dataset_key)
        return render_template(
            "visualize_dataset_analysis.html",
            dataset_namespace=dataset_namespace,
            dataset_name=dataset_name,
            dataset_key=repo_id,
            viewer_url=f"/{repo_id}/episode_{episode_ids[0] if episode_ids else 0}",
            **_dataset_nav(repo_id, episode_ids[0] if episode_ids else 0, "analysis", dataset_obj, ds_static),
        )

    @app.route("/api/analysis/<string:dataset_namespace>/<string:dataset_name>/summary")
    def api_analysis_summary(dataset_namespace, dataset_name):
        refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
        dataset_key = (dataset_namespace, dataset_name)
        try:
            analysis = _analysis_for_dataset(dataset_key, refresh=refresh)
        except KeyError:
            return jsonify({"error": f"dataset is not registered: {_repo_id_from_key(dataset_key)}"}), 404
        except Exception as exc:
            logging.exception("Failed to build analysis for %s", _repo_id_from_key(dataset_key))
            return jsonify({"error": str(exc)}), 400
        return jsonify({"summary": _analysis_summary_payload(analysis)})

    @app.route("/api/analysis/<string:dataset_namespace>/<string:dataset_name>/episodes")
    def api_analysis_episodes(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        try:
            analysis = _analysis_for_dataset(dataset_key, refresh=False)
        except KeyError:
            return jsonify({"error": f"dataset is not registered: {_repo_id_from_key(dataset_key)}"}), 404
        except Exception as exc:
            logging.exception("Failed to load analysis episodes for %s", _repo_id_from_key(dataset_key))
            return jsonify({"error": str(exc)}), 400
        episodes_payload = _filter_analysis_episodes(analysis.get("episodes", []))
        return jsonify({"episodes": episodes_payload, "total": len(episodes_payload)})

    @app.route("/api/analysis/<string:dataset_namespace>/<string:dataset_name>/refresh", methods=["POST"])
    @app.get("/api/analysis/<string:dataset_namespace>/<string:dataset_name>/refresh")
    def api_analysis_refresh(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        try:
            analysis = _analysis_for_dataset(dataset_key, refresh=True)
        except KeyError:
            return jsonify({"error": f"dataset is not registered: {_repo_id_from_key(dataset_key)}"}), 404
        except Exception as exc:
            logging.exception("Failed to refresh analysis for %s", _repo_id_from_key(dataset_key))
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {"summary": _analysis_summary_payload(analysis), "episodes": analysis.get("episodes", [])}
        )

    @app.route("/job/status")
    def job_status():
        """Return current background job status."""
        job = _job_state["current"]
        if job is None:
            return jsonify({"status": "idle"})
        return jsonify(
            {
                "status": job.status,
                "job_type": job.job_type,
                "episode_id": job.episode_id,
                "step": job.step,
                "message": job.message,
                "error": job.error,
            }
        )

    @app.route("/job/clear", methods=["POST"])
    def job_clear():
        """Clear completed job state."""
        with _job_lock:
            job = _job_state["current"]
            if job and job.status in ("done", "error"):
                _job_state["current"] = None
        return jsonify({"status": "ok"})

    @app.route("/<string:dataset_namespace>/<string:dataset_name>")
    def show_first_episode(dataset_namespace, dataset_name):
        first_episode_id = 0
        manifest, _ = _manifest_for_key((dataset_namespace, dataset_name))
        if manifest:
            manifest_ids = manifest_episode_ids(manifest)
            if manifest_ids:
                first_episode_id = manifest_ids[0]
        return redirect(
            url_for(
                "show_episode",
                dataset_namespace=dataset_namespace,
                dataset_name=dataset_name,
                episode_id=first_episode_id,
            )
        )

    def _viewer_html_cache_control() -> str:
        return "no-store, no-cache, must-revalidate, max-age=0"

    def _render_cache_only_episode(
        dataset_namespace: str,
        dataset_name: str,
        episode_id: int,
        manifest: dict,
        static_dir: Path,
    ):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        remote_location = None
        result_sync_enabled = False
        if control_plane_store is not None:
            remote_locations = control_plane_store.list_locations()
            result_sync_enabled = any(
                item["dataset_key"] == repo_id
                and Path(str((item.get("metadata") or {}).get("cache_root") or "")) / "static"
                == Path(static_dir)
                for item in remote_locations
            )
            mutation_locations = set(
                control_plane_store.mutation_location_ids(
                    (getattr(g, "control_plane_user", None) or {}).get("user_id", "")
                )
            )
            remote_location = next(
                (
                    item
                    for item in remote_locations
                    if item["dataset_key"] == repo_id
                    and item["location_id"] in mutation_locations
                    and Path(str((item.get("metadata") or {}).get("cache_root") or "")) / "static"
                    == Path(static_dir)
                ),
                None,
            )
        episode_ids = manifest_episode_ids(manifest)
        if episode_id not in episode_ids:
            abort(404)

        data_version = _normalize_data_version(
            request.args.get("data_version") or manifest.get("data_version")
        )
        server_state["data_version"] = data_version
        cache_dir = Path(static_dir) / "csv"
        cached_csv = _get_csv_cache_path(
            cache_dir,
            episode_id,
            server_state["downsample"],
            precomputed_only=True,
        )
        columns = (
            [
                c
                for c in _columns_from_csv_header(cached_csv, manifest.get("features"))
                if c["key"] != "subtask_state"
            ]
            if cached_csv
            else []
        )
        ignored_columns = []

        episode_info = manifest_episode_info(manifest, episode_id)
        tasks = list(episode_info.get("tasks") or [])
        language_instruction = tasks if tasks else None
        episode_length = int(episode_info.get("length") or 0)
        cache_buster = request.args.get("_t", "")
        image_keys = list(manifest.get("image_keys") or [])
        cached_image_keys = list(_viewer_cache_inventory(Path(static_dir)).get("image_keys") or [])
        if cached_image_keys:
            image_keys = list(dict.fromkeys([*image_keys, *cached_image_keys]))
        videos_info = _find_prepared_videos(
            Path(static_dir),
            image_keys,
            episode_id,
            cache_buster,
            make_url=lambda rel_path: _asset_url(dataset_namespace, dataset_name, rel_path),
        )
        videos_info = _sort_videos_info(videos_info)
        issue_eps = _load_issue_episodes(Path(static_dir))
        current_issue_episode = episode_id in issue_eps
        task_episode_map = {}
        tag_episode_map = {}

        task_str = tasks[0] if tasks else ""
        max_stage, subtask_names = cached_subtask_names(task_str, Path(static_dir), episode_id)

        dataset_info = {
            "repo_id": repo_id,
            "num_samples": int(manifest.get("total_frames") or 0),
            "num_episodes": int(manifest.get("total_episodes") or len(episode_ids)),
            "fps": int(manifest.get("fps") or 0),
        }

        resp = make_response(
            render_template(
                "visualize_dataset_template.html",
                episode_id=episode_id,
                episodes=episode_ids,
                dataset_namespace=dataset_namespace,
                dataset_name=dataset_name,
                dataset_info=dataset_info,
                videos_info=videos_info,
                image_keys=[],
                episode_length=episode_length,
                video_codec_hint="h264" if videos_info else "none",
                language_instruction=language_instruction,
                episode_data_csv_str="",
                csv_url=url_for(
                    "get_episode_csv",
                    _signal_schema=1,
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                    episode_id=episode_id,
                ),
                columns=columns,
                signal_presentation=viewer_signal_presentation(
                    manifest.get("features") or {}, columns, str(manifest.get("robot_type") or "")
                ),
                signal_columns=manifest.get("signal_columns")
                or signal_columns(
                    manifest.get("features") or {}, qualify=manifest.get("robot_type") == "UMI"
                ),
                ignored_columns=ignored_columns,
                flagged_url=url_for(
                    "get_flagged_episodes",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                task_assignment_save_url=url_for(
                    "save_viewer_task_assignment",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                subtask_url=url_for(
                    "get_subtask_annotations",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                subtask_merge_url="",
                annotate_toggle_url=url_for(
                    "toggle_annotate",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                trim_url=url_for(
                    "get_trim_annotations",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                trim_merge_url="",
                delete_episode_url="",
                remote_delete_url=(
                    f"/api/control/locations/{remote_location['location_id']}/mutation-jobs"
                    if remote_location is not None
                    else ""
                ),
                remote_dataset_key=repo_id,
                result_sync_enabled=result_sync_enabled,
                annotate_enabled=server_state["annotate"],
                annotation_editable=(
                    control_plane_store is None
                    or (getattr(g, "control_plane_user", None) or {}).get("role")
                    in {"admin", "data_manager", "operator"}
                ),
                legacy_mutations_enabled=False,
                data_version=data_version,
                has_body_joints=has_body_joint_dimensions(manifest.get("features") or {}),
                has_legacy_flag=has_legacy_flag_dimension(manifest.get("features") or {}),
                issue_episodes=[],
                current_issue_episode=current_issue_episode,
                stage_edit_enabled=server_state["annotate"] or current_issue_episode,
                task_episode_map=task_episode_map,
                tag_episode_map=tag_episode_map,
                task_map_url=url_for(
                    "api_viewer_task_map",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                tag_map_url=url_for(
                    "api_viewer_tag_map",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                tagging_save_base_url=f"/api/tagging/{repo_id}/save/",
                subtask_names=subtask_names,
                cache_only=True,
                **_dataset_nav(
                    repo_id,
                    episode_id,
                    "viewer",
                    dataset_obj=None,
                    ds_static=Path(static_dir),
                    cache_only=True,
                    manifest=manifest,
                ),
            )
        )
        resp.headers["Cache-Control"] = _viewer_html_cache_control()
        return resp

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/episode_<int:episode_id>")
    def show_episode(dataset_namespace, dataset_name, episode_id):
        _viewer_t0 = time.perf_counter()
        _viewer_last = _viewer_t0
        _viewer_steps: list[tuple[str, float]] = []

        def _viewer_mark(label: str) -> None:
            nonlocal _viewer_last
            now = time.perf_counter()
            _viewer_steps.append((label, now - _viewer_last))
            _viewer_last = now

        def _viewer_log_if_slow() -> None:
            total = time.perf_counter() - _viewer_t0
            if total < 0.25:
                return
            step_text = ", ".join(f"{label}={elapsed:.3f}s" for label, elapsed in _viewer_steps)
            logging.info(
                "Viewer episode render timing %s/episode_%s total=%.3fs %s",
                repo_id,
                episode_id,
                total,
                step_text,
            )

        repo_id = f"{dataset_namespace}/{dataset_name}"
        dataset_key = (dataset_namespace, dataset_name)
        if request.args.get("loading") == "1":
            target_args = request.args.to_dict(flat=True)
            target_args.pop("loading", None)
            target_args["direct"] = "1"
            target_url = url_for(
                "show_episode",
                dataset_namespace=dataset_namespace,
                dataset_name=dataset_name,
                episode_id=episode_id,
            )
            if target_args:
                target_url = f"{target_url}?{urlencode(target_args)}"
            return render_template(
                "visualize_dataset_viewer_loading.html",
                dataset_key=repo_id,
                episode_id=episode_id,
                data_version=_normalize_data_version(request.args.get("data_version")),
                target_url=target_url,
                home_url=_home_url(repo_id),
            )
        # Try to get from registry, fall back to closure capture or HuggingFace
        try:
            index_entry = datasets_index.get(dataset_key)
            if index_entry is not None:
                root_path = Path(index_entry["root"]).expanduser()
                output_dir = Path(index_entry["output_dir"]).expanduser()
                uses_v3_adapter = _dataset_index_uses_v3_adapter(dataset_key)
                if uses_v3_adapter or (
                    not _is_dataset_root(root_path) and _root_has_cache_manifest(root_path, output_dir)
                ):
                    manifest, manifest_static = _manifest_for_key(dataset_key)
                    if manifest and manifest_static is not None:
                        return _render_cache_only_episode(
                            dataset_namespace,
                            dataset_name,
                            episode_id,
                            manifest,
                            manifest_static,
                        )
                    if uses_v3_adapter:
                        return "v3.0 viewer cache is missing; run Prepare video + CSV first.", 400
            if dataset_key in datasets_index or dataset_key in datasets_registry:
                dataset_obj, static_dir = _ensure_dataset_loaded(dataset_key)
            else:
                manifest, manifest_static = _manifest_for_key(dataset_key)
                if manifest and manifest_static is not None:
                    return _render_cache_only_episode(
                        dataset_namespace, dataset_name, episode_id, manifest, manifest_static
                    )
                dataset_obj = get_dataset_info(repo_id) if dataset is None else dataset
                static_dir = static_folder  # Use closure-captured for non-registered datasets
            _viewer_mark("load_dataset")
        except Exception as exc:
            manifest, manifest_static = _manifest_for_key(dataset_key)
            if manifest and manifest_static is not None:
                return _render_cache_only_episode(
                    dataset_namespace, dataset_name, episode_id, manifest, manifest_static
                )
            if not isinstance(exc, FileNotFoundError):
                raise
            return (
                "Make sure to convert your LeRobotDataset to v2 & above. See how to convert your dataset at https://github.com/huggingface/lerobot/pull/461",
                400,
            )
        data_version = (
            _normalize_data_version(request.args.get("data_version"))
            if request.args.get("data_version")
            else resolve_processing_profile(dataset_obj.root).legacy_data_version
        )
        server_state["data_version"] = data_version
        dataset_version = (
            str(dataset_obj.meta._version)
            if isinstance(dataset_obj, LeRobotDataset)
            else dataset_obj.codebase_version
        )
        match = re.search(r"v(\d+)\.", dataset_version)
        if match:
            major_version = int(match.group(1))
            if major_version < 2:
                return "Make sure to convert your LeRobotDataset to v2 & above."

        cache_dir = static_dir / "csv"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_csv = _get_csv_cache_path(
            cache_dir,
            episode_id,
            server_state["downsample"],
            server_state["precomputed_only"],
        )
        if server_state["precomputed_only"] and cached_csv and cached_csv.is_file():
            columns = [
                c
                for c in _columns_from_csv_header(cached_csv, dataset_obj.features)
                if c["key"] != "subtask_state"
            ]
            ignored_columns = []
            if not columns:
                columns, ignored_columns, _ = _viewer_columns_info(dataset_obj, dataset_key)
        else:
            columns, ignored_columns, _ = _viewer_columns_info(dataset_obj, dataset_key)
        _viewer_mark("columns")
        episode_data_csv_str = ""
        data_len = None
        dataset_info = {
            "repo_id": f"{dataset_namespace}/{dataset_name}",
            "num_samples": dataset_obj.num_frames
            if isinstance(dataset_obj, LeRobotDataset)
            else dataset_obj.total_frames,
            "num_episodes": dataset_obj.num_episodes
            if isinstance(dataset_obj, LeRobotDataset)
            else dataset_obj.total_episodes,
            "fps": dataset_obj.fps,
        }
        # Pass cache buster to video URLs so trimmed videos are not served from browser cache
        cache_buster = request.args.get("_t", "")

        is_local = isinstance(dataset_obj, LeRobotDataset) or hasattr(dataset_obj, "meta")
        if is_local:
            video_paths = [
                dataset_obj.meta.get_video_file_path(episode_id, key) for key in dataset_obj.meta.video_keys
            ]
            _cb = f"?_t={cache_buster}" if cache_buster else ""
            videos_info = [
                {
                    "url": _asset_url(dataset_namespace, dataset_name, video_path) + _cb,
                    "filename": video_path.parent.name,
                }
                for video_path in video_paths
            ]
            tasks = dataset_obj.meta.episodes[episode_id]["tasks"]
        else:
            video_keys = [key for key, ft in dataset_obj.features.items() if ft["dtype"] == "video"]
            videos_info = [
                {
                    "url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/"
                    + dataset_obj.video_path.format(
                        episode_chunk=int(episode_id) // dataset_obj.chunks_size,
                        video_key=video_key,
                        episode_index=episode_id,
                    ),
                    "filename": video_key,
                }
                for video_key in video_keys
            ]

            response = requests.get(
                f"https://huggingface.co/datasets/{repo_id}/resolve/main/meta/episodes.jsonl", timeout=5
            )
            response.raise_for_status()
            # Split into lines and parse each line as JSON
            tasks_jsonl = [json.loads(line) for line in response.text.splitlines() if line.strip()]

            filtered_tasks_jsonl = [row for row in tasks_jsonl if row["episode_index"] == episode_id]
            tasks = filtered_tasks_jsonl[0]["tasks"]
        _viewer_mark("media_tasks")

        language_instruction = tasks if tasks else None

        image_keys = [key for key, ft in dataset_obj.features.items() if ft["dtype"] == "image"]
        prepared_videos = False
        if not videos_info and image_keys:
            prepared = _find_prepared_videos(
                static_dir,
                image_keys,
                episode_id,
                cache_buster,
                make_url=lambda rel_path: _asset_url(dataset_namespace, dataset_name, rel_path),
            )
            if prepared:
                videos_info = prepared
                image_keys = []
                prepared_videos = True

        if (
            is_local
            and server_state["prepare_videos"]
            and not server_state["precomputed_only"]
            and not videos_info
            and image_keys
        ):
            videos_info = _prepare_episode_videos(
                dataset_obj,
                episode_id,
                image_keys,
                static_dir,
                max_frames=server_state["max_frames"],
                make_url=lambda rel_path: _asset_url(dataset_namespace, dataset_name, rel_path),
            )
            if videos_info:
                image_keys = []
                prepared_videos = True

        videos_info = _sort_videos_info(videos_info)
        _viewer_mark("video_lookup")

        video_codec_hint = ("h264" if prepared_videos else "av1") if videos_info else "none"
        episode_length = data_len if data_len is not None else None
        if isinstance(dataset_obj, LeRobotDataset):
            ep_from = dataset_obj.episode_data_index["from"][episode_id].item()
            ep_to = dataset_obj.episode_data_index["to"][episode_id].item()
            if episode_length is None:
                episode_length = ep_to - ep_from
            elif server_state["max_frames"] is not None:
                episode_length = min(episode_length, ep_to - ep_from)
        elif is_local and hasattr(dataset_obj.meta, "episodes"):
            if episode_length is None:
                episode_length = dataset_obj.meta.episodes[episode_id]["length"]

        episode_ids = _dataset_episode_ids(dataset_obj, dataset_key)
        if server_state["precomputed_only"]:
            cache_for_episode = _episode_cache_status(dataset_obj, static_dir, episode_id)
            if cache_for_episode["status"] != "cached":
                resp = make_response(
                    f"""
                    <html>
                    <body style="background:#020617;color:#cbd5e1;font-family:monospace;padding:24px">
                        <h2 style="color:#f8fafc">Viewer cache is missing</h2>
                        <p>Episode {episode_id} needs precomputed video and CSV cache before opening the viewer.</p>
                        <p>Video cache: {cache_for_episode["videos"]["cached"]}/{cache_for_episode["videos"]["total"]}</p>
                        <p>CSV cache: {cache_for_episode["csv"]["cached"]}/{cache_for_episode["csv"]["total"]}</p>
                        <a href="{_home_url(repo_id)}" style="color:#7dd3fc">Return Home and run Prepare</a>
                    </body>
                    </html>
                    """,
                    409,
                )
                resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                return resp
        _viewer_mark("episode_ids_cache")

        # Task filtering is useful but not required for first paint; load it async in the browser.
        task_episode_map = {}
        # Tag maps can be large; load them asynchronously via /api/viewer/.../tag_map.
        # This keeps episode-to-episode navigation responsive under tag filters.
        tag_episode_map = {}

        issue_eps = _load_issue_episodes(static_dir)
        current_issue_episode = episode_id in issue_eps

        # Build labels for the task-dependent stage range: -1..4, or -1..5 for give tasks.
        task_str = tasks[0] if tasks else ""
        max_stage, subtask_names = cached_subtask_names(task_str, Path(static_dir), episode_id)
        _viewer_mark("flags_subtasks")

        resp = make_response(
            render_template(
                "visualize_dataset_template.html",
                episode_id=episode_id,
                episodes=episode_ids,
                dataset_namespace=dataset_namespace,
                dataset_name=dataset_name,
                dataset_info=dataset_info,
                videos_info=videos_info,
                image_keys=image_keys,
                episode_length=episode_length,
                video_codec_hint=video_codec_hint,
                language_instruction=language_instruction,
                episode_data_csv_str=episode_data_csv_str,
                csv_url=url_for(
                    "get_episode_csv",
                    _signal_schema=1,
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                    episode_id=episode_id,
                ),
                columns=columns,
                signal_presentation=viewer_signal_presentation(
                    dataset_obj.features, columns, str(dataset_obj.meta.info.get("robot_type") or "")
                ),
                signal_columns=signal_columns(
                    dataset_obj.features,
                    qualify=str(dataset_obj.meta.info.get("robot_type", "")).lower() == "umi",
                ),
                ignored_columns=ignored_columns,
                flagged_url=url_for(
                    "get_flagged_episodes",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                task_assignment_save_url=url_for(
                    "save_viewer_task_assignment",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                subtask_url=url_for(
                    "get_subtask_annotations",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                subtask_merge_url=url_for(
                    "merge_subtask_parquet",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                annotate_toggle_url=url_for(
                    "toggle_annotate",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                trim_url=url_for(
                    "get_trim_annotations",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                trim_merge_url=url_for(
                    "apply_trim",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                delete_episode_url=url_for(
                    "delete_episode",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                annotate_enabled=server_state["annotate"],
                annotation_editable=(
                    control_plane_store is None
                    or (getattr(g, "control_plane_user", None) or {}).get("role")
                    in {"admin", "data_manager", "operator"}
                ),
                legacy_mutations_enabled=(_admin_authenticated() and not _dataset_is_protected(dataset_key)),
                data_version=data_version,
                has_body_joints=has_body_joint_dimensions(dataset_obj.features),
                has_legacy_flag=has_legacy_flag_dimension(dataset_obj.features),
                issue_episodes=[],
                current_issue_episode=current_issue_episode,
                stage_edit_enabled=server_state["annotate"] or current_issue_episode,
                task_episode_map=task_episode_map,
                tag_episode_map=tag_episode_map,
                task_map_url=url_for(
                    "api_viewer_task_map",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                tag_map_url=url_for(
                    "api_viewer_tag_map",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                ),
                tagging_save_base_url=f"/api/tagging/{repo_id}/save/",
                subtask_names=subtask_names,
                cache_only=False,
                **_dataset_nav(repo_id, episode_id, "viewer", dataset_obj, static_dir),
            )
        )
        _viewer_mark("template")
        _viewer_log_if_slow()
        resp.headers["Cache-Control"] = _viewer_html_cache_control()
        return resp

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/image")
    def get_image(dataset_namespace, dataset_name):
        key = request.args.get("key")
        if not key:
            abort(400)
        try:
            episode_id = int(request.args.get("episode", 0))
            frame_index = int(request.args.get("frame", 0))
        except (TypeError, ValueError):
            abort(400)
        dataset_key = (dataset_namespace, dataset_name)
        try:
            dataset_obj, ds_static, manifest, cache_only = _static_context_for_key(dataset_key)
        except Exception:
            abort(404)
        if cache_only or dataset_obj is None:
            if manifest:
                episode_ids = set(manifest_episode_ids(manifest))
                if episode_ids and episode_id not in episode_ids:
                    abort(404)
                image_keys = set(manifest.get("image_keys") or [])
                cached_image_keys = set(_viewer_cache_inventory(ds_static).get("image_keys") or [])
                allowed_image_keys = image_keys | cached_image_keys
                if allowed_image_keys and key not in allowed_image_keys:
                    abort(404)
            video_path = _cached_video_path(ds_static, key, episode_id)
            if video_path is None:
                abort(404)
            try:
                img_bytes = _jpeg_from_cached_video(video_path, frame_index)
            except Exception:
                logging.exception(
                    "Failed to serve cached video frame for %s/%s episode %s",
                    dataset_namespace,
                    dataset_name,
                    episode_id,
                )
                abort(404)
            return send_file(BytesIO(img_bytes), mimetype="image/jpeg")
        if not hasattr(dataset_obj, "root") or not hasattr(dataset_obj, "meta"):
            abort(404)
        if key not in dataset_obj.features or dataset_obj.features[key]["dtype"] != "image":
            abort(404)
        total_episodes = (
            dataset_obj.num_episodes
            if isinstance(dataset_obj, LeRobotDataset)
            else getattr(dataset_obj, "total_episodes", 0)
        )
        if episode_id < 0 or episode_id >= total_episodes:
            abort(404)
        if isinstance(dataset_obj, LeRobotDataset):
            ep_from = dataset_obj.episode_data_index["from"][episode_id].item()
            ep_to = dataset_obj.episode_data_index["to"][episode_id].item()
            episode_length = ep_to - ep_from
        else:
            episode_length = dataset_obj.meta.episodes[episode_id]["length"]
        if frame_index < 0 or frame_index >= episode_length:
            abort(404)
        parquet_path = dataset_obj.root / dataset_obj.meta.get_data_file_path(episode_id)
        img_bytes = cached_image_bytes(str(parquet_path), str(dataset_obj.root), key, frame_index)
        if img_bytes is None:
            abort(404)
        return send_file(BytesIO(img_bytes), mimetype="image/png")

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/episode_<int:episode_id>/data.csv")
    def get_episode_csv(dataset_namespace, dataset_name, episode_id):
        csv_started_at = time.perf_counter()

        def _csv_response(text_or_response):
            if isinstance(text_or_response, Response):
                resp = text_or_response
            else:
                resp = make_response(text_or_response)
            resp.headers["Cache-Control"] = "private, max-age=300"
            elapsed = time.perf_counter() - csv_started_at
            if elapsed > 0.25:
                size = resp.calculate_content_length()
                if size is None and not isinstance(text_or_response, Response):
                    size = len(text_or_response)
                logging.info(
                    "Viewer CSV timing %s/%s/episode_%s %.3fs bytes=%s",
                    dataset_namespace,
                    dataset_name,
                    episode_id,
                    elapsed,
                    size if size is not None else "unknown",
                )
            return resp

        try:
            dataset_key = (dataset_namespace, dataset_name)
            ds_static = _static_dir_for_key(dataset_key)
            manifest = load_viewer_manifest(ds_static) if ds_static is not None else None
            data_version = _normalize_data_version(
                request.args.get("data_version") or (manifest or {}).get("data_version")
            )
            if manifest and dataset_semantics(manifest)["data_version"] is None:
                data_version = None
            server_state["data_version"] = data_version
            cache_path = None
            if ds_static is not None:
                cache_dir = ds_static / "csv"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path = _get_csv_cache_path(
                    cache_dir,
                    episode_id,
                    server_state["downsample"],
                    server_state["precomputed_only"],
                )
            if server_state["precomputed_only"]:
                if cache_path and cache_path.is_file():
                    return _csv_response(
                        _serve_csv_stripped(cache_path, data_version, (manifest or {}).get("features"))
                    )
                return (
                    "CSV cache not found. Please precompute with python -m lerobot.data_platform --prepare-csv 1.",
                    404,
                )
            if cache_path and cache_path.is_file():
                return _csv_response(
                    _serve_csv_stripped(cache_path, data_version, (manifest or {}).get("features"))
                )

            dataset_obj, ds_static = _get_ctx(dataset_namespace, dataset_name)
            cache_dir = ds_static / "csv"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = _get_csv_cache_path(
                cache_dir,
                episode_id,
                server_state["downsample"],
                server_state["precomputed_only"],
            )
            data_version = (
                _normalize_data_version(request.args.get("data_version"))
                if request.args.get("data_version")
                else resolve_processing_profile(dataset_obj.root).legacy_data_version
            )
            server_state["data_version"] = data_version
            if cache_path is None or not cache_path.is_file():
                csv_string, _, _, _ = get_episode_data(
                    dataset_obj,
                    episode_id,
                    max_frames=server_state["max_frames"],
                    downsample=server_state["downsample"],
                    data_version=data_version,
                )
                ds = (
                    server_state["downsample"]
                    if server_state["downsample"] and server_state["downsample"] > 1
                    else 1
                )
                cache_path = cache_dir / f"episode_{episode_id:06d}_ds{ds}.csv"
                cache_path.write_text(csv_string)
            return _csv_response(_serve_csv_stripped(cache_path, data_version, dataset_obj.features))
        except Exception:
            tb = traceback.format_exc()
            print(f"\n=== CSV ERROR episode {episode_id} ===\n{tb}=== END ===\n", flush=True)
            logging.exception("Failed to serve CSV for episode %s", episode_id)
            return (f"Failed to serve CSV for episode {episode_id}:\n\n{tb}", 500)

    def _flagged_path(static_dir: Path) -> Path:
        return static_dir / "flagged_episodes.json"

    def _pending_prompt_assignments_path(static_dir: Path) -> Path:
        return static_dir / "prompt_assignments_pending.json"

    def _load_pending_prompt_assignments(static_dir: Path) -> dict[int, dict]:
        path = _pending_prompt_assignments_path(static_dir)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        raw_items = payload.get("assignments") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            return {}
        assignments: dict[int, dict] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                episode_index = int(item.get("episode_index"))
            except (TypeError, ValueError):
                continue
            selected_task = str(item.get("selected_task") or "").strip()
            if not selected_task:
                continue
            assignments[episode_index] = {
                "episode_index": episode_index,
                "selected_task": selected_task,
                "updated_at": item.get("updated_at") or time.time(),
                "source": item.get("source") or "viewer_cache_only",
            }
        return dict(sorted(assignments.items()))

    def _save_pending_prompt_assignments(static_dir: Path, assignments: dict[int, dict]) -> None:
        path = _pending_prompt_assignments_path(static_dir)
        items = [
            {
                "episode_index": int(item["episode_index"]),
                "selected_task": str(item["selected_task"]),
                "updated_at": item.get("updated_at") or time.time(),
                "source": item.get("source") or "viewer_cache_only",
            }
            for _, item in sorted(assignments.items())
        ]
        path.write_text(json.dumps({"version": 1, "assignments": items}, indent=2, ensure_ascii=False))

    def _upsert_pending_prompt_assignment(static_dir: Path, episode_id: int, selected_task: str) -> dict:
        assignments = _load_pending_prompt_assignments(static_dir)
        record = {
            "episode_index": int(episode_id),
            "selected_task": str(selected_task),
            "updated_at": time.time(),
            "source": "viewer_cache_only",
        }
        assignments[int(episode_id)] = record
        _save_pending_prompt_assignments(static_dir, assignments)
        return {"record": record, "pending_count": len(assignments)}

    def _load_flagged(static_dir: Path) -> list[int]:
        p = _flagged_path(static_dir)
        signature = _file_signature(p)
        cached = _flagged_episodes_cache.get(p)
        if cached is not None and cached[0] == signature:
            return list(cached[1])
        if p.is_file():
            try:
                data = json.loads(p.read_text())
                flagged = _normalize_episode_list(data.get("flagged_episodes", []))
                _flagged_episodes_cache[p] = (signature, flagged)
                return list(flagged)
            except (json.JSONDecodeError, OSError):
                pass
        _flagged_episodes_cache[p] = (signature, [])
        return []

    def _save_flagged(static_dir: Path, episodes: list[int]) -> bool:
        flagged_path = _flagged_path(static_dir)
        try:
            flagged_path.write_text(json.dumps({"flagged_episodes": sorted(episodes)}))
            _flagged_episodes_cache.pop(flagged_path, None)
        except OSError as exc:
            logging.warning("Could not save flagged episodes to %s: %s", flagged_path, exc)
            return False
        return True

    def _manual_flags_path(static_dir: Path) -> Path:
        return static_dir / "manual_flagged_episodes.json"

    def _load_flag_sidecar_json(path: Path) -> dict:
        path = Path(path)
        signature = _file_signature(path)
        cached = _flag_sidecar_json_cache.get(path)
        if cached is not None and cached[0] == signature:
            return dict(cached[1])
        payload: dict = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text())
                payload = data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, OSError):
                payload = {}
        _flag_sidecar_json_cache[path] = (signature, payload)
        return dict(payload)

    def _flag_set(path: Path) -> set[int]:
        payload = _load_flag_sidecar_json(path)
        values = payload.get("flagged_episodes") if isinstance(payload, dict) else []
        return set(_normalize_episode_list(values))

    def _load_manual_flag_payload(static_dir: Path) -> dict:
        path = _manual_flags_path(static_dir)
        payload = _load_flag_sidecar_json(path)
        return payload or {"flagged_episodes": [], "flag_reasons": {}}

    def _save_manual_flag_payload(static_dir: Path, payload: dict) -> None:
        episodes = sorted({int(ep) for ep in payload.get("flagged_episodes", [])})
        reasons = payload.get("flag_reasons") if isinstance(payload.get("flag_reasons"), dict) else {}
        normalized_reasons = {str(int(ep)): items for ep, items in reasons.items() if items}
        _manual_flags_path(static_dir).write_text(
            json.dumps(
                {"flagged_episodes": episodes, "flag_reasons": normalized_reasons},
                indent=2,
                ensure_ascii=False,
            )
        )
        _flag_sidecar_json_cache.pop(_manual_flags_path(static_dir), None)

    def _set_manual_flag_reason(
        static_dir: Path, episode_id: int, reason: str | None, issue_type: str = "manual"
    ) -> None:
        payload = _load_manual_flag_payload(static_dir)
        episodes = {int(ep) for ep in payload.get("flagged_episodes", [])}
        reasons = payload.get("flag_reasons") if isinstance(payload.get("flag_reasons"), dict) else {}
        if reason:
            episodes.add(int(episode_id))
            reasons[str(int(episode_id))] = [
                {
                    "type": issue_type or "manual",
                    "reason": reason,
                    "source": "viewer_manual",
                }
            ]
        else:
            episodes.discard(int(episode_id))
            reasons.pop(str(int(episode_id)), None)
        payload["flagged_episodes"] = sorted(episodes)
        payload["flag_reasons"] = reasons
        _save_manual_flag_payload(static_dir, payload)

    def _load_issue_episodes(static_dir: Path) -> set:
        issues_path = static_dir / "annotation_issues.json"
        signature = _file_signature(issues_path)
        cached = _issue_episodes_cache.get(issues_path)
        if cached is not None and cached[0] == signature:
            return set(cached[1])
        if issues_path.is_file():
            try:
                issues = json.loads(issues_path.read_text())
                episodes = {issue["episode"] for issue in issues}
                _issue_episodes_cache[issues_path] = (signature, episodes)
                return set(episodes)
            except (json.JSONDecodeError, OSError, KeyError):
                pass
        _issue_episodes_cache[issues_path] = (signature, set())
        return set()

    def _annotation_issues_by_episode(static_dir: Path) -> dict[int, list[dict]]:
        issues_path = static_dir / "annotation_issues.json"
        signature = _file_signature(issues_path)
        cached = _annotation_issues_by_episode_cache.get(issues_path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        grouped: dict[int, list[dict]] = {}
        if issues_path.is_file():
            try:
                issues = json.loads(issues_path.read_text())
            except (json.JSONDecodeError, OSError):
                issues = []
            for issue in issues if isinstance(issues, list) else []:
                if not isinstance(issue, dict):
                    continue
                try:
                    episode = int(issue.get("episode"))
                except (TypeError, ValueError):
                    continue
                grouped.setdefault(episode, []).append(issue)
        _annotation_issues_by_episode_cache[issues_path] = (signature, grouped)
        return grouped

    def _load_flag_reasons(
        static_dir: Path,
        flagged_episodes: list[int],
        dataset_root: Path | None = None,
        only_episodes: set[int] | None = None,
    ) -> dict[str, list[dict]]:
        flagged_set = {int(ep) for ep in flagged_episodes}
        if only_episodes is not None:
            flagged_set &= {int(ep) for ep in only_episodes}
        reasons: dict[str, list[dict]] = {str(ep): [] for ep in flagged_set}
        episode_tasks: dict[int, str] = {}
        if dataset_root is not None and flagged_set:
            try:
                for row in load_episode_records(Path(dataset_root)):
                    episode = int(row.get("episode_index"))
                    if episode not in flagged_set:
                        continue
                    tasks = row.get("tasks") or []
                    if tasks:
                        episode_tasks[episode] = str(tasks[0])
                    if len(episode_tasks) >= len(flagged_set):
                        break
            except (OSError, TypeError, ValueError):
                episode_tasks = {}

        task_assignment_records: dict[tuple[int, str], dict] = {}

        def _task_assignment_record_for(episode: int, reason: str | None) -> dict | None:
            reason_key = str(reason or "")
            if dataset_root is None or reason_key not in {
                "multiple_task_assignments",
                "prompt_action_mismatch",
                "wrong_prompt",
            }:
                return None
            key = (int(episode), reason_key)
            if key in task_assignment_records:
                return task_assignment_records[key]
            try:
                for record in list_task_assignment_choices(Path(dataset_root), static_dir):
                    try:
                        record_episode = int(record["episode_index"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if record_episode in flagged_set:
                        task_assignment_records[(record_episode, str(record.get("reason") or ""))] = record
                return task_assignment_records.get(key)
            except Exception:
                return None

        issues_by_episode = _annotation_issues_by_episode(static_dir)
        if issues_by_episode:
            for issue in [
                issue for episode in flagged_set for issue in issues_by_episode.get(int(episode), [])
            ]:
                if not isinstance(issue, dict):
                    continue
                try:
                    episode = int(issue.get("episode"))
                except (TypeError, ValueError):
                    continue
                if episode not in flagged_set:
                    continue
                item = {
                    "type": str(issue.get("type") or "issue"),
                    "reason": str(issue.get("reason") or "unknown"),
                }
                if issue.get("task"):
                    item["task"] = str(issue.get("task"))
                elif episode in episode_tasks:
                    item["task"] = episode_tasks[episode]
                if "frames" in issue:
                    item["frames"] = issue.get("frames") or []
                if "metrics" in issue:
                    item["metrics"] = issue.get("metrics") or {}
                assignment = _task_assignment_record_for(episode, item["reason"])
                if assignment and assignment.get("reason") == item["reason"]:
                    metrics = dict(item.get("metrics") or {})
                    metrics["candidates"] = assignment.get("candidates") or []
                    item["metrics"] = metrics
                reasons.setdefault(str(episode), []).append(item)
        manual_payload = _load_manual_flag_payload(static_dir)
        manual_reasons = (
            manual_payload.get("flag_reasons") if isinstance(manual_payload.get("flag_reasons"), dict) else {}
        )
        for episode_key, items in manual_reasons.items():
            try:
                episode = int(episode_key)
            except (TypeError, ValueError):
                continue
            if episode not in flagged_set:
                continue
            if isinstance(items, list):
                enriched_items = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    enriched = dict(item)
                    assignment = _task_assignment_record_for(episode, enriched.get("reason"))
                    if assignment and assignment.get("reason") == enriched.get("reason"):
                        metrics = dict(enriched.get("metrics") or {})
                        metrics["candidates"] = assignment.get("candidates") or []
                        enriched["metrics"] = metrics
                    if not enriched.get("task") and episode in episode_tasks:
                        enriched["task"] = episode_tasks[episode]
                    enriched_items.append(enriched)
                reasons.setdefault(str(episode), []).extend(enriched_items)
        for auto_path in static_dir.glob("*_flagged_episodes.json"):
            if auto_path.name in {"flagged_episodes.json", "manual_flagged_episodes.json"}:
                continue
            payload = _load_flag_sidecar_json(auto_path)
            if not isinstance(payload, dict):
                continue
            auto_flagged = _flag_set(auto_path)
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
            fallback_reason = str(summary.get("reason") or "").strip()
            fallback_type = "auto_flag"
            if auto_path.name == "tagging_prompt_mismatch_flagged_episodes.json":
                fallback_reason = "prompt_action_mismatch"
                fallback_type = "tagging_prompt_behavior"
            elif auto_path.name == "quality_flagged_episodes.json":
                fallback_type = "quality_flag"
            flag_reasons = payload.get("flag_reasons")
            if not isinstance(flag_reasons, dict):
                flag_reasons = {}
            for episode_key, items in flag_reasons.items():
                try:
                    episode = int(episode_key)
                except (TypeError, ValueError):
                    continue
                if episode not in flagged_set:
                    continue
                if isinstance(items, list):
                    enriched_items = []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        enriched = dict(item)
                        assignment = _task_assignment_record_for(episode, enriched.get("reason"))
                        if assignment and assignment.get("reason") == enriched.get("reason"):
                            metrics = dict(enriched.get("metrics") or {})
                            metrics["candidates"] = assignment.get("candidates") or []
                            enriched["metrics"] = metrics
                        if not enriched.get("task") and episode in episode_tasks:
                            enriched["task"] = episode_tasks[episode]
                        enriched_items.append(enriched)
                    existing_items = reasons.setdefault(str(episode), [])
                    seen = {
                        (
                            str(existing.get("type") or ""),
                            str(existing.get("reason") or ""),
                            str(existing.get("source") or ""),
                        )
                        for existing in existing_items
                        if isinstance(existing, dict)
                    }
                    for enriched in enriched_items:
                        key = (
                            str(enriched.get("type") or ""),
                            str(enriched.get("reason") or ""),
                            str(enriched.get("source") or ""),
                        )
                        if key in seen:
                            continue
                        existing_items.append(enriched)
                        seen.add(key)
            if fallback_reason:
                for episode in auto_flagged & flagged_set:
                    existing_items = reasons.setdefault(str(episode), [])
                    if any(
                        isinstance(item, dict) and item.get("reason") == fallback_reason
                        for item in existing_items
                    ):
                        continue
                    item = {"type": fallback_type, "reason": fallback_reason}
                    if episode in episode_tasks:
                        item["task"] = episode_tasks[episode]
                    existing_items.append(item)
        manual_flagged = _flag_set(_manual_flags_path(static_dir))
        for episode in flagged_set:
            if reasons.get(str(episode)):
                continue
            if episode in manual_flagged:
                reasons[str(episode)] = [{"type": "manual", "reason": "manual_flag"}]
            else:
                reasons[str(episode)] = [{"type": "unknown", "reason": "unknown_flag_reason"}]
        return reasons

    def _flagged_payload(
        static_dir: Path,
        dataset_root: Path | None = None,
        include_reasons: bool = True,
        only_episodes: set[int] | None = None,
    ) -> dict:
        flagged = _load_flagged(static_dir)
        payload = {"flagged_episodes": flagged}
        if include_reasons:
            payload["flag_reasons"] = _load_flag_reasons(static_dir, flagged, dataset_root, only_episodes)
        return payload

    @app.get("/api/control/locations/<string:location_id>/flagged-episodes")
    def remote_flagged_episodes(location_id):
        if control_plane_store is None:
            return jsonify(error="Remote datasets are unavailable"), 404
        try:
            location = control_plane_store.get_location(location_id)
        except KeyError:
            return jsonify(error="Dataset location not found"), 404
        metadata = location.get("metadata") or {}
        cache_root = metadata.get("cache_root")
        if not metadata.get("viewer_ready") or not cache_root:
            return jsonify(error="Prepare viewer for this Agent dataset before reading flag types"), 409
        cache_base = (
            Path(remote_cache_root).expanduser()
            if remote_cache_root is not None
            else Path(registry_state["path"]).parent.parent / "remote_cache"
        ).resolve()
        ds_static = (Path(cache_root) / "static").resolve()
        if not ds_static.is_relative_to(cache_base):
            return jsonify(error="Viewer cache is outside the configured cache root"), 409
        if not (ds_static / "viewer_manifest.json").is_file():
            return jsonify(
                error="Viewer cache is missing; prepare viewer again before reading flag types"
            ), 409
        try:
            return jsonify(_flagged_payload(ds_static))
        except (OSError, ValueError, TypeError, AttributeError):
            logging.exception("Failed to read remote flag types for location %s", location_id)
            return jsonify(error="Could not read flag files from the Viewer cache; prepare viewer again"), 409

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/flagged_episodes", methods=["GET"])
    def get_flagged_episodes(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        ds_static = _static_dir_for_key(dataset_key)
        if ds_static is None:
            abort(404)
        include_reasons = str(request.args.get("reasons", "1")).strip().lower() not in {"0", "false", "no"}
        only_episodes = set()
        for value in request.args.getlist("episode"):
            try:
                only_episodes.add(int(value))
            except (TypeError, ValueError):
                continue
        dataset_root = _dataset_root_for_key(dataset_key)
        try:
            return jsonify(
                _flagged_payload(
                    ds_static,
                    dataset_root,
                    include_reasons=include_reasons,
                    only_episodes=only_episodes or None,
                )
            )
        except Exception:
            logging.exception(
                "Failed to load flagged episode reasons for %s/%s", dataset_namespace, dataset_name
            )
            # Keep viewer navigation usable even if a malformed sidecar reason file exists.
            return jsonify({"flagged_episodes": _load_flagged(ds_static), "flag_reasons": {}})

    def _operation_log_response(dataset_key: str | None = None):
        try:
            limit = max(1, min(5000, int(request.args.get("limit", 200))))
        except (TypeError, ValueError):
            limit = 200
        log_dirs = [static_folder]
        if dataset_key:
            try:
                key = _repo_key(dataset_key)
            except KeyError:
                return jsonify({"error": f"dataset is not registered: {dataset_key}"}), 404
            ds_static = _static_dir_for_key(key)
            if ds_static is None:
                return jsonify({"error": f"dataset is not registered: {dataset_key}"}), 404
            log_dirs.append(ds_static)
        operations = read_operation_events(
            log_dirs,
            limit=limit,
            dataset_key=dataset_key,
            status=str(request.args.get("status") or "").strip() or None,
            operation=str(request.args.get("operation") or "").strip() or None,
        )
        return jsonify({"operations": operations, "count": len(operations)})

    @app.route("/api/operation_log", methods=["GET"])
    def api_operation_log():
        return _operation_log_response(str(request.args.get("dataset_key") or "").strip() or None)

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/operation_log", methods=["GET"])
    def get_operation_log(dataset_namespace, dataset_name):
        return _operation_log_response(f"{dataset_namespace}/{dataset_name}")

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/flagged_episodes", methods=["POST"])
    def toggle_flagged_episode(dataset_namespace, dataset_name):
        ds_static = _static_dir_for_key((dataset_namespace, dataset_name))
        if ds_static is None:
            abort(404)
        body = request.get_json(silent=True) or {}
        episode_id = body.get("episode_id")
        if episode_id is None:
            return jsonify({"error": "episode_id required"}), 400
        episode_id = int(episode_id)
        reason = str(body.get("reason") or "").strip()
        issue_type = str(body.get("issue_type") or "manual").strip() or "manual"
        current = set(_load_flagged(ds_static))
        if "flagged" in body:
            flagged = bool(body.get("flagged"))
            if flagged:
                current.add(episode_id)
                _set_manual_flag_reason(ds_static, episode_id, reason or "manual_flag", issue_type)
            else:
                current.discard(episode_id)
                _set_manual_flag_reason(ds_static, episode_id, None)
        else:
            if episode_id in current:
                current.discard(episode_id)
                _set_manual_flag_reason(ds_static, episode_id, None)
            else:
                current.add(episode_id)
                if reason:
                    _set_manual_flag_reason(ds_static, episode_id, reason, issue_type)
        result = sorted(current)
        _save_flagged(ds_static, result)
        dataset_obj = None
        try:
            dataset_obj, _ = _get_ctx(dataset_namespace, dataset_name)
        except Exception:
            dataset_obj = None
        _append_operation_log(
            ds_static,
            "flag_toggle",
            dataset_key=(dataset_namespace, dataset_name),
            dataset_root=Path(dataset_obj.root) if dataset_obj is not None else None,
            episode_ids=[episode_id],
            details={
                "flagged": episode_id in current,
                "flagged_count": len(result),
                "reason": reason,
                "issue_type": issue_type,
            },
        )
        return jsonify(
            _flagged_payload(
                ds_static,
                Path(dataset_obj.root) if dataset_obj is not None else None,
                include_reasons=True,
                only_episodes={episode_id},
            )
        )

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/task_assignment", methods=["POST"])
    def save_viewer_task_assignment(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        dataset_obj = None
        ds_static = None
        try:
            dataset_obj, ds_static = _get_ctx(dataset_namespace, dataset_name)
        except Exception:
            manifest, manifest_static = _manifest_for_key(dataset_key)
            if manifest is None or manifest_static is None:
                abort(404)
            ds_static = Path(manifest_static)
        body = request.get_json(silent=True) or {}
        try:
            episode_id = int(body.get("episode_id"))
            selected_task = str(body.get("selected_task") or "").strip()
            reason = str(body.get("reason") or "").strip()
            if not selected_task:
                raise ValueError("selected_task is required")
            if dataset_obj is None or not _admin_authenticated() or _dataset_is_protected(dataset_key):
                pending = _upsert_pending_prompt_assignment(ds_static, episode_id, selected_task)
                result = {
                    "episode_index": episode_id,
                    "selected_task": selected_task,
                    "pending": True,
                    "pending_count": pending["pending_count"],
                    "message": (
                        "Saved as a Data Curation sidecar decision. Publish a curation manifest "
                        "to materialize it into a new dataset version."
                    ),
                }
                _append_operation_log(
                    ds_static,
                    "prompt_assignment_pending_save",
                    dataset_key=dataset_key,
                    dataset_root=None,
                    episode_ids=[episode_id],
                    details={"selected_task": selected_task, "result": result},
                )
                return jsonify(
                    {
                        "result": result,
                        **_flagged_payload(ds_static, None, include_reasons=True, only_episodes={episode_id}),
                    }
                )
            result = apply_task_assignment_choice(
                Path(dataset_obj.root),
                ds_static,
                episode_id,
                selected_task,
                reason=reason or None,
            )
            pending_assignments = _load_pending_prompt_assignments(ds_static)
            if episode_id in pending_assignments:
                pending_assignments.pop(episode_id, None)
                _save_pending_prompt_assignments(ds_static, pending_assignments)
            if hasattr(dataset_obj, "meta") and hasattr(dataset_obj.meta, "episodes"):
                episode_info = dataset_obj.meta.episodes.get(episode_id)
                if isinstance(episode_info, dict):
                    episode_info["tasks"] = [selected_task]
            _task_caches.pop(dataset_key, None)
            _append_operation_log(
                ds_static,
                "prompt_assignment_save",
                dataset_key=dataset_key,
                dataset_root=Path(dataset_obj.root),
                episode_ids=[episode_id],
                details={"selected_task": selected_task, "result": result},
            )
        except Exception as exc:
            logging.exception("Failed to save viewer task assignment")
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "result": result,
                **_flagged_payload(
                    ds_static, Path(dataset_obj.root), include_reasons=True, only_episodes={episode_id}
                ),
            }
        )

    # --- Subtask annotation helpers ---

    def _subtask_ann_path(static_dir: Path) -> Path:
        return static_dir / "subtask_annotations.json"

    def _load_subtask_annotations(static_dir: Path) -> dict:
        p = _subtask_ann_path(static_dir)
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _compute_subtask_states(timestamps: list, transitions: list) -> list:
        sorted_tr = sorted(transitions, key=lambda x: x["time"])
        result = []
        for t in timestamps:
            state = 0
            for tr in sorted_tr:
                if tr["time"] <= t:
                    state = tr["state"]
                else:
                    break
            result.append(state)
        return result

    def _get_episode_task_text(dataset_obj, episode_id: int) -> str:
        if not hasattr(dataset_obj, "meta") or not hasattr(dataset_obj.meta, "episodes"):
            return ""
        episode_info = dataset_obj.meta.episodes.get(episode_id)
        if episode_info is None:
            episode_info = dataset_obj.meta.episodes.get(str(episode_id), {})
        tasks = episode_info.get("tasks", []) if episode_info else []
        return tasks[0] if tasks else ""

    def _compute_subtask_texts(dataset_obj, episode_id: int, states: list[int]) -> list[str]:
        task = _get_episode_task_text(dataset_obj, episode_id)
        config = _lifecycle_store().tasks.snapshot(getattr(dataset_obj, "repo_id", "")).to_dict()
        count = max(2, max(states, default=4) + 1)
        equal_time = _data_profile_for_dataset(dataset_obj).stage_profile == STAGE_PROFILE_EQUAL_TIME
        names = {
            state: generate_subtask_text(
                task, int(state), task_config=config, stage_count=count, force_equal_time=equal_time
            )
            for state in set(states)
        }
        return [names[state] for state in states]

    def _upsert_table_column(table: pa.Table, name: str, values: list, arrow_type: pa.DataType) -> pa.Table:
        field = pa.field(name, arrow_type)
        column = pa.array(values, type=arrow_type)
        idx = table.schema.get_field_index(name)
        if idx >= 0:
            return table.set_column(idx, field, column)
        return table.append_column(field, column)

    def _update_cached_csv_subtask_columns(
        csv_path: Path,
        transitions: list,
        include_hidden_state: bool = True,
    ) -> bool:
        if not csv_path.is_file():
            return False

        with open_cached_csv(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])

        if not rows:
            return False

        timestamps = [float(r["timestamp"]) for r in rows]
        states = _compute_subtask_states(timestamps, transitions)
        max_stage = max(1, max((int(item["state"]) for item in transitions), default=4))
        logical_csv_path(csv_path).with_suffix(".stages.json").write_text(
            json.dumps(
                {
                    "stage_count": max_stage + 1,
                    "encoding": "normalized",
                    "source": "existing",
                }
            )
        )

        if include_hidden_state and "subtask_state" not in fieldnames:
            fieldnames.append("subtask_state")
        if "stage" not in fieldnames:
            fieldnames.append("stage")

        for row, st in zip(rows, states, strict=False):
            if include_hidden_state:
                row["subtask_state"] = st
            row["stage"] = st / float(max_stage)

        with open_cached_csv(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        return True

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/subtask_annotations", methods=["GET"])
    def get_subtask_annotations(dataset_namespace, dataset_name):
        ds_static = _static_dir_for_key((dataset_namespace, dataset_name))
        if ds_static is None:
            abort(404)
        return jsonify({"annotations": _load_subtask_annotations(ds_static)})

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/subtask_annotations", methods=["POST"])
    def save_subtask_annotation(dataset_namespace, dataset_name):
        dataset_obj, ds_static, manifest, _ = _static_context_for_key((dataset_namespace, dataset_name))
        body = request.get_json(silent=True) or {}
        episode_id = body.get("episode_id")
        if episode_id is None:
            return jsonify({"error": "episode_id required"}), 400
        try:
            episode_id = int(episode_id)
        except (TypeError, ValueError):
            return jsonify({"error": "episode_id must be an integer"}), 400
        episode_ids = (
            manifest_episode_ids(manifest)
            if manifest is not None
            else _dataset_episode_ids(dataset_obj, (dataset_namespace, dataset_name))
        )
        if episode_id not in episode_ids:
            return jsonify({"error": "episode not found"}), 404
        denied = _check_edit_permission(ds_static, episode_id)
        if denied:
            return denied
        transitions = body.get("transitions", [])
        update_csv = body.get("update_csv", False)

        # Save to JSON
        annotations = _load_subtask_annotations(ds_static)
        annotations[str(episode_id)] = transitions
        _subtask_ann_path(ds_static).write_text(json.dumps(annotations, indent=2))

        # Optionally update precomputed CSV subtask columns
        if update_csv:
            try:
                csv_path = _get_csv_cache_path(
                    ds_static / "csv", episode_id, server_state["downsample"], precomputed_only=True
                )
                if csv_path and _update_cached_csv_subtask_columns(
                    csv_path, transitions, include_hidden_state=True
                ):
                    from lerobot.data_platform.dataset_results import mark_edited_csv

                    mark_edited_csv(Path(ds_static).parent, episode_id)
                    logging.info("Updated subtask_state and stage in CSV for episode %s", episode_id)
            except Exception:
                logging.exception("Failed to update subtask columns in CSV for episode %s", episode_id)
                return jsonify(
                    {"error": "Annotation saved, but updating the Viewer CSV failed. Retry saving."}
                ), 500

        _append_operation_log(
            ds_static,
            "subtask_annotation_save",
            dataset_key=(dataset_namespace, dataset_name),
            episode_ids=[episode_id],
            details={"transition_count": len(transitions), "update_csv": bool(update_csv)},
        )
        return jsonify({"status": "ok", "episode_id": episode_id})

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/subtask_merge_parquet", methods=["POST"])
    def merge_subtask_parquet(dataset_namespace, dataset_name):
        ds, ds_static = _get_ctx(dataset_namespace, dataset_name)
        body = request.get_json(silent=True) or {}
        episode_id = body.get("episode_id")
        if episode_id is None:
            return jsonify({"error": "episode_id required"}), 400
        episode_id = int(episode_id)
        denied = _check_edit_permission(ds_static, episode_id)
        if denied:
            return denied

        annotations = _load_subtask_annotations(ds_static)
        transitions = annotations.get(str(episode_id))
        if not transitions:
            return jsonify({"status": "no_annotation"})

        if ds is None:
            return jsonify({"status": "no_dataset"})

        try:
            import pyarrow as pa
            import pyarrow.parquet as pq_mod

            parquet_path = ds.root / ds.meta.get_data_file_path(episode_id)
            if not parquet_path.is_file():
                return jsonify({"status": "parquet_not_found"})

            table = (
                read_episode_table(ds.root, ds.meta, episode_id)
                if is_v3_metadata(ds.meta)
                else pq_mod.read_table(parquet_path)
            )
            timestamps = table["timestamp"].to_pylist()
            states = _compute_subtask_states(timestamps, transitions)

            sync_subtask_text = "subtask" in table.schema.names
            if not sync_subtask_text and hasattr(ds.meta, "features"):
                sync_subtask_text = "subtask" in ds.meta.features
            subtask_texts = _compute_subtask_texts(ds, episode_id, states) if sync_subtask_text else None

            if is_v3_metadata(ds.meta):
                upsert_episode_column(
                    ds.root,
                    ds.meta,
                    episode_id,
                    "subtask_state",
                    states,
                    pa.int32(),
                )
                if subtask_texts is not None:
                    upsert_episode_column(
                        ds.root,
                        ds.meta,
                        episode_id,
                        "subtask",
                        subtask_texts,
                        pa.string(),
                    )
            else:
                table = _upsert_table_column(table, "subtask_state", states, pa.int32())
                if subtask_texts is not None:
                    table = _upsert_table_column(
                        table,
                        "subtask",
                        subtask_texts,
                        pa.string(),
                    )
                tmp_path = parquet_path.with_suffix(".parquet.tmp")
                pq_mod.write_table(table, tmp_path)
                tmp_path.replace(parquet_path)

            update_info_features(
                ds.root,
                {
                    "subtask_state": {
                        "dtype": "int32",
                        "shape": [1],
                        "names": None,
                    },
                    **(
                        {
                            "subtask": {
                                "dtype": "string",
                                "shape": [1],
                                "names": None,
                            }
                        }
                        if subtask_texts is not None
                        else {}
                    ),
                },
            )
            state_values = np.asarray(states, dtype=np.float64)
            update_episode_stats_for_subtask_state(
                ds.root,
                {
                    episode_id: {
                        "min": [int(state_values.min())],
                        "max": [int(state_values.max())],
                        "mean": [float(state_values.mean())],
                        "std": [float(state_values.std())],
                        "count": [len(state_values)],
                    }
                },
            )
            if sync_subtask_text:
                logging.info("Merged subtask_state and subtask into parquet for episode %s", episode_id)
            else:
                logging.info("Merged subtask_state into parquet for episode %s", episode_id)
        except Exception:
            logging.exception("Failed to merge subtask_state into parquet for episode %s", episode_id)
            return jsonify({"status": "error"}), 500

        # Also update precomputed CSV subtask columns
        try:
            cache_dir = ds_static / "csv"
            csv_path = _find_any_cached_csv(cache_dir, episode_id) if cache_dir.is_dir() else None
            if csv_path and _update_cached_csv_subtask_columns(
                csv_path, transitions, include_hidden_state=True
            ):
                logging.info("Updated subtask_state and stage in CSV for episode %s", episode_id)
        except Exception:
            logging.exception("Failed to update subtask columns in CSV for episode %s", episode_id)

        _append_operation_log(
            ds_static,
            "subtask_merge_parquet",
            dataset_key=(dataset_namespace, dataset_name),
            dataset_root=Path(ds.root),
            episode_ids=[episode_id],
            details={"transition_count": len(transitions), "sync_subtask_text": bool(sync_subtask_text)},
        )
        return jsonify({"status": "ok", "episode_id": episode_id})

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/toggle_annotate", methods=["POST"])
    def toggle_annotate(dataset_namespace, dataset_name):
        body = request.get_json(silent=True) or {}
        if "annotate" in body:
            server_state["annotate"] = bool(body["annotate"])
        else:
            server_state["annotate"] = not server_state["annotate"]
        logging.info("Annotate mode toggled to %s", server_state["annotate"])
        return jsonify({"annotate": server_state["annotate"]})

    # --- Trim annotation helpers ---

    def _trim_ann_path(static_dir: Path) -> Path:
        return static_dir / "trim_annotations.json"

    def _load_trim_annotations(static_dir: Path) -> dict:
        p = _trim_ann_path(static_dir)
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/trim_annotations", methods=["GET"])
    def get_trim_annotations(dataset_namespace, dataset_name):
        ds_static = _static_dir_for_key((dataset_namespace, dataset_name))
        if ds_static is None:
            abort(404)
        return jsonify({"annotations": _load_trim_annotations(ds_static)})

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/trim_annotations", methods=["POST"])
    def save_trim_annotation(dataset_namespace, dataset_name):
        dataset_obj, ds_static, manifest, _ = _static_context_for_key((dataset_namespace, dataset_name))
        body = request.get_json(silent=True) or {}
        episode_id = body.get("episode_id")
        if episode_id is None:
            return jsonify({"error": "episode_id required"}), 400
        try:
            episode_id = int(episode_id)
        except (TypeError, ValueError):
            return jsonify({"error": "episode_id must be an integer"}), 400
        episode_ids = (
            manifest_episode_ids(manifest)
            if manifest is not None
            else _dataset_episode_ids(dataset_obj, (dataset_namespace, dataset_name))
        )
        if episode_id not in episode_ids:
            return jsonify({"error": "episode not found"}), 404
        denied = _check_edit_permission(ds_static, episode_id)
        if denied:
            return denied
        trim_start_frame = body.get("trim_start_frame")
        trim_end_frame = body.get("trim_end_frame")
        trim_start = body.get("trim_start")
        trim_end = body.get("trim_end")
        annotations = _load_trim_annotations(ds_static)
        if trim_start_frame is None and trim_end_frame is None and trim_start is None and trim_end is None:
            annotations.pop(str(episode_id), None)
        else:
            annotations[str(episode_id)] = {
                "unit": "frame",
                "trim_start_frame": int(trim_start_frame) if trim_start_frame is not None else None,
                "trim_end_frame": int(trim_end_frame) if trim_end_frame is not None else None,
                "trim_start": trim_start,
                "trim_end": trim_end,
            }
        _trim_ann_path(ds_static).write_text(json.dumps(annotations, indent=2))
        _append_operation_log(
            ds_static,
            "trim_annotation_save",
            dataset_key=(dataset_namespace, dataset_name),
            episode_ids=[episode_id],
            details={
                "cleared": str(episode_id) not in annotations,
                "trim_start_frame": trim_start_frame,
                "trim_end_frame": trim_end_frame,
                "trim_start": trim_start,
                "trim_end": trim_end,
            },
        )
        return jsonify({"status": "ok", "episode_id": episode_id})

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/trim_merge", methods=["POST"])
    def apply_trim(dataset_namespace, dataset_name):
        ds, ds_static = _get_ctx(dataset_namespace, dataset_name)
        body = request.get_json(silent=True) or {}
        episode_id = body.get("episode_id")
        if episode_id is None:
            return jsonify({"error": "episode_id required"}), 400
        reason = str(body.get("reason") or "").strip()
        if not reason:
            return jsonify({"error": "reason is required for in-place frame deletion"}), 400
        if len(reason) > 500:
            return jsonify({"error": "reason must be 500 characters or fewer"}), 400
        episode_id = int(episode_id)
        denied = _check_edit_permission(ds_static, episode_id)
        if denied:
            return denied

        annotations = _load_trim_annotations(ds_static)
        trim_info = annotations.get(str(episode_id))
        if "trim_start_frame" in body or "trim_end_frame" in body:
            start, end = body.get("trim_start_frame"), body.get("trim_end_frame")
            if type(start) is not int or type(end) is not int or start < 0 or end < start:
                return jsonify({"error": "A valid inclusive integer frame range is required"}), 400
            trim_info = body
        if not trim_info:
            return jsonify({"status": "no_annotation"})

        trim_start_frame = trim_info.get("trim_start_frame")
        trim_end_frame = trim_info.get("trim_end_frame")
        trim_start = float(trim_info.get("trim_start") or 0.0)
        trim_end = trim_info.get("trim_end")
        trim_end = float(trim_end) if trim_end is not None else None

        if ds is None:
            return jsonify({"status": "no_dataset"}), 400
        dataset = ds
        static_folder = ds_static

        def _sse(event_type, data_dict):
            """Format a Server-Sent Event line."""
            if event_type == "error":
                _append_operation_log(
                    ds_static,
                    "trim_apply",
                    dataset_key=(dataset_namespace, dataset_name),
                    dataset_root=Path(dataset.root),
                    episode_ids=[episode_id],
                    status="failed",
                    details={**data_dict, "reason": reason},
                )
            return f"event: {event_type}\ndata: {json.dumps(data_dict)}\n\n"

        if is_v3_dataset(dataset.root):

            def generate_v3():
                yield _sse(
                    "progress",
                    {
                        "step": 1,
                        "total": 2,
                        "message": "Rebuilding shared v3 data/video shards losslessly...",
                    },
                )
                try:
                    table = read_episode_table(
                        dataset.root,
                        dataset.meta,
                        episode_id,
                        columns=["timestamp"],
                    )
                    timestamps = table["timestamp"].to_pylist()
                    original_length = len(timestamps)
                    if original_length == 0:
                        yield _sse("error", {"message": "episode has no frames"})
                        return

                    def _time_to_frame(value, default: int) -> int:
                        if value is None:
                            return default
                        target = float(value)
                        return min(
                            range(original_length),
                            key=lambda index: abs(float(timestamps[index]) - target),
                        )

                    start_frame = (
                        int(trim_start_frame)
                        if trim_start_frame is not None
                        else _time_to_frame(trim_start, 0)
                    )
                    end_frame = (
                        int(trim_end_frame)
                        if trim_end_frame is not None
                        else _time_to_frame(trim_end, original_length - 1)
                    )
                    result = trim_v3_episode_inplace(
                        dataset,
                        static_folder,
                        episode_id,
                        start_frame,
                        end_frame,
                        workers=8,
                    )
                except Exception as exc:
                    logging.exception("Failed to trim v3 episode %s", episode_id)
                    yield _sse("error", {"message": str(exc)})
                    return

                yield _sse(
                    "progress",
                    {
                        "step": 2,
                        "total": 2,
                        "message": "Refreshing viewer metadata and caches...",
                    },
                )
                for cached_fn in [get_parquet_file, get_row_group_offsets, cached_image_bytes]:
                    if hasattr(cached_fn, "cache_clear"):
                        cached_fn.cache_clear()
                annotations.pop(str(episode_id), None)
                _trim_ann_path(ds_static).write_text(json.dumps(annotations, indent=2))
                _append_operation_log(
                    ds_static,
                    "trim_apply",
                    dataset_key=(dataset_namespace, dataset_name),
                    dataset_root=Path(dataset.root),
                    episode_ids=[episode_id],
                    details={
                        **result,
                        "dataset_format": "v3.0",
                        "workers": 8,
                        "reason": reason,
                    },
                )
                yield _sse(
                    "done",
                    {
                        "status": "ok",
                        "episode_id": episode_id,
                        "new_length": result["new_length"],
                    },
                )

            return Response(
                stream_with_context(generate_v3()),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        def generate():
            total_steps = 8
            trim_origin = 0.0
            new_duration = 0.0
            trim_video_start = 0.0
            trim_duration = 0.0
            start_frame = 0
            end_frame = 0

            # --- 1. Trim parquet ---
            yield _sse("progress", {"step": 1, "total": total_steps, "message": "Trimming parquet data..."})
            try:
                parquet_path = dataset.root / dataset.meta.get_data_file_path(episode_id)
                if not parquet_path.is_file():
                    yield _sse("error", {"message": "parquet not found"})
                    return

                table = pq.read_table(parquet_path)
                original_length = table.num_rows
                timestamps = table["timestamp"].to_pylist()
                if original_length == 0:
                    yield _sse("error", {"message": "episode has no frames"})
                    return

                def _clamp_frame(value, default: int) -> int:
                    if value is None:
                        value = default
                    return max(0, min(original_length - 1, int(round(float(value)))))

                def _time_to_frame(value, default: int) -> int:
                    if value is None or not timestamps:
                        return default
                    target = float(value)
                    return min(range(len(timestamps)), key=lambda idx: abs(float(timestamps[idx]) - target))

                start_frame = _clamp_frame(
                    trim_start_frame,
                    _time_to_frame(trim_start, 0),
                )
                end_frame = _clamp_frame(
                    trim_end_frame,
                    _time_to_frame(trim_end, original_length - 1),
                )
                if end_frame < start_frame:
                    yield _sse("error", {"message": "trim end frame must be >= start frame"})
                    return

                keep_mask = [start_frame <= idx <= end_frame for idx in range(original_length)]
                mask_array = pa.array(keep_mask, type=pa.bool_())
                table = table.filter(mask_array)

                if table.num_rows == 0:
                    yield _sse("error", {"message": "trim would remove all frames"})
                    return

                trim_origin = float(table["timestamp"][0].as_py())
                new_duration = float(table["timestamp"][-1].as_py()) - trim_origin
                trim_video_start = float(timestamps[start_frame]) if timestamps else 0.0
                fps_value = float(getattr(dataset, "fps", getattr(dataset.meta, "fps", 0)) or 0)
                if fps_value > 0:
                    trim_duration = (end_frame - start_frame + 1) / fps_value
                elif len(timestamps) > 1:
                    frame_dt = float(timestamps[1]) - float(timestamps[0])
                    trim_duration = max(
                        0.0, float(timestamps[end_frame]) - float(timestamps[start_frame]) + frame_dt
                    )
                else:
                    trim_duration = max(0.0, float(timestamps[end_frame]) - float(timestamps[start_frame]))
                if trim_duration <= 0:
                    trim_duration = (1.0 / fps_value) if fps_value > 0 else 0.001
                new_ts = [t - trim_origin for t in table["timestamp"].to_pylist()]
                ts_field = table.schema.field("timestamp")
                col_idx = table.schema.get_field_index("timestamp")
                table = table.set_column(col_idx, ts_field, pa.array(new_ts, type=ts_field.type))

                if "frame_index" in table.schema.names:
                    fi_field = table.schema.field("frame_index")
                    fi_idx = table.schema.get_field_index("frame_index")
                    table = table.set_column(
                        fi_idx, fi_field, pa.array(range(table.num_rows), type=fi_field.type)
                    )

                if "index" in table.schema.names:
                    idx_field = table.schema.field("index")
                    idx_idx = table.schema.get_field_index("index")
                    table = table.set_column(
                        idx_idx, idx_field, pa.array(range(table.num_rows), type=idx_field.type)
                    )

                new_length = table.num_rows
                dropped_frames = original_length - new_length

                tmp_path = parquet_path.with_suffix(".parquet.tmp")
                pq.write_table(table, tmp_path)
                tmp_path.replace(parquet_path)
                logging.info(
                    "Trimmed parquet for episode %s by frame: %d-%d, %d -> %d frames",
                    episode_id,
                    start_frame,
                    end_frame,
                    original_length,
                    new_length,
                )
            except Exception:
                logging.exception("Failed to trim parquet for episode %s", episode_id)
                yield _sse("error", {"message": "parquet trim failed"})
                return

            # --- 2. Trim source videos (re-encode) ---
            yield _sse(
                "progress", {"step": 2, "total": total_steps, "message": "Re-encoding source videos..."}
            )
            ffmpeg_path = shutil.which("ffmpeg")
            if ffmpeg_path and hasattr(dataset.meta, "video_keys"):
                video_keys_list = list(dataset.meta.video_keys)
                for vi, video_key in enumerate(video_keys_list):
                    try:
                        video_rel = dataset.meta.get_video_file_path(episode_id, video_key)
                        video_path = dataset.root / video_rel
                        if not video_path.is_file():
                            continue
                        yield _sse(
                            "progress",
                            {
                                "step": 2,
                                "total": total_steps,
                                "message": f"Re-encoding video {vi + 1}/{len(video_keys_list)}: {video_key}...",
                            },
                        )
                        tmp_video = video_path.with_suffix(".mp4.tmp")
                        cmd = [
                            ffmpeg_path,
                            "-y",
                            "-ss",
                            f"{trim_video_start:.6f}",
                            "-i",
                            str(video_path),
                            "-t",
                            f"{trim_duration:.6f}",
                            "-c:v",
                            "libx264",
                            "-profile:v",
                            "baseline",
                            "-pix_fmt",
                            "yuv420p",
                            "-movflags",
                            "+faststart",
                            "-an",
                            str(tmp_video),
                        ]
                        result = subprocess.run(cmd, capture_output=True, timeout=300)
                        if result.returncode == 0:
                            tmp_video.replace(video_path)
                            logging.info("Trimmed video %s for episode %s", video_key, episode_id)
                        else:
                            logging.warning(
                                "ffmpeg trim failed for %s ep %s: %s",
                                video_key,
                                episode_id,
                                result.stderr.decode(errors="ignore"),
                            )
                            if tmp_video.is_file():
                                tmp_video.unlink()
                    except Exception:
                        logging.exception("Failed to trim video %s for episode %s", video_key, episode_id)

            # --- 3. Regenerate prepared videos from trimmed parquet ---
            yield _sse(
                "progress", {"step": 3, "total": total_steps, "message": "Regenerating preview videos..."}
            )
            image_keys = [key for key, ft in dataset.features.items() if ft["dtype"] == "image"]
            if image_keys:
                for image_key in image_keys:
                    cached = static_folder / "videos" / image_key / f"episode_{episode_id:06d}_h264.mp4"
                    if cached.is_file():
                        cached.unlink()
                        logging.info("Deleted cached prepared video: %s", cached)
                if hasattr(dataset, "root") and hasattr(dataset, "meta"):
                    for vi, image_key in enumerate(image_keys):
                        yield _sse(
                            "progress",
                            {
                                "step": 3,
                                "total": total_steps,
                                "message": f"Encoding preview video {vi + 1}/{len(image_keys)}: {image_key}...",
                            },
                        )
                        try:
                            _prepare_episode_videos(
                                dataset, episode_id, [image_key], static_folder, max_frames=max_frames
                            )
                            logging.info(
                                "Regenerated prepared video %s for episode %s", image_key, episode_id
                            )
                        except Exception:
                            logging.exception(
                                "Failed to regenerate prepared video %s for episode %s", image_key, episode_id
                            )
            elif hasattr(dataset.meta, "video_keys"):
                for video_key in dataset.meta.video_keys:
                    cached = static_folder / "videos" / video_key / f"episode_{episode_id:06d}_h264.mp4"
                    if cached.is_file():
                        cached.unlink()

            # --- 4. Update metadata ---
            yield _sse("progress", {"step": 4, "total": total_steps, "message": "Updating metadata..."})
            try:
                from lerobot.common.datasets.compute_stats import aggregate_stats, compute_episode_stats
                from lerobot.common.datasets.utils import (
                    EPISODES_PATH,
                    EPISODES_STATS_PATH,
                    STATS_PATH,
                    load_episodes,
                    load_episodes_stats,
                    load_info,
                    serialize_dict,
                    write_info,
                    write_jsonlines,
                    write_stats,
                )

                eps_data = load_episodes(dataset.root)
                if episode_id in eps_data:
                    eps_data[episode_id]["length"] = new_length
                    all_eps = [eps_data[k] for k in sorted(eps_data.keys())]
                    write_jsonlines(all_eps, dataset.root / EPISODES_PATH)
                    if hasattr(dataset.meta, "episodes") and episode_id in dataset.meta.episodes:
                        dataset.meta.episodes[episode_id]["length"] = new_length

                info = load_info(dataset.root)
                info["total_frames"] = info.get("total_frames", 0) - dropped_frames
                write_info(info, dataset.root)
                if hasattr(dataset.meta, "info"):
                    dataset.meta.info["total_frames"] = info["total_frames"]

                episodes_stats_path = dataset.root / EPISODES_STATS_PATH
                if episodes_stats_path.is_file():
                    try:
                        import numpy as np

                        all_ep_stats = load_episodes_stats(dataset.root)
                        trimmed_table = pq.read_table(parquet_path)
                        features = info.get("features", {})
                        episode_data = {}
                        for key, ft in features.items():
                            if ft["dtype"] in ["string", "image", "video"]:
                                continue
                            if key in trimmed_table.schema.names:
                                col_data = trimmed_table[key].to_pylist()
                                episode_data[key] = np.array(col_data)
                        new_ep_stats = compute_episode_stats(episode_data, features)
                        # Preserve old image/video stats (pixel stats don't need recomputation
                        # after trimming, and parquet image format is not directly loadable)
                        old_stats = all_ep_stats.get(episode_id, {})
                        for key in old_stats:
                            if key not in new_ep_stats and features.get(key, {}).get("dtype") in [
                                "image",
                                "video",
                            ]:
                                new_ep_stats[key] = old_stats[key]
                        # Update count for preserved image/video stats to match new frame count
                        for key in new_ep_stats:
                            if features.get(key, {}).get("dtype") in ["image", "video"]:
                                new_ep_stats[key]["count"] = np.array([new_length])
                        all_ep_stats[episode_id] = new_ep_stats
                        all_stats_list = []
                        for ep_idx in sorted(all_ep_stats.keys()):
                            all_stats_list.append(
                                {
                                    "episode_index": ep_idx,
                                    "stats": serialize_dict(all_ep_stats[ep_idx]),
                                }
                            )
                        write_jsonlines(all_stats_list, episodes_stats_path)
                        logging.info("Updated episodes_stats.jsonl for episode %s", episode_id)

                        global_stats_path = dataset.root / STATS_PATH
                        if global_stats_path.is_file():
                            all_stats_values = list(all_ep_stats.values())
                            if all_stats_values:
                                aggregated = aggregate_stats(all_stats_values)
                                write_stats(aggregated, dataset.root)
                                logging.info("Updated global stats.json")
                    except Exception:
                        logging.exception("Failed to update episodes_stats for episode %s", episode_id)

                logging.info("Updated metadata for episode %s: dropped %d frames", episode_id, dropped_frames)
            except Exception:
                logging.exception(
                    "Failed to update metadata for episode %s (parquet already trimmed)", episode_id
                )

            # --- 5. Auto-fix parquet indices across the dataset ---
            yield _sse(
                "progress", {"step": 5, "total": total_steps, "message": "Repairing episode indices..."}
            )
            try:
                affected_episode_ids = sorted(getattr(dataset.meta, "episodes", {}).keys())
                if not affected_episode_ids:
                    total_eps = getattr(dataset.meta, "total_episodes", 0) or getattr(
                        dataset, "total_episodes", 0
                    )
                    affected_episode_ids = list(range(total_eps))
                fix_episode_indices(dataset.root, dataset.meta, affected_episode_ids)
            except Exception:
                logging.exception("Failed to auto-fix episode indices after trimming episode %s", episode_id)
                yield _sse("error", {"message": "trim succeeded but automatic index repair failed"})
                return

            # --- 6. Adjust subtask annotations ---
            yield _sse("progress", {"step": 6, "total": total_steps, "message": "Adjusting annotations..."})
            try:
                subtask_anns = _load_subtask_annotations(ds_static)
                ep_key = str(episode_id)
                if ep_key in subtask_anns:
                    transitions = subtask_anns[ep_key]
                    adjusted = []
                    for tr in transitions:
                        new_time = tr["time"] - trim_origin
                        if 0 <= new_time <= new_duration:
                            adjusted.append({"time": round(new_time, 6), "state": tr["state"]})
                    if adjusted:
                        subtask_anns[ep_key] = adjusted
                    else:
                        del subtask_anns[ep_key]
                    _subtask_ann_path(ds_static).write_text(json.dumps(subtask_anns, indent=2))
            except Exception:
                logging.exception("Failed to adjust subtask annotations for episode %s", episode_id)

            # --- 7. Regenerate CSV cache ---
            yield _sse("progress", {"step": 7, "total": total_steps, "message": "Regenerating CSV cache..."})
            for cached_fn in [get_parquet_file, get_row_group_offsets, cached_image_bytes]:
                if hasattr(cached_fn, "cache_clear"):
                    cached_fn.cache_clear()

            cache_dir = static_folder / "csv"
            if cache_dir.is_dir():
                for csv_file in cached_csv_files(cache_dir, f"episode_{episode_id:06d}_ds*.csv"):
                    csv_file.unlink()
                    logging.info("Deleted cached CSV: %s", csv_file.name)
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                csv_string, _, _, _ = get_episode_data(
                    dataset,
                    episode_id,
                    max_frames=max_frames,
                    downsample=downsample,
                    data_version=server_state["data_version"],
                )
                ds = downsample if downsample and downsample > 1 else 1
                cache_path = cache_dir / f"episode_{episode_id:06d}_ds{ds}.csv"
                cache_path.write_text(csv_string)
                logging.info("Regenerated CSV cache for episode %s", episode_id)

                # Re-inject subtask columns from adjusted subtask annotations
                try:
                    subtask_anns = _load_subtask_annotations(ds_static)
                    ep_key = str(episode_id)
                    if ep_key in subtask_anns:
                        transitions = subtask_anns[ep_key]
                        if _update_cached_csv_subtask_columns(
                            cache_path, transitions, include_hidden_state=True
                        ):
                            logging.info(
                                "Re-injected subtask_state and stage into CSV for episode %s", episode_id
                            )
                except Exception:
                    logging.exception(
                        "Failed to re-inject subtask columns into CSV for episode %s", episode_id
                    )
            except Exception:
                tb = traceback.format_exc()
                print(f"\n=== CSV REGEN ERROR episode {episode_id} ===\n{tb}=== END ===\n", flush=True)
                logging.exception(
                    "Failed to regenerate CSV for episode %s (will be re-generated on next load)", episode_id
                )

            # --- 8. Finalize ---
            yield _sse("progress", {"step": 8, "total": total_steps, "message": "Cleaning up..."})
            annotations.pop(str(episode_id), None)
            _trim_ann_path(ds_static).write_text(json.dumps(annotations, indent=2))
            _append_operation_log(
                ds_static,
                "trim_apply",
                dataset_key=(dataset_namespace, dataset_name),
                dataset_root=Path(dataset.root),
                episode_ids=[episode_id],
                details={
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "original_length": original_length,
                    "new_length": new_length,
                    "dropped_frames": dropped_frames,
                    "new_duration": new_duration,
                    "reason": reason,
                },
            )

            yield _sse("done", {"status": "ok", "episode_id": episode_id, "new_length": new_length})

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/delete_episode", methods=["POST"])
    def delete_episode(dataset_namespace, dataset_name):
        ds, ds_static = _get_ctx(dataset_namespace, dataset_name)
        body = request.get_json(silent=True) or {}
        episode_id = body.get("episode_id")
        if episode_id is None:
            return jsonify({"error": "episode_id required"}), 400
        reason = str(body.get("reason") or "").strip()
        if not reason:
            return jsonify({"error": "reason is required for in-place episode deletion"}), 400
        if len(reason) > 500:
            return jsonify({"error": "reason must be 500 characters or fewer"}), 400
        episode_id = int(episode_id)

        if ds is None:
            return jsonify({"status": "no_dataset"}), 400
        dataset = ds
        static_folder = ds_static

        def _sse(event_type, data_dict):
            if event_type == "error":
                _append_operation_log(
                    ds_static,
                    "episode_delete",
                    dataset_key=(dataset_namespace, dataset_name),
                    dataset_root=Path(dataset.root),
                    episode_ids=[episode_id],
                    status="failed",
                    details={**data_dict, "reason": reason},
                )
            return f"event: {event_type}\ndata: {json.dumps(data_dict)}\n\n"

        def generate():
            nonlocal episodes
            if is_v3_dataset(dataset.root):
                yield _sse(
                    "progress",
                    {
                        "step": 1,
                        "total": 2,
                        "message": "Rebuilding shared v3 shards without the selected episode...",
                    },
                )
                try:
                    result = delete_episodes_inplace(
                        dataset,
                        [episode_id],
                        static_folder=static_folder,
                    )
                except Exception as exc:
                    logging.exception("Failed to delete v3 episode %s", episode_id)
                    yield _sse("error", {"message": str(exc)})
                    return
                remaining_ids = _refresh_dataset_after_episode_delete(
                    (dataset_namespace, dataset_name),
                    dataset,
                    ds_static,
                )
                if episodes is not None:
                    episodes.clear()
                    episodes.extend(remaining_ids)
                _append_operation_log(
                    ds_static,
                    "episode_delete",
                    dataset_key=(dataset_namespace, dataset_name),
                    dataset_root=Path(dataset.root),
                    episode_ids=[episode_id],
                    details={**result, "dataset_format": "v3.0", "reason": reason},
                )
                yield _sse(
                    "progress",
                    {
                        "step": 2,
                        "total": 2,
                        "message": "Refreshed viewer metadata and caches",
                    },
                )
                yield _sse(
                    "done",
                    {
                        "status": "ok",
                        "episode_id": episode_id,
                        "next_episode": result["next_episode"],
                    },
                )
                return

            import math

            from lerobot.common.datasets.compute_stats import aggregate_stats
            from lerobot.common.datasets.utils import (
                EPISODES_PATH,
                EPISODES_STATS_PATH,
                STATS_PATH,
                load_episodes,
                load_episodes_stats,
                load_info,
                serialize_dict,
                write_info,
                write_jsonlines,
                write_stats,
            )

            total_steps = 8
            chunks_size = dataset.meta.info.get("chunks_size", 1000)
            image_keys = [key for key, ft in dataset.features.items() if ft["dtype"] == "image"]
            video_keys = list(dataset.meta.video_keys) if hasattr(dataset.meta, "video_keys") else []
            remaining_episode_ids: list[int] = []

            # --- 1. Collect info & delete target episode files ---
            yield _sse("progress", {"step": 1, "total": total_steps, "message": "Deleting episode files..."})
            eps_data = load_episodes(dataset.root)
            ep_length = 0
            if episode_id in eps_data:
                ep_length = eps_data[episode_id].get("length", 0)

            # Determine all existing episode indices (sorted)
            all_indices = sorted(eps_data.keys())
            if episode_id not in all_indices:
                yield _sse("error", {"message": f"episode {episode_id} not found in metadata"})
                return
            # Episodes after the deleted one that need reindexing
            indices_to_shift = [idx for idx in all_indices if idx > episode_id]

            try:
                # Delete parquet
                parquet_path = dataset.root / dataset.meta.get_data_file_path(episode_id)
                if parquet_path.is_file():
                    if ep_length == 0:
                        ep_length = pq.read_table(parquet_path, columns=["timestamp"]).num_rows
                    parquet_path.unlink()
                    logging.info("Deleted parquet for episode %s", episode_id)
                # Delete source videos
                for vk in video_keys:
                    vp = dataset.root / dataset.meta.get_video_file_path(episode_id, vk)
                    if vp.is_file():
                        vp.unlink()
                # Delete cached preview videos + CSV
                for key in image_keys:
                    c = static_folder / "videos" / key / f"episode_{episode_id:06d}_h264.mp4"
                    if c.is_file():
                        c.unlink()
                for vk in video_keys:
                    c = static_folder / "videos" / vk / f"episode_{episode_id:06d}_h264.mp4"
                    if c.is_file():
                        c.unlink()
                cache_dir = static_folder / "csv"
                if cache_dir.is_dir():
                    for f in cached_csv_files(cache_dir, f"episode_{episode_id:06d}_ds*.csv"):
                        f.unlink()
            except Exception:
                logging.exception("Failed to delete files for episode %s", episode_id)
                yield _sse("error", {"message": "failed to delete episode files"})
                return

            # --- 2. Reindex subsequent episodes (rename files) ---
            n_shift = len(indices_to_shift)
            yield _sse(
                "progress", {"step": 2, "total": total_steps, "message": f"Reindexing {n_shift} episodes..."}
            )
            try:
                for si, old_idx in enumerate(indices_to_shift):
                    new_idx = old_idx - 1
                    if si % max(1, n_shift // 20) == 0 or si == n_shift - 1:
                        yield _sse(
                            "progress",
                            {
                                "step": 2,
                                "total": total_steps,
                                "message": f"Reindexing episode {old_idx} -> {new_idx}  ({si + 1}/{n_shift})...",
                            },
                        )

                    old_chunk = old_idx // chunks_size
                    new_chunk = new_idx // chunks_size

                    # Rename parquet & update episode_index column inside
                    old_pq = dataset.root / dataset.meta.info["data_path"].format(
                        episode_chunk=old_chunk, episode_index=old_idx
                    )
                    new_pq = dataset.root / dataset.meta.info["data_path"].format(
                        episode_chunk=new_chunk, episode_index=new_idx
                    )
                    if old_pq.is_file():
                        new_pq.parent.mkdir(parents=True, exist_ok=True)
                        # Read, update episode_index, write to new location
                        table = pq.read_table(old_pq)
                        if "episode_index" in table.schema.names:
                            ei_field = table.schema.field("episode_index")
                            ei_col_idx = table.schema.get_field_index("episode_index")
                            table = table.set_column(
                                ei_col_idx, ei_field, pa.array([new_idx] * table.num_rows, type=ei_field.type)
                            )
                        tmp_pq = new_pq.with_suffix(".parquet.tmp")
                        pq.write_table(table, tmp_pq)
                        # Remove old file first (might be same dir), then rename tmp
                        old_pq.unlink()
                        tmp_pq.rename(new_pq)

                    # Rename source videos
                    if dataset.meta.info.get("video_path"):
                        for vk in video_keys:
                            old_vp = dataset.root / dataset.meta.info["video_path"].format(
                                episode_chunk=old_chunk, video_key=vk, episode_index=old_idx
                            )
                            new_vp = dataset.root / dataset.meta.info["video_path"].format(
                                episode_chunk=new_chunk, video_key=vk, episode_index=new_idx
                            )
                            if old_vp.is_file():
                                new_vp.parent.mkdir(parents=True, exist_ok=True)
                                old_vp.rename(new_vp)

                    # Rename cached preview videos
                    for key in image_keys:
                        old_c = static_folder / "videos" / key / f"episode_{old_idx:06d}_h264.mp4"
                        new_c = static_folder / "videos" / key / f"episode_{new_idx:06d}_h264.mp4"
                        if old_c.is_file():
                            old_c.rename(new_c)
                    for vk in video_keys:
                        old_c = static_folder / "videos" / vk / f"episode_{old_idx:06d}_h264.mp4"
                        new_c = static_folder / "videos" / vk / f"episode_{new_idx:06d}_h264.mp4"
                        if old_c.is_file():
                            old_c.rename(new_c)

                    # Rename cached CSV files
                    if cache_dir.is_dir():
                        for csv_f in cached_csv_files(cache_dir, f"episode_{old_idx:06d}_ds*.csv"):
                            new_name = csv_f.name.replace(f"episode_{old_idx:06d}", f"episode_{new_idx:06d}")
                            csv_f.rename(cache_dir / new_name)
            except Exception:
                logging.exception(
                    "Failed during reindexing at episode %s", old_idx if indices_to_shift else episode_id
                )
                yield _sse("error", {"message": "reindexing failed — dataset may be in inconsistent state"})
                return

            # --- 3. Clean up empty chunk directories ---
            yield _sse(
                "progress", {"step": 3, "total": total_steps, "message": "Cleaning empty directories..."}
            )
            try:
                data_dir = dataset.root / "data"
                if data_dir.is_dir():
                    for chunk_dir in sorted(data_dir.iterdir(), reverse=True):
                        if chunk_dir.is_dir() and not any(chunk_dir.iterdir()):
                            chunk_dir.rmdir()
                videos_dir = dataset.root / "videos"
                if videos_dir.is_dir():
                    for sub in videos_dir.iterdir():
                        if sub.is_dir():
                            for chunk_dir in sorted(sub.iterdir(), reverse=True):
                                if chunk_dir.is_dir() and not any(chunk_dir.iterdir()):
                                    chunk_dir.rmdir()
            except Exception:
                logging.exception("Failed to clean empty directories")

            # --- 4. Update metadata files ---
            yield _sse("progress", {"step": 4, "total": total_steps, "message": "Updating metadata..."})
            try:
                # Rebuild episodes.jsonl with new contiguous indices
                del eps_data[episode_id]
                new_eps_data = {}
                for new_idx, old_idx in enumerate(sorted(eps_data.keys())):
                    entry = eps_data[old_idx].copy()
                    entry["episode_index"] = new_idx
                    new_eps_data[new_idx] = entry
                write_jsonlines(
                    [new_eps_data[k] for k in sorted(new_eps_data.keys())],
                    dataset.root / EPISODES_PATH,
                )
                # Update in-memory meta.episodes
                if hasattr(dataset.meta, "episodes"):
                    dataset.meta.episodes.clear()
                    dataset.meta.episodes.update(new_eps_data)

                # Update info.json
                info = load_info(dataset.root)
                new_total_eps = len(new_eps_data)
                info["total_episodes"] = new_total_eps
                info["total_frames"] = max(0, info.get("total_frames", 0) - ep_length)
                if video_keys:
                    info["total_videos"] = max(0, info.get("total_videos", 0) - len(video_keys))
                if "splits" in info:
                    for split_name in info["splits"]:
                        info["splits"][split_name] = f"0:{new_total_eps}"
                info["total_chunks"] = math.ceil(new_total_eps / chunks_size) if new_total_eps > 0 else 0
                write_info(info, dataset.root)
                if hasattr(dataset.meta, "info"):
                    dataset.meta.info.update(info)
                if hasattr(dataset, "total_episodes"):
                    dataset.total_episodes = new_total_eps
                if hasattr(dataset, "total_frames"):
                    dataset.total_frames = info["total_frames"]
                remaining_episode_ids = sorted(new_eps_data.keys())

                # Rebuild episodes_stats.jsonl with new indices
                episodes_stats_path = dataset.root / EPISODES_STATS_PATH
                if episodes_stats_path.is_file():
                    try:
                        old_stats = load_episodes_stats(dataset.root)
                        old_stats.pop(episode_id, None)
                        new_stats = {}
                        for new_idx, old_idx in enumerate(sorted(old_stats.keys())):
                            new_stats[new_idx] = old_stats[old_idx]
                        stats_list = []
                        for idx in sorted(new_stats.keys()):
                            stats_list.append(
                                {
                                    "episode_index": idx,
                                    "stats": serialize_dict(new_stats[idx]),
                                }
                            )
                        write_jsonlines(stats_list, episodes_stats_path)
                        logging.info("Rebuilt episodes_stats.jsonl with contiguous indices")

                        global_stats_path = dataset.root / STATS_PATH
                        if global_stats_path.is_file():
                            vals = list(new_stats.values())
                            if vals:
                                write_stats(aggregate_stats(vals), dataset.root)
                            else:
                                global_stats_path.unlink()
                    except Exception:
                        logging.exception("Failed to update episodes_stats")

                logging.info("Updated all metadata after deleting episode %s", episode_id)
            except Exception:
                logging.exception("Failed to update metadata for episode %s", episode_id)
                yield _sse("error", {"message": "failed to update metadata"})
                return

            # --- 5. Auto-fix parquet indices across remaining episodes ---
            yield _sse(
                "progress", {"step": 5, "total": total_steps, "message": "Repairing episode indices..."}
            )
            try:
                fix_episode_indices(dataset.root, dataset.meta, remaining_episode_ids)
            except Exception:
                logging.exception("Failed to auto-fix episode indices after deleting episode %s", episode_id)
                yield _sse("error", {"message": "delete succeeded but automatic index repair failed"})
                return

            # --- 6. Reindex annotation files ---
            yield _sse("progress", {"step": 6, "total": total_steps, "message": "Reindexing annotations..."})
            # Build old→new index mapping
            idx_map = {}
            for new_idx, old_idx in enumerate(sorted(eps_data.keys())):
                if old_idx != new_idx:
                    idx_map[old_idx] = new_idx
            try:
                reindex_static_after_episode_delete(ds_static, {episode_id}, idx_map)
            except Exception:
                logging.exception("Failed to reindex annotations after deleting episode %s", episode_id)

            # --- 7. Clear caches & update server state ---
            yield _sse("progress", {"step": 7, "total": total_steps, "message": "Clearing caches..."})
            refreshed_episode_ids = _refresh_dataset_after_episode_delete(
                (dataset_namespace, dataset_name),
                dataset,
                ds_static,
            )
            # Update the closure-captured episodes list so sidebar reflects new indices
            if episodes is not None:
                episodes.clear()
                episodes.extend(refreshed_episode_ids)

            # --- 8. Finalize ---
            yield _sse("progress", {"step": 8, "total": total_steps, "message": "Done!"})
            # Navigate to the same index position (which is now the next episode),
            # or the last episode if we deleted the last one
            next_ep = min(episode_id, new_total_eps - 1) if new_total_eps > 0 else None
            _append_operation_log(
                ds_static,
                "episode_delete",
                dataset_key=(dataset_namespace, dataset_name),
                dataset_root=Path(dataset.root),
                episode_ids=[episode_id],
                details={
                    "deleted_episode_id": episode_id,
                    "deleted_length": ep_length,
                    "shifted_count": len(indices_to_shift),
                    "new_total_episodes": new_total_eps,
                    "next_episode": next_ep,
                    "reason": reason,
                },
            )
            yield _sse("done", {"status": "ok", "episode_id": episode_id, "next_episode": next_ep})

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _delete_episode_inplace(dataset, static_folder: Path, episode_id: int, log=None) -> dict:
        """Delete one episode using the same file/meta/cache reindexing steps as viewer DEL."""
        from lerobot.common.datasets.compute_stats import aggregate_stats
        from lerobot.common.datasets.utils import (
            EPISODES_PATH,
            EPISODES_STATS_PATH,
            STATS_PATH,
            load_episodes,
            load_episodes_stats,
            load_info,
            serialize_dict,
            write_info,
            write_jsonlines,
            write_stats,
        )

        def _log(message: str) -> None:
            if log:
                log(message)

        chunks_size = dataset.meta.info.get("chunks_size", 1000)
        image_keys = [key for key, ft in dataset.features.items() if ft["dtype"] == "image"]
        video_keys = list(dataset.meta.video_keys) if hasattr(dataset.meta, "video_keys") else []
        cache_dir = static_folder / "csv"
        remaining_episode_ids: list[int] = []

        eps_data = load_episodes(dataset.root)
        ep_length = 0
        if episode_id in eps_data:
            ep_length = eps_data[episode_id].get("length", 0)

        all_indices = sorted(eps_data.keys())
        if episode_id not in all_indices:
            raise ValueError(f"episode {episode_id} not found in metadata")
        indices_to_shift = [idx for idx in all_indices if idx > episode_id]

        try:
            parquet_path = dataset.root / dataset.meta.get_data_file_path(episode_id)
            if parquet_path.is_file():
                if ep_length == 0:
                    ep_length = pq.read_table(parquet_path, columns=["timestamp"]).num_rows
                parquet_path.unlink()
            for video_key in video_keys:
                video_path = dataset.root / dataset.meta.get_video_file_path(episode_id, video_key)
                if video_path.is_file():
                    video_path.unlink()
            for key in image_keys:
                cached_video = static_folder / "videos" / key / f"episode_{episode_id:06d}_h264.mp4"
                if cached_video.is_file():
                    cached_video.unlink()
            for video_key in video_keys:
                cached_video = static_folder / "videos" / video_key / f"episode_{episode_id:06d}_h264.mp4"
                if cached_video.is_file():
                    cached_video.unlink()
            if cache_dir.is_dir():
                for csv_file in cached_csv_files(cache_dir, f"episode_{episode_id:06d}_ds*.csv"):
                    csv_file.unlink()
            _log(f"Deleted files for episode {episode_id}")
        except Exception as exc:
            logging.exception("Failed to delete files for episode %s", episode_id)
            raise RuntimeError("failed to delete episode files") from exc

        try:
            for old_idx in indices_to_shift:
                new_idx = old_idx - 1
                old_chunk = old_idx // chunks_size
                new_chunk = new_idx // chunks_size

                old_pq = dataset.root / dataset.meta.info["data_path"].format(
                    episode_chunk=old_chunk, episode_index=old_idx
                )
                new_pq = dataset.root / dataset.meta.info["data_path"].format(
                    episode_chunk=new_chunk, episode_index=new_idx
                )
                if old_pq.is_file():
                    new_pq.parent.mkdir(parents=True, exist_ok=True)
                    table = pq.read_table(old_pq)
                    if "episode_index" in table.schema.names:
                        ei_field = table.schema.field("episode_index")
                        ei_col_idx = table.schema.get_field_index("episode_index")
                        table = table.set_column(
                            ei_col_idx,
                            ei_field,
                            pa.array([new_idx] * table.num_rows, type=ei_field.type),
                        )
                    tmp_pq = new_pq.with_suffix(".parquet.tmp")
                    pq.write_table(table, tmp_pq)
                    old_pq.unlink()
                    tmp_pq.rename(new_pq)

                if dataset.meta.info.get("video_path"):
                    for video_key in video_keys:
                        old_video = dataset.root / dataset.meta.info["video_path"].format(
                            episode_chunk=old_chunk, video_key=video_key, episode_index=old_idx
                        )
                        new_video = dataset.root / dataset.meta.info["video_path"].format(
                            episode_chunk=new_chunk, video_key=video_key, episode_index=new_idx
                        )
                        if old_video.is_file():
                            new_video.parent.mkdir(parents=True, exist_ok=True)
                            old_video.rename(new_video)

                for key in image_keys:
                    old_cached = static_folder / "videos" / key / f"episode_{old_idx:06d}_h264.mp4"
                    new_cached = static_folder / "videos" / key / f"episode_{new_idx:06d}_h264.mp4"
                    if old_cached.is_file():
                        old_cached.rename(new_cached)
                for video_key in video_keys:
                    old_cached = static_folder / "videos" / video_key / f"episode_{old_idx:06d}_h264.mp4"
                    new_cached = static_folder / "videos" / video_key / f"episode_{new_idx:06d}_h264.mp4"
                    if old_cached.is_file():
                        old_cached.rename(new_cached)

                if cache_dir.is_dir():
                    for csv_file in cached_csv_files(cache_dir, f"episode_{old_idx:06d}_ds*.csv"):
                        new_name = csv_file.name.replace(f"episode_{old_idx:06d}", f"episode_{new_idx:06d}")
                        csv_file.rename(cache_dir / new_name)
            _log(f"Reindexed {len(indices_to_shift)} following episodes")
        except Exception as exc:
            logging.exception("Failed during reindexing after deleting episode %s", episode_id)
            raise RuntimeError("reindexing failed; dataset may be inconsistent") from exc

        try:
            data_dir = dataset.root / "data"
            if data_dir.is_dir():
                for chunk_dir in sorted(data_dir.iterdir(), reverse=True):
                    if chunk_dir.is_dir() and not any(chunk_dir.iterdir()):
                        chunk_dir.rmdir()
            videos_dir = dataset.root / "videos"
            if videos_dir.is_dir():
                for subdir in videos_dir.iterdir():
                    if subdir.is_dir():
                        for chunk_dir in sorted(subdir.iterdir(), reverse=True):
                            if chunk_dir.is_dir() and not any(chunk_dir.iterdir()):
                                chunk_dir.rmdir()
        except Exception:
            logging.exception("Failed to clean empty directories after deleting episode %s", episode_id)

        try:
            del eps_data[episode_id]
            new_eps_data = {}
            for new_idx, old_idx in enumerate(sorted(eps_data.keys())):
                entry = eps_data[old_idx].copy()
                entry["episode_index"] = new_idx
                new_eps_data[new_idx] = entry
            write_jsonlines(
                [new_eps_data[idx] for idx in sorted(new_eps_data.keys())],
                dataset.root / EPISODES_PATH,
            )
            if hasattr(dataset.meta, "episodes"):
                dataset.meta.episodes.clear()
                dataset.meta.episodes.update(new_eps_data)

            info = load_info(dataset.root)
            new_total_eps = len(new_eps_data)
            info["total_episodes"] = new_total_eps
            info["total_frames"] = max(0, info.get("total_frames", 0) - ep_length)
            if video_keys:
                info["total_videos"] = max(0, info.get("total_videos", 0) - len(video_keys))
            if "splits" in info:
                for split_name in info["splits"]:
                    info["splits"][split_name] = f"0:{new_total_eps}"
            info["total_chunks"] = math.ceil(new_total_eps / chunks_size) if new_total_eps > 0 else 0
            write_info(info, dataset.root)
            if hasattr(dataset.meta, "info"):
                dataset.meta.info.update(info)
            if hasattr(dataset, "total_episodes"):
                dataset.total_episodes = new_total_eps
            if hasattr(dataset, "total_frames"):
                dataset.total_frames = info["total_frames"]
            remaining_episode_ids = sorted(new_eps_data.keys())

            episodes_stats_path = dataset.root / EPISODES_STATS_PATH
            if episodes_stats_path.is_file():
                try:
                    old_stats = load_episodes_stats(dataset.root)
                    old_stats.pop(episode_id, None)
                    new_stats = {}
                    for new_idx, old_idx in enumerate(sorted(old_stats.keys())):
                        new_stats[new_idx] = old_stats[old_idx]
                    write_jsonlines(
                        [
                            {"episode_index": idx, "stats": serialize_dict(new_stats[idx])}
                            for idx in sorted(new_stats.keys())
                        ],
                        episodes_stats_path,
                    )
                    global_stats_path = dataset.root / STATS_PATH
                    if global_stats_path.is_file():
                        values = list(new_stats.values())
                        if values:
                            write_stats(aggregate_stats(values), dataset.root)
                        else:
                            global_stats_path.unlink()
                except Exception:
                    logging.exception("Failed to update episodes_stats after deleting episode %s", episode_id)
            _log(f"Updated metadata after deleting episode {episode_id}")
        except Exception as exc:
            logging.exception("Failed to update metadata for episode %s", episode_id)
            raise RuntimeError("failed to update metadata") from exc

        try:
            fix_episode_indices(dataset.root, dataset.meta, remaining_episode_ids)
        except Exception as exc:
            logging.exception("Failed to repair episode indices after deleting episode %s", episode_id)
            raise RuntimeError("delete succeeded but automatic index repair failed") from exc

        idx_map = {
            old_idx: new_idx for new_idx, old_idx in enumerate(sorted(eps_data.keys())) if old_idx != new_idx
        }
        try:
            reindex_static_after_episode_delete(static_folder, {episode_id}, idx_map, log=_log)
        except Exception:
            logging.exception("Failed to reindex annotations after deleting episode %s", episode_id)

        _clear_episode_dependent_caches()
        if episodes is not None:
            episodes.clear()
            episodes.extend(list(range(len(remaining_episode_ids))))
        return {
            "episode_id": episode_id,
            "new_total_episodes": len(remaining_episode_ids),
            "next_episode": min(episode_id, len(remaining_episode_ids) - 1)
            if remaining_episode_ids
            else None,
        }

    # Auto-flag issue episodes so they appear highlighted in the sidebar
    _issue_eps = _load_issue_episodes(static_folder)
    if _issue_eps:
        current_flagged = set(_load_flagged(static_folder))
        merged = current_flagged | _issue_eps
        if merged != current_flagged and _save_flagged(static_folder, sorted(merged)):
            logging.info("Auto-flagged %d issue episodes", len(merged - current_flagged))

    if precompute_csv and dataset is not None and not precomputed_only:
        cache_dir = static_folder / "csv"
        cache_dir.mkdir(parents=True, exist_ok=True)
        target_episodes = episodes
        if target_episodes is None:
            total_eps = (
                dataset.num_episodes if isinstance(dataset, LeRobotDataset) else dataset.total_episodes
            )
            target_episodes = range(total_eps)
        ds = downsample if downsample and downsample > 1 else 1
        for ep_id in target_episodes:
            cache_path = resolve_cached_csv(cache_dir / f"episode_{ep_id:06d}_ds{ds}.csv")
            if cache_path.is_file():
                continue
            csv_string, _, _, _ = get_episode_data(
                dataset,
                ep_id,
                max_frames=max_frames,
                downsample=downsample,
                data_version=data_version,
            )
            cache_path.write_text(csv_string)
            del csv_string
            gc.collect()
            logging.info("CSV cached: %s", cache_path)

    if start_server:
        import socket

        max_retries = 10
        for attempt in range(max_retries):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex((host, port)) == 0:
                    if attempt < max_retries - 1:
                        logging.warning("Port %d is in use, trying %d...", port, port + 1)
                        port += 1
                        continue
                    raise OSError(f"Ports {port - max_retries + 1}-{port} are all in use.")
            break
        app.run(host=host, port=port, request_handler=QuietRequestHandler, threaded=True)
    return app


def get_ep_csv_fname(episode_id: int):
    ep_csv_fname = f"episode_{episode_id}.csv"
    return ep_csv_fname


def get_episode_data(
    dataset: LeRobotDataset | IterableNamespace | MetaOnlyDataset,
    episode_index: int,
    max_frames: int | None = None,
    downsample: int | None = None,
    data_version: str | None = None,
):
    """Return episode time-series data as CSV for Dygraphs, plus plotting metadata."""
    columns, ignored_columns, selected_columns = get_columns_info(dataset)

    # Try local parquet first for LeRobotDataset and MetaOnlyDataset
    local_parquet = None
    if hasattr(dataset, "root") and hasattr(dataset, "meta"):
        local_parquet = dataset.root / dataset.meta.get_data_file_path(episode_index)
        if not local_parquet.is_file():
            local_parquet = None

    if local_parquet is not None:
        if is_v3_metadata(dataset.meta):
            data = read_episode_table(
                dataset.root,
                dataset.meta,
                episode_index,
                columns=selected_columns,
            ).to_pandas()
            if max_frames is not None:
                data = data.head(max_frames)
        elif max_frames is not None:
            data = _read_parquet_head(local_parquet, selected_columns, max_frames)
        else:
            data = pd.read_parquet(local_parquet, columns=selected_columns)
    elif isinstance(dataset, LeRobotDataset):
        from_idx = dataset.episode_data_index["from"][episode_index]
        to_idx = dataset.episode_data_index["to"][episode_index]
        data = (
            dataset.hf_dataset.select(range(from_idx, to_idx))
            .select_columns(selected_columns)
            .with_format("pandas")
        )
    else:
        repo_id = dataset.repo_id
        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/" + dataset.data_path.format(
            episode_chunk=int(episode_index) // dataset.chunks_size, episode_index=episode_index
        )
        df = pd.read_parquet(url)
        data = df[selected_columns]
        if max_frames is not None:
            data = data.head(max_frames)

    if downsample is not None and downsample > 1:
        data = data.iloc[::downsample].reset_index(drop=True)

    def _get_len(item) -> int:
        if item is None:
            return 0
        try:
            return len(item)
        except TypeError:
            return 1  # scalar value counts as dim=1

    def _normalize_series(series, dim: int, column_name: str) -> np.ndarray:
        rows = []
        for item in series:
            row = [np.nan] * dim
            if item is not None:
                try:
                    values = list(item)
                except TypeError:
                    # scalar value (int, float) — wrap in a list
                    values = [item]
                if len(values) > dim:
                    values = values[:dim]
                row[: len(values)] = values
            rows.append(row)
        return normalize_gripper_columns(np.asarray(rows), column_name, data_version)

    data_arrays = []
    for col in selected_columns[1:]:
        if isinstance(dataset, LeRobotDataset):
            fallback_dim = dataset.meta.shapes[col][0]
        else:
            shape = _get_feature_shape(dataset.features[col])
            fallback_dim = shape[0] if shape else 0
        series = data[col]
        actual_dim = max((_get_len(item) for item in series), default=0)
        dim = actual_dim if actual_dim > 0 else fallback_dim

        # Ensure column names match actual data length
        col_entry = next((c for c in columns if c["key"] == col), None)
        if col_entry is not None and len(col_entry["value"]) != dim:
            col_entry["value"] = [f"{col}_{i}" for i in range(dim)]

        data_arrays.append(_normalize_series(series, dim, col))

    # Refresh header in case we resized columns to match actual data
    header = ["timestamp"]
    for col_entry in columns:
        header += col_entry["value"]

    rows = np.hstack((np.expand_dims(data["timestamp"], axis=1), *data_arrays)).tolist()

    # Convert data to CSV string
    csv_buffer = StringIO()
    csv_writer = csv.writer(csv_buffer)
    # Write header
    csv_writer.writerow(header)
    # Write data rows
    csv_writer.writerows(rows)
    csv_string = csv_buffer.getvalue()

    return csv_string, columns, ignored_columns, len(data)


def get_episode_video_paths(dataset: LeRobotDataset, ep_index: int) -> list[str]:
    # Video paths are stored in frame metadata; the first frame identifies the episode files.
    first_frame_idx = dataset.episode_data_index["from"][ep_index].item()
    return [
        dataset.hf_dataset.select_columns(key)[first_frame_idx][key]["path"]
        for key in dataset.meta.video_keys
    ]


def get_episode_language_instruction(dataset: LeRobotDataset, ep_index: int) -> list[str]:
    # Return no instruction when the feature is absent.
    if "language_instruction" not in dataset.features:
        return None

    # The episode instruction is constant, so read it from the first frame.
    first_frame_idx = dataset.episode_data_index["from"][ep_index].item()

    language_instruction = dataset.hf_dataset[first_frame_idx]["language_instruction"]
    # Some Open-X records store the TensorFlow tensor representation instead of the raw string.
    return language_instruction.removeprefix("tf.Tensor(b'").removesuffix("', shape=(), dtype=string)")


def get_dataset_info(repo_id: str) -> IterableNamespace:
    response = requests.get(
        f"https://huggingface.co/datasets/{repo_id}/resolve/main/meta/info.json", timeout=5
    )
    response.raise_for_status()  # Raises an HTTPError for bad responses
    dataset_info = response.json()
    dataset_info["repo_id"] = repo_id
    return IterableNamespace(dataset_info)


def visualize_dataset_html(
    dataset: LeRobotDataset | MetaOnlyDataset | IterableNamespace | None,
    episodes: list[int] | None = None,
    output_dir: Path | None = None,
    serve: bool = True,
    host: str = "127.0.0.1",
    port: int = 9091,
    force_override: bool = False,
    max_frames: int | None = None,
    prepare_videos: int | bool = False,
    downsample: int | None = None,
    precompute_csv: int | bool = False,
    precomputed_only: int | bool = False,
    annotate: bool = False,
    datasets_root: Path | None = None,
    data_version: str | None = None,
    console_mode: str = CONSOLE_MODE_FULL,
    legacy_mutations_enabled: bool = False,
    protected_source_roots: list[Path] | None = None,
    database_url: str | None = None,
    allow_registration: bool = False,
    remote_cache_root: Path | None = None,
) -> Path | None:
    init_logging()

    template_dir = Path(__file__).resolve().parent / "templates"

    if output_dir is None:
        # mkdtemp creates a unique directory but does not remove it automatically.
        output_dir = tempfile.mkdtemp(prefix="lerobot_visualize_dataset_")

    output_dir = Path(output_dir)
    if output_dir.exists():
        if force_override:
            shutil.rmtree(output_dir)
        else:
            logging.info(f"Output directory already exists. Loading from it: '{output_dir}'")

    output_dir.mkdir(parents=True, exist_ok=True)

    static_dir = output_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    if dataset is None:
        if serve:
            run_server(
                dataset=None,
                episodes=None,
                max_frames=max_frames,
                prepare_videos=False,
                downsample=downsample,
                precompute_csv=False,
                precomputed_only=bool(precomputed_only),
                host=host,
                port=port,
                static_folder=static_dir,
                template_folder=template_dir,
                annotate=annotate,
                datasets_root=datasets_root,
                data_version=data_version,
                console_mode=console_mode,
                legacy_mutations_enabled=legacy_mutations_enabled,
                protected_source_roots=protected_source_roots,
                database_url=database_url,
                allow_registration=allow_registration,
                remote_cache_root=remote_cache_root,
            )
    else:
        # Symlink source MP4 files into the served output directory.
        if isinstance(dataset, LeRobotDataset) or hasattr(dataset, "meta"):
            ln_videos_dir = static_dir / "videos"
            if ln_videos_dir.is_symlink() and not ln_videos_dir.exists():
                try:
                    ln_videos_dir.unlink()
                except OSError:
                    logging.exception("Could not remove broken videos symlink: %s", ln_videos_dir)
            if not ln_videos_dir.exists():
                if len(dataset.meta.video_keys) > 0:
                    ln_videos_dir.symlink_to((dataset.root / "videos").resolve().as_posix())
                else:
                    ln_videos_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = static_dir / "viewer_manifest.json"
            if not manifest_path.is_file():
                try:
                    manifest_episodes = episodes
                    if manifest_episodes is None:
                        manifest_episodes = sorted(int(ep) for ep in getattr(dataset.meta, "episodes", {}))
                    image_keys = [
                        key for key, feature in dataset.features.items() if feature.get("dtype") == "image"
                    ]
                    write_viewer_manifest(
                        root=Path(dataset.root),
                        repo_id=dataset.repo_id,
                        meta=dataset.meta,
                        episodes=manifest_episodes,
                        image_keys=image_keys,
                        static_dir=static_dir,
                        data_version=_normalize_data_version(
                            data_version
                            or resolve_processing_profile(
                                Path(dataset.root),
                                dataset.features,
                            ).legacy_data_version
                        ),
                        downsample=downsample,
                    )
                except Exception:
                    logging.exception("Could not write viewer manifest to %s", static_dir)

        if serve:
            run_server(
                dataset=dataset,
                episodes=episodes,
                max_frames=max_frames,
                prepare_videos=bool(prepare_videos),
                downsample=downsample,
                precompute_csv=bool(precompute_csv),
                precomputed_only=bool(precomputed_only),
                host=host,
                port=port,
                static_folder=static_dir,
                template_folder=template_dir,
                annotate=annotate,
                datasets_root=datasets_root,
                data_version=data_version,
                console_mode=console_mode,
                legacy_mutations_enabled=legacy_mutations_enabled,
                protected_source_roots=protected_source_roots,
                database_url=database_url,
                allow_registration=allow_registration,
                remote_cache_root=remote_cache_root,
            )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--repo-id",
        type=str,
        default=None,
        help="Name of hugging face repositery containing a LeRobotDataset dataset (e.g. `lerobot/pusht` for https://huggingface.co/datasets/lerobot/pusht).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "Root directory for a dataset stored locally (e.g. `--root data`). "
            "If used without `--repo-id`, the dataset is loaded directly from this path."
        ),
    )
    parser.add_argument(
        "--load-from-hf-hub",
        type=int,
        default=0,
        help="Load videos and parquet files from HF Hub rather than local system.",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        nargs="*",
        default=None,
        help="Episode indices to visualize (e.g. `0 1 5 6` to load episodes of index 0, 1, 5 and 6). By default loads all episodes.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory path to write html files and kickoff a web server. By default write them to 'outputs/visualize_dataset/REPO_ID'.",
    )
    parser.add_argument(
        "--serve",
        type=int,
        default=1,
        help="Launch web server.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Web host used by the http server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9091,
        help="Web port used by the http server.",
    )
    parser.add_argument(
        "--force-override",
        type=int,
        default=0,
        help="Delete the output directory if it exists already.",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Limit the number of frames loaded per episode (useful for quick debugging).",
    )
    parser.add_argument(
        "--prepare-videos",
        type=int,
        default=0,
        help="Precompute mp4 videos from image streams and serve them.",
    )
    parser.add_argument(
        "--downsample",
        type=int,
        default=None,
        help="Downsample time series by keeping one every N frames (e.g. 5).",
    )
    parser.add_argument(
        "--precompute-csv",
        type=int,
        default=0,
        help="Precompute CSV cache for selected episodes on startup.",
    )
    parser.add_argument(
        "--precomputed-only",
        type=int,
        default=0,
        help="Only use precomputed CSV/videos. Do not generate on the fly.",
    )
    parser.add_argument(
        "--data-version",
        type=str,
        choices=[DATA_VERSION_DVT1, DATA_VERSION_DVT2],
        default=None,
        help=(
            "Override the robot/Stage profile. By default the portable dataset profile is used; "
            "dimensions are only a legacy fallback."
        ),
    )
    parser.add_argument(
        "--mode",
        dest="console_mode",
        type=str,
        choices=[CONSOLE_MODE_FULL, CONSOLE_MODE_VISUALIZE],
        default=CONSOLE_MODE_FULL,
        help="Home console feature set: full platform or visualize-only cache/viewer/analysis.",
    )
    parser.add_argument(
        "--console-mode",
        dest="console_mode",
        type=str,
        choices=[CONSOLE_MODE_FULL, CONSOLE_MODE_VISUALIZE],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--enable-legacy-mutations",
        dest="legacy_mutations_enabled",
        type=int,
        choices=[0, 1],
        default=0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--protected-source-root",
        dest="protected_source_roots",
        type=Path,
        action="append",
        default=[],
        help="Protect datasets below this path from in-place mutation; repeat as needed.",
    )

    parser.add_argument(
        "--tolerance-s",
        type=float,
        default=1e-4,
        help=(
            "Tolerance in seconds used to ensure data timestamps respect the dataset fps value"
            "This is argument passed to the constructor of LeRobotDataset and maps to its tolerance_s constructor argument"
            "If not given, defaults to 1e-4."
        ),
    )

    args = parser.parse_args()
    kwargs = vars(args)
    repo_id = kwargs.pop("repo_id")
    load_from_hf_hub = kwargs.pop("load_from_hf_hub")
    root = kwargs.pop("root")
    tolerance_s = kwargs.pop("tolerance_s")

    dataset = None
    precomputed_only = bool(kwargs.get("precomputed_only", False))
    if root is not None and not repo_id:
        if load_from_hf_hub:
            raise ValueError("--load-from-hf-hub requires --repo-id and cannot be used with a local root.")
        if not root.exists():
            raise FileNotFoundError(f"Local dataset root does not exist: {root}")
        info_path = root / "meta" / "info.json"
        if not info_path.is_file():
            raise FileNotFoundError(f"Missing dataset metadata at: {info_path}")
        repo_id = f"local/{root.name or 'dataset'}"
        dataset = (
            MetaOnlyDataset(repo_id, root=root)
            if precomputed_only
            else LeRobotDataset(repo_id, root=root, tolerance_s=tolerance_s)
        )
    elif repo_id:
        if precomputed_only:
            dataset = MetaOnlyDataset(repo_id, root=root)
        else:
            dataset = (
                LeRobotDataset(repo_id, root=root, tolerance_s=tolerance_s)
                if not load_from_hf_hub
                else get_dataset_info(repo_id)
            )

    visualize_dataset_html(dataset, **kwargs)


if __name__ == "__main__":
    main()

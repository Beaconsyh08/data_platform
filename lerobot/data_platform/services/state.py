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

"""References to the single console runtime owned by the composition root."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class ConsoleState:
    columns_info_cache: dict
    status_cache_ttl_s: float
    task_caches: dict
    viewer_cache_inventory_cache: dict
    control_plane_store: Any
    dataset: Any
    datasets_index: dict
    datasets_registry: dict
    episodes_by_key: dict
    primary_key: tuple[str, str] | None
    registry_state: dict
    remote_cache_root: Path | None
    server_state: dict
    static_folder: Path
    light_cache_status_cache: dict
    viewer_tag_map_cache: dict
    issue_episodes_cache: dict
    flagged_episodes_cache: dict
    annotation_issues_by_episode_cache: dict
    flag_sidecar_json_cache: dict
    construction_status_cache: dict
    tagging_status_cache: dict
    embedding_count_cache: dict
    jsonl_count_cache: dict
    metadata_factory: Callable

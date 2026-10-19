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

"""Dataset probes and shared console constants; no Flask application state."""

import re
from pathlib import Path

from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
from lerobot.common.datasets.utils import IterableNamespace
from lerobot.data_platform.cache_files import (
    cached_csv_files,
)
from lerobot.data_platform.dataset_metadata import MetaOnlyDataset
from lerobot.data_platform.precompute.timeseries import (
    DATA_VERSION_DVT1,
    DATA_VERSION_DVT2,
)


def _find_any_cached_csv(cache_dir: Path, episode_id: int) -> Path | None:
    pattern = f"episode_{episode_id:06d}_ds*.csv"
    candidates = cached_csv_files(cache_dir, pattern)
    if not candidates:
        return None

    def _ds_value(path: Path) -> int:
        match = re.search(r"_ds(\d+)\.csv(?:\.gz)?$", path.name)
        return int(match.group(1)) if match else 10**9

    return min(candidates, key=_ds_value)


def _normalize_data_version(value: str | None) -> str:
    normalized = str(value or "").upper()
    return normalized if normalized in {DATA_VERSION_DVT1, DATA_VERSION_DVT2} else None


def _is_dataset_root(path: Path) -> bool:
    try:
        return path.is_dir() and (path / "meta" / "info.json").is_file()
    except OSError:
        return False


def _get_feature_shape(feature) -> tuple:
    if isinstance(feature, dict):
        shape = feature.get("shape")
        if isinstance(shape, int):
            return (shape,)
        return tuple(shape) if shape is not None else ()
    if hasattr(feature, "shape"):
        return tuple(feature.shape)
    return ()


def _is_exist_label_feature(column_name: str, feature: dict) -> bool:
    if column_name != "exist_label":
        return False
    dtype = str(feature.get("dtype", ""))
    return dtype.startswith(("float", "int", "uint")) or dtype in {"bool", "boolean"}


def _is_plot_feature(column_name: str, feature: dict) -> bool:
    return feature.get("dtype") in ["float32", "int32"] or _is_exist_label_feature(column_name, feature)


def get_columns_info(dataset: LeRobotDataset | IterableNamespace | MetaOnlyDataset):
    columns = []
    # subtask_state is excluded because the precompute pipeline exports it as a
    # task-dependent normalized "stage" series for plotting.
    _exclude = {"timestamp", "subtask_state"}
    selected_columns = [
        col for col, ft in dataset.features.items() if _is_plot_feature(col, ft) and col not in _exclude
    ]

    ignored_columns = []
    filtered_columns = []
    for column_name in selected_columns:
        shape = _get_feature_shape(dataset.features[column_name])
        shape_dim = len(shape)
        if shape_dim > 1:
            ignored_columns.append(column_name)
        else:
            filtered_columns.append(column_name)
    selected_columns = filtered_columns

    for column_name in selected_columns:
        if isinstance(dataset, LeRobotDataset):
            dim_state = dataset.meta.shapes[column_name][0]
        else:
            shape = _get_feature_shape(dataset.features[column_name])
            dim_state = shape[0] if shape else 0

        if "names" in dataset.features[column_name] and dataset.features[column_name]["names"]:
            column_names = dataset.features[column_name]["names"]
            while not isinstance(column_names, list):
                column_names = list(column_names.values())[0]
            if not isinstance(column_names, list) or len(column_names) != dim_state:
                column_names = [f"{column_name}_{i}" for i in range(dim_state)]
        elif column_name == "exist_label" and dim_state == 1:
            column_names = ["exist_label"]
        else:
            column_names = [f"{column_name}_{i}" for i in range(dim_state)]
        columns.append({"key": column_name, "value": column_names})

    selected_columns.insert(0, "timestamp")
    return columns, ignored_columns, selected_columns


def _repo_id_from_key(dataset_key: tuple[str, str]) -> str:
    return f"{dataset_key[0]}/{dataset_key[1]}"


def _repo_key(repo_id: str) -> tuple[str, str]:
    if "/" not in repo_id:
        repo_id = f"local/{repo_id}"
    return tuple(repo_id.split("/", 1))

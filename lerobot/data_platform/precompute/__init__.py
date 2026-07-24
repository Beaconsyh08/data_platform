"""Shared helpers for local dataset precompute workflows."""

from lerobot.data_platform.precompute.annotation import (
    assign_subtask_states,
    compute_subtask_boundaries,
    compute_quality_flags,
    get_columns_info,
    series_to_2d,
    write_episode_csv,
)
from lerobot.data_platform.precompute.analysis import (
    build_dataset_analysis,
    infer_target_object,
    infer_task_scene,
    parse_canonical_task,
    read_analysis_cache,
    write_analysis_cache,
)
from lerobot.data_platform.precompute.image_io import (
    cached_image_bytes,
    get_parquet_file,
    get_row_group_offsets,
    iter_image_bytes,
    read_image_bytes,
)
from lerobot.data_platform.precompute.mutations import (
    fix_episode_indices,
    update_episode_stats_for_subtask_state,
    update_info_features,
    write_subtask_state_to_parquet,
    write_subtask_text_to_parquet,
)
from lerobot.data_platform.precompute.video import encode_episode_video, encode_with_ffmpeg

__all__ = [
    "assign_subtask_states",
    "build_dataset_analysis",
    "cached_image_bytes",
    "compute_subtask_boundaries",
    "compute_quality_flags",
    "encode_episode_video",
    "encode_with_ffmpeg",
    "fix_episode_indices",
    "get_columns_info",
    "get_parquet_file",
    "get_row_group_offsets",
    "infer_target_object",
    "infer_task_scene",
    "iter_image_bytes",
    "parse_canonical_task",
    "read_analysis_cache",
    "read_image_bytes",
    "series_to_2d",
    "update_episode_stats_for_subtask_state",
    "update_info_features",
    "write_episode_csv",
    "write_analysis_cache",
    "write_subtask_state_to_parquet",
    "write_subtask_text_to_parquet",
]

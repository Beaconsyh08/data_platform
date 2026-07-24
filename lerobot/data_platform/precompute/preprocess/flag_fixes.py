from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.data_platform.precompute.annotation import QUALITY_FLAG_TYPE
from lerobot.data_platform.precompute.mutations import fix_episode_indices
from lerobot.data_platform.precompute.preprocess.common import (
    PreprocessResult,
    ProgressCallback,
    emit,
    format_data_path,
    load_json,
    load_jsonl,
    validate_dataset_root,
    write_json,
    write_jsonl,
)
from lerobot.data_platform.precompute.preprocess.quality_flags import QUALITY_FLAGGED_EPISODES
from lerobot.data_platform.precompute.timeseries import DATA_VERSION_DVT2, infer_data_version_from_features


FLAG_FIX_TRIM_EARLY_GRIPPER = "trim_early_gripper_first_frame"
FLAG_FIX_STUCK_CLOSED_ACTION = "fix_stuck_closed_action"
FLAG_FIX_STATE_GRIPPER_TRANSITION_ACTION = "fix_state_gripper_transition_action"
FLAG_FIX_DELETE_ALL_FLAGGED = "delete_all_flagged"
FLAG_FIX_STATE_ACTION_LEAD_SECONDS = 0.1


class _MetaLite:
    def __init__(self, info: dict, episodes: dict[int, dict]):
        self.info = info
        self.episodes = episodes

    def get_data_file_path(self, episode_id: int) -> Path:
        return format_data_path(self.info, episode_id)


def _load_json_any(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _issue_episode(issue: dict) -> int | None:
    try:
        return int(issue.get("episode"))
    except (TypeError, ValueError):
        return None


def _load_quality_issues(static_dir: Path, *, reason: str | None = None) -> list[dict]:
    issues = _load_json_any(Path(static_dir) / "annotation_issues.json")
    if not isinstance(issues, list):
        return []
    out = []
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("type") != QUALITY_FLAG_TYPE:
            continue
        if reason is not None and issue.get("reason") != reason:
            continue
        if _issue_episode(issue) is not None:
            out.append(issue)
    return out


def _flag_set(path: Path) -> set[int]:
    data = _load_json_any(path)
    values = data.get("flagged_episodes") if isinstance(data, dict) else data if isinstance(data, list) else []
    out = set()
    for value in values or []:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out


def load_flagged_episode_ids(static_dir: Path) -> list[int]:
    return sorted(_flag_set(Path(static_dir) / "flagged_episodes.json"))


def _delete_episode_cache(static_dir: Path, episode_id: int) -> None:
    static_dir = Path(static_dir)
    csv_dir = static_dir / "csv"
    if csv_dir.is_dir():
        for path in csv_dir.glob(f"episode_{episode_id:06d}_ds*.csv"):
            path.unlink()
    videos_dir = static_dir / "videos"
    if videos_dir.is_dir():
        for path in videos_dir.glob(f"*/episode_{episode_id:06d}_h264.mp4"):
            path.unlink()


def _replace_column(table: pa.Table, name: str, values, value_type: pa.DataType | None = None) -> pa.Table:
    field = table.schema.field(name)
    idx = table.column_names.index(name)
    return table.set_column(idx, field, pa.array(values, type=value_type or field.type))


def _trim_first_frame(root: Path, info: dict, episodes_by_id: dict[int, dict], episode_id: int) -> int:
    parquet_path = root / format_data_path(info, episode_id)
    if not parquet_path.is_file():
        return 0
    table = pq.read_table(parquet_path)
    if table.num_rows <= 1:
        return 0
    table = table.slice(1)
    new_rows = int(table.num_rows)
    if "timestamp" in table.column_names and new_rows:
        timestamps = np.asarray(table["timestamp"].to_pylist(), dtype=np.float64)
        timestamps = timestamps - float(timestamps[0])
        table = _replace_column(table, "timestamp", timestamps.tolist())
    if "frame_index" in table.column_names:
        table = _replace_column(table, "frame_index", list(range(new_rows)))
    tmp_path = parquet_path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path)
    tmp_path.replace(parquet_path)
    if episode_id in episodes_by_id:
        episodes_by_id[episode_id]["length"] = new_rows
    return 1


def _raw_closed_action_value(action_array: np.ndarray, gripper_index: int, data_version: str) -> float:
    if str(data_version).upper() == DATA_VERSION_DVT2 and action_array.shape[1] >= 19:
        return 100.0
    if gripper_index < action_array.shape[1]:
        finite = action_array[:, gripper_index][np.isfinite(action_array[:, gripper_index])]
        if finite.size and float(np.nanmax(np.abs(finite))) > 1.5:
            return 100.0
    return 1.0


def _raw_gripper_action_value(action_array: np.ndarray, gripper_index: int, closed: int, data_version: str) -> float:
    return _raw_closed_action_value(action_array, gripper_index, data_version) if int(closed) else 0.0


def _backup_parquet(root: Path, parquet_path: Path, backup_dir: Path) -> Path:
    rel_path = parquet_path.relative_to(root)
    backup_path = backup_dir / rel_path
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(parquet_path, backup_path)
    return backup_path


def _fix_stuck_action(root: Path, info: dict, issue: dict, data_version: str) -> bool:
    episode_id = int(issue["episode"])
    metrics = issue.get("metrics") or {}
    gripper_index = int(metrics.get("gripper_index", 7))
    parquet_path = root / format_data_path(info, episode_id)
    if not parquet_path.is_file():
        return False
    table = pq.read_table(parquet_path)
    if "action" not in table.column_names:
        return False
    values = table["action"].to_pylist()
    action = np.asarray(values, dtype=np.float64)
    if action.ndim != 2 or gripper_index >= action.shape[1]:
        return False
    action[:, gripper_index] = _raw_closed_action_value(action, gripper_index, data_version)
    field = table.schema.field("action")
    table = _replace_column(table, "action", action.tolist(), field.type)
    tmp_path = parquet_path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path)
    tmp_path.replace(parquet_path)
    return True


def _fix_state_transition_action(
    root: Path,
    info: dict,
    issue: dict,
    data_version: str,
    backup_dir: Path,
) -> dict:
    episode_id = int(issue["episode"])
    metrics = issue.get("metrics") or {}
    events = [event for event in metrics.get("events") or [] if isinstance(event, dict)]
    if not events:
        events = [
            {
                "frame": frame,
                "gripper_index": 7,
                "from_state": 0,
                "to_state": 1,
            }
            for frame in issue.get("frames") or []
        ]
    parquet_path = root / format_data_path(info, episode_id)
    if not parquet_path.is_file():
        return {"fixed": False, "reason": "missing_parquet"}
    table = pq.read_table(parquet_path)
    if "action" not in table.column_names:
        return {"fixed": False, "reason": "missing_action"}
    values = table["action"].to_pylist()
    action = np.asarray(values, dtype=np.float64)
    if action.ndim != 2 or action.shape[0] == 0:
        return {"fixed": False, "reason": "invalid_action"}

    fps = float(info.get("fps") or 0)
    lead_frames = max(1, int(round(fps * FLAG_FIX_STATE_ACTION_LEAD_SECONDS))) if fps > 0 else 1
    changed = False
    applied = []
    events_by_gripper: dict[int, list[dict]] = defaultdict(list)
    for event in events:
        try:
            gripper_index = int(event.get("gripper_index", 7))
            frame = int(event["frame"])
        except (KeyError, TypeError, ValueError):
            continue
        if gripper_index < 0 or gripper_index >= action.shape[1] or frame <= 0:
            continue
        copied = dict(event)
        copied["gripper_index"] = gripper_index
        copied["frame"] = min(frame, action.shape[0] - 1)
        events_by_gripper[gripper_index].append(copied)

    for gripper_index, gripper_events in events_by_gripper.items():
        gripper_events.sort(key=lambda item: int(item["frame"]))
        starts = [max(0, int(event["frame"]) - lead_frames) for event in gripper_events]
        for idx, event in enumerate(gripper_events):
            frame = int(event["frame"])
            start = starts[idx]
            end = starts[idx + 1] if idx + 1 < len(starts) else action.shape[0]
            if end <= start:
                end = min(action.shape[0], start + 1)
            from_state = int(event.get("from_state", 0))
            to_state = int(event.get("to_state", 1))
            pre_start = max(0, start - lead_frames)
            if pre_start < start:
                old_value = _raw_gripper_action_value(action, gripper_index, from_state, data_version)
                if not np.allclose(action[pre_start:start, gripper_index], old_value, equal_nan=True):
                    action[pre_start:start, gripper_index] = old_value
                    changed = True
            new_value = _raw_gripper_action_value(action, gripper_index, to_state, data_version)
            if not np.allclose(action[start:end, gripper_index], new_value, equal_nan=True):
                action[start:end, gripper_index] = new_value
                changed = True
            applied.append(
                {
                    "gripper_index": int(gripper_index),
                    "state_frame": int(frame),
                    "action_frame": int(start),
                    "end_frame": int(end),
                    "from_state": int(from_state),
                    "to_state": int(to_state),
                }
            )

    if not changed:
        return {"fixed": False, "reason": "no_change", "applied": applied}

    backup_path = _backup_parquet(root, parquet_path, backup_dir)
    field = table.schema.field("action")
    table = _replace_column(table, "action", action.tolist(), field.type)
    tmp_path = parquet_path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path)
    tmp_path.replace(parquet_path)
    return {
        "fixed": True,
        "backup_path": str(backup_path),
        "lead_frames": int(lead_frames),
        "lead_seconds": float(FLAG_FIX_STATE_ACTION_LEAD_SECONDS),
        "applied": applied,
    }


def _write_meta_lengths(root: Path, info: dict, episodes_by_id: dict[int, dict]) -> None:
    rows = [episodes_by_id[idx] for idx in sorted(episodes_by_id)]
    write_jsonl(root / "meta" / "episodes.jsonl", rows)
    total_frames = sum(int(row.get("length") or 0) for row in rows)
    info["total_frames"] = total_frames
    write_json(root / "meta" / "info.json", info)


def _remove_resolved_quality_issues(static_dir: Path, *, reason: str, episode_ids: set[int]) -> dict:
    static_dir = Path(static_dir)
    issues_path = static_dir / "annotation_issues.json"
    issues = _load_json_any(issues_path)
    if not isinstance(issues, list):
        issues = []
    retained = []
    removed = []
    for issue in issues:
        episode = _issue_episode(issue) if isinstance(issue, dict) else None
        if (
            isinstance(issue, dict)
            and issue.get("type") == QUALITY_FLAG_TYPE
            and issue.get("reason") == reason
            and episode in episode_ids
        ):
            removed.append(issue)
            continue
        retained.append(issue)
    write_json(issues_path, retained)

    remaining_quality = [
        issue for issue in retained if isinstance(issue, dict) and issue.get("type") == QUALITY_FLAG_TYPE
    ]
    next_auto = {_issue_episode(issue) for issue in remaining_quality}
    next_auto = {episode for episode in next_auto if episode is not None}
    previous_auto = _flag_set(static_dir / QUALITY_FLAGGED_EPISODES)
    existing_flagged = _flag_set(static_dir / "flagged_episodes.json")
    manual_or_other_auto = existing_flagged - previous_auto
    combined = manual_or_other_auto | next_auto

    reason_map: dict[str, list[dict]] = defaultdict(list)
    for issue in remaining_quality:
        episode = _issue_episode(issue)
        if episode is None:
            continue
        reason_item = {
            "type": str(issue.get("type") or QUALITY_FLAG_TYPE),
            "reason": str(issue.get("reason") or "unknown"),
        }
        if "frames" in issue:
            reason_item["frames"] = issue.get("frames") or []
        if "metrics" in issue:
            reason_item["metrics"] = issue.get("metrics") or {}
        reason_map[str(episode)].append(reason_item)

    reason_counts = Counter(str(issue.get("reason") or "unknown") for issue in remaining_quality)
    write_json(
        static_dir / QUALITY_FLAGGED_EPISODES,
        {
            "flagged_episodes": sorted(next_auto),
            "flag_reasons": dict(sorted(reason_map.items())),
            "summary": {
                "quality_episode_count": len(next_auto),
                "quality_issue_count": len(remaining_quality),
                "reason_counts": dict(sorted(reason_counts.items())),
            },
        },
    )
    write_json(static_dir / "flagged_episodes.json", {"flagged_episodes": sorted(combined)})
    return {"removed_issues": len(removed), "remaining_quality_episodes": len(next_auto)}


def run_flag_fix(
    root: Path,
    static_dir: Path,
    fix_kind: str,
    *,
    episodes: list[int] | None = None,
    data_version: str | None = None,
    progress_callback: ProgressCallback = None,
) -> PreprocessResult:
    root = validate_dataset_root(Path(root))
    static_dir = Path(static_dir).expanduser()
    info = load_json(root / "meta" / "info.json")
    selected_data_version = str(data_version or infer_data_version_from_features(info.get("features") or {})).upper()
    episode_rows = load_jsonl(root / "meta" / "episodes.jsonl")
    episodes_by_id = {int(row["episode_index"]): row for row in episode_rows}
    allowed = set(int(ep) for ep in episodes) if episodes else None

    if fix_kind == FLAG_FIX_TRIM_EARLY_GRIPPER:
        reason = "early_gripper_transition"
        issues = _load_quality_issues(static_dir, reason=reason)
        episode_ids = sorted({int(issue["episode"]) for issue in issues if allowed is None or int(issue["episode"]) in allowed})
        emit(progress_callback, status="running", current=0, total=len(episode_ids), message=f"Trimming first frame for {len(episode_ids)} episodes")
        fixed = 0
        fixed_episode_ids: set[int] = set()
        for idx, episode_id in enumerate(episode_ids, start=1):
            did_fix = _trim_first_frame(root, info, episodes_by_id, episode_id)
            fixed += did_fix
            if did_fix:
                fixed_episode_ids.add(episode_id)
                _delete_episode_cache(static_dir, episode_id)
            emit(progress_callback, status="running", current=idx, total=len(episode_ids), episode=episode_id, message=f"Trimmed episode {episode_id}")
        if fixed_episode_ids:
            _write_meta_lengths(root, info, episodes_by_id)
            fix_episode_indices(root, _MetaLite(info, episodes_by_id), sorted(episodes_by_id))
        cleanup = _remove_resolved_quality_issues(static_dir, reason=reason, episode_ids=fixed_episode_ids)
        summary = {"fix_kind": fix_kind, "episodes": sorted(fixed_episode_ids), "attempted_episodes": episode_ids, "fixed": fixed, **cleanup}

    elif fix_kind == FLAG_FIX_STUCK_CLOSED_ACTION:
        reason = "stuck_closed_gripper_no_action"
        issues = _load_quality_issues(static_dir, reason=reason)
        selected_issues = [issue for issue in issues if allowed is None or int(issue["episode"]) in allowed]
        emit(progress_callback, status="running", current=0, total=len(selected_issues), message=f"Fixing stuck gripper action for {len(selected_issues)} issues")
        fixed_episodes: set[int] = set()
        fixed = 0
        for idx, issue in enumerate(selected_issues, start=1):
            episode_id = int(issue["episode"])
            if _fix_stuck_action(root, info, issue, selected_data_version):
                fixed += 1
                fixed_episodes.add(episode_id)
                _delete_episode_cache(static_dir, episode_id)
            emit(progress_callback, status="running", current=idx, total=len(selected_issues), episode=episode_id, message=f"Updated episode {episode_id}")
        cleanup = _remove_resolved_quality_issues(static_dir, reason=reason, episode_ids=fixed_episodes)
        summary = {"fix_kind": fix_kind, "episodes": sorted(fixed_episodes), "fixed": fixed, **cleanup}

    elif fix_kind == FLAG_FIX_STATE_GRIPPER_TRANSITION_ACTION:
        reason = "state_gripper_transition_without_action"
        issues = _load_quality_issues(static_dir, reason=reason)
        selected_issues = [issue for issue in issues if allowed is None or int(issue["episode"]) in allowed]
        emit(progress_callback, status="running", current=0, total=len(selected_issues), message=f"Adding gripper action lead signals for {len(selected_issues)} issues")
        backup_dir = (
            static_dir
            / "flag_fix_backups"
            / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{FLAG_FIX_STATE_GRIPPER_TRANSITION_ACTION}"
        )
        fixed_episodes: set[int] = set()
        fixed = 0
        details = []
        for idx, issue in enumerate(selected_issues, start=1):
            episode_id = int(issue["episode"])
            result = _fix_state_transition_action(root, info, issue, selected_data_version, backup_dir)
            details.append({"episode": episode_id, **result})
            if result.get("fixed"):
                fixed += 1
                fixed_episodes.add(episode_id)
                _delete_episode_cache(static_dir, episode_id)
            emit(progress_callback, status="running", current=idx, total=len(selected_issues), episode=episode_id, message=f"Updated episode {episode_id}")
        backup_manifest = None
        if fixed_episodes:
            backup_manifest = backup_dir / "manifest.json"
            write_json(
                backup_manifest,
                {
                    "fix_kind": fix_kind,
                    "source_root": str(root),
                    "data_version": selected_data_version,
                    "episodes": sorted(fixed_episodes),
                    "lead_seconds": FLAG_FIX_STATE_ACTION_LEAD_SECONDS,
                    "details": details,
                    "restore_note": "To roll back manually, copy each backed up parquet over the same relative path under source_root.",
                },
            )
        cleanup = _remove_resolved_quality_issues(static_dir, reason=reason, episode_ids=fixed_episodes)
        summary = {
            "fix_kind": fix_kind,
            "episodes": sorted(fixed_episodes),
            "fixed": fixed,
            "backup_manifest": str(backup_manifest) if backup_manifest else None,
            "details": details,
            **cleanup,
        }
    else:
        raise ValueError(f"Unsupported flag fix: {fix_kind}")

    emit(progress_callback, status="done", current=summary.get("fixed", 0), total=max(1, summary.get("fixed", 0)), message="Flag fix complete")
    return PreprocessResult(
        op=f"flag_fix:{fix_kind}",
        src_roots=[root],
        out_root=root,
        repo_id=root.name,
        total_episodes=len(summary.get("episodes", [])),
        total_frames=int(info.get("total_frames") or 0),
        dry_run=False,
        summary=summary,
    )

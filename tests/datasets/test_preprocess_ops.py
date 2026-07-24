import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.data_platform.precompute.preprocess import (
    run_convert_action,
    run_drop_field,
    run_fix_prompt_prepositions,
    run_flag_fix,
    run_lowercase_prompts,
    run_merge,
    run_smooth_action,
    run_split,
    run_standardize_dataset,
    run_subtract,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _make_dataset(root: Path, task: str = "pick duck", task_index: int = 0) -> None:
    (root / "data" / "chunk-000").mkdir(parents=True)
    (root / "meta").mkdir(parents=True)
    info = {
        "robot_type": "dummy",
        "fps": 10,
        "codebase_version": "v2.1",
        "total_episodes": 2,
        "total_frames": 5,
        "total_tasks": 1,
        "total_chunks": 1,
        "chunks_size": 1000,
        "splits": {"train": "0:2"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "features": {
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            "action": {"dtype": "float32", "shape": [17], "names": None},
            "state": {"dtype": "float32", "shape": [17], "names": None},
            "old_field": {"dtype": "float32", "shape": [1], "names": None},
        },
    }
    (root / "meta" / "info.json").write_text(json.dumps(info))
    _write_jsonl(root / "meta" / "tasks.jsonl", [{"task_index": task_index, "task": task}])
    _write_jsonl(
        root / "meta" / "episodes.jsonl",
        [
            {"episode_index": 0, "tasks": [task], "length": 2},
            {"episode_index": 1, "tasks": [task], "length": 3},
        ],
    )
    _write_jsonl(
        root / "meta" / "episodes_stats.jsonl",
        [
            {"episode_index": 0, "stats": {"episode_index": {"min": [0], "max": [0], "mean": [0.0], "std": [0.0]}, "index": {"min": [0], "max": [1], "mean": [0.5], "std": [0.0]}, "action": {"min": [[0.0] * 17], "max": [[1.0] * 17], "mean": [[0.5] * 17], "std": [[0.1] * 17]}, "state": {"min": [[0.0] * 17], "max": [[1.0] * 17], "mean": [[0.5] * 17], "std": [[0.1] * 17]}, "old_field": {"min": [0.0], "max": [1.0], "mean": [0.5], "std": [0.1]}}},
            {"episode_index": 1, "stats": {"episode_index": {"min": [1], "max": [1], "mean": [1.0], "std": [0.0]}, "index": {"min": [2], "max": [4], "mean": [3.0], "std": [0.0]}, "action": {"min": [[0.0] * 17], "max": [[1.0] * 17], "mean": [[0.5] * 17], "std": [[0.1] * 17]}, "state": {"min": [[0.0] * 17], "max": [[1.0] * 17], "mean": [[0.5] * 17], "std": [[0.1] * 17]}, "old_field": {"min": [0.0], "max": [1.0], "mean": [0.5], "std": [0.1]}}},
        ],
    )
    offset = 0
    for episode_index, length in [(0, 2), (1, 3)]:
        df = pd.DataFrame(
            {
                "episode_index": [episode_index] * length,
                "frame_index": list(range(length)),
                "index": list(range(offset, offset + length)),
                "timestamp": [i / 10 for i in range(length)],
                "task_index": [task_index] * length,
                "action": [[float(i)] * 17 for i in range(length)],
                "state": [[float(i)] * 17 for i in range(length)],
                "old_field": [float(i) for i in range(length)],
            }
        )
        df.to_parquet(root / "data" / "chunk-000" / f"episode_{episode_index:06d}.parquet", index=False)
        offset += length


def test_convert_action_and_drop_field(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src)

    converted = run_convert_action(src, tmp_path / "action16")
    info = json.loads((converted.out_root / "meta" / "info.json").read_text())
    assert info["features"]["action"]["shape"] == [16]
    assert info["features"]["state"]["shape"] == [16]
    row = pd.read_parquet(converted.out_root / "data" / "chunk-000" / "episode_000000.parquet").iloc[0]
    assert len(row["action"]) == 16

    dropped = run_drop_field(src, tmp_path / "drop_old", field_name="old_field")
    info = json.loads((dropped.out_root / "meta" / "info.json").read_text())
    assert "old_field" not in info["features"]
    assert "old_field" not in pd.read_parquet(dropped.out_root / "data" / "chunk-000" / "episode_000000.parquet").columns


def test_smooth_action_rewrites_action_and_stats(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src)

    smoothed = run_smooth_action(src, tmp_path / "smooth", window=3, workers=2)
    df = pd.read_parquet(smoothed.out_root / "data" / "chunk-000" / "episode_000001.parquet")
    assert abs(df.iloc[0]["action"][0] - (1 / 3)) < 1e-6
    assert abs(df.iloc[1]["action"][0] - 1.0) < 1e-6
    assert abs(df.iloc[2]["action"][0] - (5 / 3)) < 1e-6
    assert abs(df.iloc[0]["state"][0] - (1 / 3)) < 1e-6
    stats_rows = [
        json.loads(line)
        for line in (smoothed.out_root / "meta" / "episodes_stats.jsonl").read_text().splitlines()
        if line.strip()
    ]
    ep1_stats = next(row for row in stats_rows if row["episode_index"] == 1)["stats"]["action"]
    assert abs(ep1_stats["min"][0] - (1 / 3)) < 1e-6
    assert ep1_stats["count"] == [3]
    smooth_meta = json.loads((smoothed.out_root / "meta" / "preprocess_smooth_action.json").read_text())
    assert smooth_meta["source_root"] == str(src)
    assert smooth_meta["window"] == 3
    assert smooth_meta["workers"] == 2
    assert smooth_meta["fields"] == ["action", "state"]
    assert smooth_meta["smooth_state"] is True


def test_standardize_dataset_normalizes_trims_and_drops_depth(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src)
    info_path = src / "meta" / "info.json"
    info = json.loads(info_path.read_text())
    info["features"]["action"]["shape"] = [20]
    info["features"]["state"]["shape"] = [19]
    info["features"]["head_depth"] = {"dtype": "float32", "shape": [1], "names": None}
    info["features"]["observation.images.left_wrist_depth"] = {"dtype": "float32", "shape": [1], "names": None}
    info_path.write_text(json.dumps(info))

    for parquet_path in sorted((src / "data").rglob("*.parquet")):
        df = pd.read_parquet(parquet_path)
        actions = []
        states = []
        for frame_idx in range(len(df)):
            action = [float(frame_idx)] * 20
            action[7] = 50.0
            action[15] = 120.0
            state = [float(frame_idx)] * 19
            state[7] = 25.0
            state[15] = 75.0
            actions.append(action)
            states.append(state)
        df["action"] = actions
        df["state"] = states
        df["head_depth"] = [1.0] * len(df)
        df["observation.images.left_wrist_depth"] = [2.0] * len(df)
        df.to_parquet(parquet_path, index=False)

    result = run_standardize_dataset(src, tmp_path / "standardized")
    out_info = json.loads((result.out_root / "meta" / "info.json").read_text())
    assert out_info["features"]["action"]["shape"] == [16]
    assert out_info["features"]["state"]["shape"] == [16]
    assert out_info["features"]["exist_label"] == {"dtype": "int32", "shape": [1], "names": None}
    assert "head_depth" not in out_info["features"]
    assert "observation.images.left_wrist_depth" not in out_info["features"]

    out_df = pd.read_parquet(result.out_root / "data" / "chunk-000" / "episode_000000.parquet")
    assert len(out_df.iloc[0]["action"]) == 16
    assert len(out_df.iloc[0]["state"]) == 16
    assert abs(out_df.iloc[0]["action"][7] - 0.5) < 1e-6
    assert abs(out_df.iloc[0]["action"][15] - 1.2) < 1e-6
    assert abs(out_df.iloc[0]["state"][7] - 0.25) < 1e-6
    assert abs(out_df.iloc[0]["state"][15] - 0.75) < 1e-6
    assert out_df["exist_label"].tolist() == [1, 1]
    assert "head_depth" not in out_df.columns
    assert "observation.images.left_wrist_depth" not in out_df.columns
    stats_rows = [
        json.loads(line)
        for line in (result.out_root / "meta" / "episodes_stats.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert stats_rows[0]["stats"]["exist_label"]["min"] == [1.0]


def test_standardize_dataset_preserves_existing_exist_label(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src)
    info_path = src / "meta" / "info.json"
    info = json.loads(info_path.read_text())
    info["features"]["exist_label"] = {"dtype": "int64", "shape": [1], "names": None}
    info_path.write_text(json.dumps(info))

    for parquet_path in sorted((src / "data").rglob("*.parquet")):
        df = pd.read_parquet(parquet_path)
        df["exist_label"] = [idx % 2 for idx in range(len(df))]
        df.to_parquet(parquet_path, index=False)

    result = run_standardize_dataset(src, tmp_path / "standardized")
    out_info = json.loads((result.out_root / "meta" / "info.json").read_text())
    assert out_info["features"]["exist_label"] == {"dtype": "int64", "shape": [1], "names": None}

    out_df = pd.read_parquet(result.out_root / "data" / "chunk-000" / "episode_000001.parquet")
    assert out_df["exist_label"].tolist() == [0, 1, 0]


def test_flag_fix_adds_action_lead_for_state_only_gripper_transition(tmp_path: Path):
    src = tmp_path / "src"
    static_dir = tmp_path / "static"
    _make_dataset(src)

    info_path = src / "meta" / "info.json"
    info = json.loads(info_path.read_text())
    info["total_frames"] = 23
    info_path.write_text(json.dumps(info))
    _write_jsonl(
        src / "meta" / "episodes.jsonl",
        [
            {"episode_index": 0, "tasks": ["pick duck"], "length": 20},
            {"episode_index": 1, "tasks": ["pick duck"], "length": 3},
        ],
    )
    frames = list(range(20))
    action = [[0.0] * 17 for _ in frames]
    state = [[0.0] * 17 for _ in frames]
    for idx in range(10, 20):
        state[idx][7] = 1.0
    pd.DataFrame(
        {
            "episode_index": [0] * 20,
            "frame_index": frames,
            "index": frames,
            "timestamp": [idx / 10 for idx in frames],
            "task_index": [0] * 20,
            "action": action,
            "state": state,
            "old_field": [0.0] * 20,
        }
    ).to_parquet(src / "data" / "chunk-000" / "episode_000000.parquet", index=False)
    (static_dir / "csv").mkdir(parents=True)
    (static_dir / "csv" / "episode_000000_ds1.csv").write_text("stale")
    (static_dir / "annotation_issues.json").write_text(
        json.dumps(
            [
                {
                    "episode": 0,
                    "type": "quality_flag",
                    "reason": "state_gripper_transition_without_action",
                    "frames": [10],
                    "metrics": {
                        "events": [
                            {
                                "gripper_index": 7,
                                "frame": 10,
                                "from_state": 0,
                                "to_state": 1,
                            }
                        ]
                    },
                }
            ]
        )
    )
    (static_dir / "quality_flagged_episodes.json").write_text(
        json.dumps(
            {
                "flagged_episodes": [0],
                "flag_reasons": {"0": [{"type": "quality_flag", "reason": "state_gripper_transition_without_action"}]},
            }
        )
    )
    (static_dir / "flagged_episodes.json").write_text(json.dumps({"flagged_episodes": [0]}))

    result = run_flag_fix(src, static_dir, "fix_state_gripper_transition_action", data_version="DVT1")

    fixed = pd.read_parquet(src / "data" / "chunk-000" / "episode_000000.parquet")
    assert fixed.iloc[8]["action"][7] == 0.0
    assert fixed.iloc[9]["action"][7] == 1.0
    assert fixed.iloc[10]["action"][7] == 1.0
    assert not (static_dir / "csv" / "episode_000000_ds1.csv").exists()
    assert json.loads((static_dir / "annotation_issues.json").read_text()) == []
    assert json.loads((static_dir / "flagged_episodes.json").read_text()) == {"flagged_episodes": []}
    backup_manifest = Path(result.summary["backup_manifest"])
    assert backup_manifest.is_file()
    backup = json.loads(backup_manifest.read_text())
    assert backup["episodes"] == [0]
    assert Path(backup["details"][0]["backup_path"]).is_file()


def test_fix_prompt_prepositions_rewrites_absolute_and_relative_metadata(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src, task="pick up the yellow duck to the left")
    _write_jsonl(
        src / "meta" / "tasks.jsonl",
        [
            {"task_index": 0, "task": "pick up the yellow duck to the left"},
            {"task_index": 1, "task": "pick up the brown dog on the right of the green dinosaur"},
        ],
    )
    _write_jsonl(
        src / "meta" / "episodes.jsonl",
        [
            {"episode_index": 0, "tasks": ["pick up the yellow duck to the left"], "length": 2},
            {"episode_index": 1, "tasks": ["pick up the brown dog on the right of the green dinosaur"], "length": 3},
        ],
    )

    dry_run = run_fix_prompt_prepositions(src, dry_run=True)
    assert dry_run.summary["total_replacements"] == 4

    result = run_fix_prompt_prepositions(src)
    assert result.summary["total_replacements"] == 4
    tasks = [json.loads(line)["task"] for line in (src / "meta" / "tasks.jsonl").read_text().splitlines()]
    assert tasks == [
        "pick up the yellow duck on the left",
        "pick up the brown dog to the right of the green dinosaur",
    ]
    episodes = [json.loads(line)["tasks"][0] for line in (src / "meta" / "episodes.jsonl").read_text().splitlines()]
    assert episodes == tasks
    assert list((src / "meta" / "prompt_rewrite_backups").glob("*"))


def test_lowercase_prompts_rewrites_task_and_episode_metadata(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src, task="Pick up the Yellow Duck")
    _write_jsonl(
        src / "meta" / "tasks.jsonl",
        [
            {"task_index": 0, "task": "Pick up the Yellow Duck"},
            {"task_index": 1, "task": "pick up the green dinosaur"},
        ],
    )
    _write_jsonl(
        src / "meta" / "episodes.jsonl",
        [
            {"episode_index": 0, "tasks": ["Pick up the Yellow Duck"], "length": 2},
            {"episode_index": 1, "tasks": ["pick up the green dinosaur"], "length": 3},
        ],
    )

    dry_run = run_lowercase_prompts(src, dry_run=True)
    assert dry_run.summary["total_replacements"] == 2
    assert json.loads((src / "meta" / "tasks.jsonl").read_text().splitlines()[0])["task"] == "Pick up the Yellow Duck"

    result = run_lowercase_prompts(src)
    assert result.summary["total_replacements"] == 2
    tasks = [json.loads(line)["task"] for line in (src / "meta" / "tasks.jsonl").read_text().splitlines()]
    assert tasks == ["pick up the yellow duck", "pick up the green dinosaur"]
    episodes = [json.loads(line)["tasks"][0] for line in (src / "meta" / "episodes.jsonl").read_text().splitlines()]
    assert episodes == tasks
    assert list((src / "meta" / "prompt_rewrite_backups").glob("*"))


def test_lowercase_prompts_remaps_task_index_and_pending_viewer_prompts(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src, task="Pick up the Yellow Duck")
    _write_jsonl(
        src / "meta" / "tasks.jsonl",
        [
            {"task_index": 0, "task": "Pick up the Yellow Duck"},
            {"task_index": 1, "task": "pick up the yellow duck"},
        ],
    )
    _write_jsonl(
        src / "meta" / "episodes.jsonl",
        [
            {"episode_index": 0, "tasks": ["Pick up the Yellow Duck"], "length": 2},
            {"episode_index": 1, "tasks": ["pick up the yellow duck"], "length": 3},
        ],
    )
    parquet_path = src / "data" / "chunk-000" / "episode_000001.parquet"
    table = pq.read_table(parquet_path)
    task_field = table.schema.field("task_index")
    table = table.set_column(
        table.column_names.index("task_index"),
        task_field,
        pa.array([1] * table.num_rows, type=task_field.type),
    )
    pq.write_table(table, parquet_path)

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "prompt_assignments_pending.json").write_text(
        json.dumps(
            {
                "version": 1,
                "assignments": [
                    {
                        "episode_index": 0,
                        "selected_task": "Pick up the Yellow Duck",
                        "updated_at": 1,
                        "source": "viewer_cache_only",
                    }
                ],
            }
        )
    )

    result = run_lowercase_prompts(src, static_dir=static_dir)

    assert result.summary["removed_duplicate_task_rows"] == 1
    assert result.summary["parquet_task_index_files_changed"] == 1
    assert result.summary["parquet_task_index_values_changed"] == 3
    assert result.summary["pending_prompt_assignments_changed"] == 1
    tasks = [json.loads(line) for line in (src / "meta" / "tasks.jsonl").read_text().splitlines()]
    assert tasks == [{"task_index": 0, "task": "pick up the yellow duck"}]
    info = json.loads((src / "meta" / "info.json").read_text())
    assert info["total_tasks"] == 1
    table = pq.read_table(parquet_path, columns=["task_index"])
    assert set(table["task_index"].to_pylist()) == {0}
    pending = json.loads((static_dir / "prompt_assignments_pending.json").read_text())
    assert pending["assignments"][0]["selected_task"] == "pick up the yellow duck"


def test_split_and_merge_reindex_metadata(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src)

    split = run_split(src, tmp_path / "split", episode_range="1:2")
    info = json.loads((split.out_root / "meta" / "info.json").read_text())
    assert info["total_episodes"] == 1
    assert info["total_frames"] == 3
    df = pd.read_parquet(split.out_root / "data" / "chunk-000" / "episode_000000.parquet")
    assert df["episode_index"].tolist() == [0, 0, 0]
    assert df["index"].tolist() == [0, 1, 2]

    merged = run_merge([split.out_root, split.out_root], tmp_path / "merge")
    info = json.loads((merged.out_root / "meta" / "info.json").read_text())
    assert info["total_episodes"] == 2
    assert info["total_frames"] == 6
    df = pd.read_parquet(merged.out_root / "data" / "chunk-000" / "episode_000001.parquet")
    assert df["episode_index"].tolist() == [1, 1, 1]
    assert df["index"].tolist() == [3, 4, 5]


def test_merge_rejects_action_shape_mismatch_before_writing(tmp_path: Path):
    src_a = tmp_path / "src_a"
    src_b = tmp_path / "src_b"
    _make_dataset(src_a)
    _make_dataset(src_b)
    info_path = src_b / "meta" / "info.json"
    info = json.loads(info_path.read_text())
    info["features"]["action"]["shape"] = [18]
    info_path.write_text(json.dumps(info))

    try:
        run_merge([src_a, src_b], tmp_path / "merge_bad")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("run_merge should reject mismatched action shapes")

    assert "action shape mismatch" in message
    assert "please standardize datasets first" in message
    assert not (tmp_path / "merge_bad").exists()


def test_merge_can_exclude_source_episodes_before_reindexing(tmp_path: Path):
    src_a = tmp_path / "src_a"
    src_b = tmp_path / "src_b"
    _make_dataset(src_a, task="pick duck", task_index=0)
    _make_dataset(src_b, task="pick dog", task_index=0)

    merged = run_merge(
        [src_a, src_b],
        tmp_path / "merge_excluding",
        exclude_episodes=[[0], [1]],
        workers=2,
    )
    info = json.loads((merged.out_root / "meta" / "info.json").read_text())
    episodes = [json.loads(line) for line in (merged.out_root / "meta" / "episodes.jsonl").read_text().splitlines()]
    first = pd.read_parquet(merged.out_root / "data" / "chunk-000" / "episode_000000.parquet")
    second = pd.read_parquet(merged.out_root / "data" / "chunk-000" / "episode_000001.parquet")

    assert info["total_episodes"] == 2
    assert info["total_frames"] == 5
    assert [row["episode_index"] for row in episodes] == [0, 1]
    assert first["episode_index"].tolist() == [0, 0, 0]
    assert first["index"].tolist() == [0, 1, 2]
    assert second["episode_index"].tolist() == [1, 1]
    assert second["index"].tolist() == [3, 4]
    assert merged.summary["deleted_source_episodes"] == {str(src_a): [0], str(src_b): [1]}


def test_subtract_matches_split_subset_by_content_fingerprint(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src)
    split = run_split(src, tmp_path / "split", episode_range="1:2")

    subtracted = run_subtract(src, [split.out_root], tmp_path / "subtract")
    info = json.loads((subtracted.out_root / "meta" / "info.json").read_text())
    episodes = [json.loads(line) for line in (subtracted.out_root / "meta" / "episodes.jsonl").read_text().splitlines()]
    out_df = pd.read_parquet(subtracted.out_root / "data" / "chunk-000" / "episode_000000.parquet")
    src_df = pd.read_parquet(src / "data" / "chunk-000" / "episode_000001.parquet")

    assert info["total_episodes"] == 1
    assert info["total_frames"] == 2
    assert episodes == [{"episode_index": 0, "tasks": ["pick duck"], "length": 2}]
    assert out_df["episode_index"].tolist() == [0, 0]
    assert out_df["index"].tolist() == [0, 1]
    assert src_df["episode_index"].tolist() == [1, 1, 1]
    assert subtracted.summary["removed_base_episodes"] == [1]
    assert subtracted.summary["kept_base_episode_count"] == 1


def test_subtract_carries_static_artifacts_for_kept_episodes(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src)
    split = run_split(src, tmp_path / "split", episode_range="0:1")
    static = tmp_path / "static"
    out_static = tmp_path / "out_static"

    (static / "csv").mkdir(parents=True)
    (static / "csv" / "episode_000000_ds1.csv").write_text(
        "timestamp,episode_index,frame_index,index,stage\n0.0,0,0,0,delete\n"
    )
    (static / "csv" / "episode_000001_ds1.csv").write_text(
        "timestamp,episode_index,frame_index,index,stage\n0.0,1,0,2,keep\n0.1,1,1,3,keep\n"
    )
    (static / "labeling").mkdir(parents=True)
    _write_jsonl(static / "labeling" / "labels.jsonl", [{"episode_index": 0, "task": "delete"}])
    _write_jsonl(static / "labeling" / "labels_reviewed.jsonl", [{"episode_index": 1, "task": "keep"}])
    (static / "tagging").mkdir(parents=True)
    _write_jsonl(static / "tagging" / "tags.jsonl", [{"episode_index": 1, "tags": {"arm": "right"}}])
    (static / "flagged_episodes.json").write_text(json.dumps({"flagged_episodes": [0, 1]}))
    (static / "annotation_issues.json").write_text(json.dumps([{"episode": 1, "type": "quality_flag"}]))
    (static / "trim_annotations.json").write_text(json.dumps({"0": {"start": 0.0}, "1": {"start": 1.0}}))
    (static / "subtask_annotations.json").write_text(json.dumps({"1": {"stage": "keep"}}))
    (static / "prompt_assignments_pending.json").write_text(
        json.dumps(
            {
                "version": 1,
                "assignments": [
                    {"episode_index": 0, "selected_task": "delete"},
                    {"episode_index": 1, "selected_task": "keep"},
                ],
            }
        )
    )

    result = run_subtract(
        src,
        [split.out_root],
        tmp_path / "subtract_artifacts",
        src_static_dir=static,
        out_static_dir=out_static,
    )

    assert result.summary["artifacts"]["csv_files"] == 1
    assert (out_static / "csv" / "episode_000000_ds1.csv").read_text().splitlines() == [
        "timestamp,episode_index,frame_index,index,stage",
        "0.0,0,0,0,keep",
        "0.1,0,1,1,keep",
    ]
    reviewed = [json.loads(line) for line in (out_static / "labeling" / "labels_reviewed.jsonl").read_text().splitlines()]
    tags = [json.loads(line) for line in (out_static / "tagging" / "tags.jsonl").read_text().splitlines()]
    flags = json.loads((out_static / "flagged_episodes.json").read_text())
    issues = json.loads((out_static / "annotation_issues.json").read_text())
    pending = json.loads((out_static / "prompt_assignments_pending.json").read_text())

    assert not (out_static / "labeling" / "labels.jsonl").exists()
    assert reviewed == [{"episode_index": 0, "task": "keep"}]
    assert tags == [{"episode_index": 0, "tags": {"arm": "right"}}]
    assert flags["flagged_episodes"] == [0]
    assert issues == [{"episode": 0, "type": "quality_flag"}]
    assert json.loads((out_static / "trim_annotations.json").read_text()) == {"0": {"start": 1.0}}
    assert json.loads((out_static / "subtask_annotations.json").read_text()) == {"0": {"stage": "keep"}}
    assert pending["assignments"] == [{"episode_index": 0, "selected_task": "keep"}]


def test_subtract_dry_run_reports_removals_without_writing(tmp_path: Path):
    src = tmp_path / "src"
    _make_dataset(src)
    split = run_split(src, tmp_path / "split", episode_range="1:2")
    out_root = tmp_path / "subtract_dry"

    result = run_subtract(src, [split.out_root], out_root, dry_run=True)

    assert not out_root.exists()
    assert result.dry_run is True
    assert result.summary["removed_base_episodes"] == [1]
    assert result.summary["removed_base_episode_count"] == 1
    assert result.summary["unmatched_subtract_fingerprint_count"] == 0


def test_merge_preserves_depth_array_columns(tmp_path: Path):
    src_a = tmp_path / "src_a"
    src_b = tmp_path / "src_b"
    _make_dataset(src_a)
    _make_dataset(src_b)
    for src in (src_a, src_b):
        info_path = src / "meta" / "info.json"
        info = json.loads(info_path.read_text())
        info["features"]["head_depth"] = {"dtype": "float32", "shape": [2], "names": None}
        info_path.write_text(json.dumps(info))
        for parquet_path in sorted((src / "data").rglob("*.parquet")):
            table = pq.read_table(parquet_path)
            values = [[1.0, 2.0] for _ in range(table.num_rows)]
            table = table.append_column(
                pa.field("head_depth", pa.list_(pa.float32(), 2)),
                pa.array(values, type=pa.list_(pa.float32(), 2)),
            )
            pq.write_table(table, parquet_path)

    merged = run_merge([src_a, src_b], tmp_path / "merge_depth")
    out_table = pq.read_table(merged.out_root / "data" / "chunk-000" / "episode_000000.parquet")

    assert "head_depth" in out_table.column_names
    assert out_table.schema.field("head_depth").type == pa.list_(pa.float32(), 2)
    assert out_table["head_depth"][0].as_py() == [1.0, 2.0]


def test_merge_carries_static_artifacts_with_episode_remap(tmp_path: Path):
    src_a = tmp_path / "src_a"
    src_b = tmp_path / "src_b"
    _make_dataset(src_a, task="pick duck", task_index=0)
    _make_dataset(src_b, task="pick dog", task_index=0)
    static_a = tmp_path / "static_a"
    static_b = tmp_path / "static_b"

    (static_a / "csv").mkdir(parents=True)
    (static_a / "csv" / "episode_000001_ds1.csv").write_text(
        "timestamp,episode_index,frame_index,index,stage\n0.0,1,0,2,0\n0.1,1,1,3,1\n"
    )
    (static_b / "videos" / "front").mkdir(parents=True)
    (static_b / "videos" / "front" / "episode_000000_h264.mp4").write_bytes(b"video")

    (static_a / "labeling").mkdir(parents=True)
    _write_jsonl(static_a / "labeling" / "labels.jsonl", [{"episode_index": 1, "task": "pick duck", "selected": {"left": 1}}])
    _write_jsonl(static_a / "labeling" / "labels_reviewed.jsonl", [{"episode_index": 1, "task": "pick duck", "selected": {"left": 2}}])
    (static_a / "labeling" / "source.json").write_text(json.dumps({"backend": "grounding_dino"}))
    (static_a / "labeling" / "vis").mkdir(parents=True)
    (static_a / "labeling" / "vis" / "episode_000001.png").write_bytes(b"png")

    (static_b / "tagging").mkdir(parents=True)
    _write_jsonl(static_b / "tagging" / "tags.jsonl", [{"episode_index": 0, "tags": {"background": "sofa"}}])
    (static_b / "tagging" / "source.json").write_text(json.dumps({"output_variant": "latest"}))
    (static_b / "flagged_episodes.json").write_text(json.dumps({"flagged_episodes": [0]}))
    (static_b / "quality_flagged_episodes.json").write_text(
        json.dumps(
            {
                "flagged_episodes": [0],
                "flag_reasons": {"0": [{"type": "quality_flag", "reason": "early_gripper_transition"}]},
            }
        )
    )
    (static_b / "annotation_issues.json").write_text(
        json.dumps([{"episode": 0, "type": "quality_flag", "reason": "early_gripper_transition"}])
    )
    (static_a / "trim_annotations.json").write_text(json.dumps({"1": {"start": 1.0}}))
    (static_b / "subtask_annotations.json").write_text(json.dumps({"0": {"stage": "place"}}))
    (static_b / "prompt_assignments_pending.json").write_text(
        json.dumps({"version": 1, "assignments": [{"episode_index": 0, "selected_task": "pick dog"}]})
    )

    out_static = tmp_path / "out_static"
    result = run_merge(
        [src_a, src_b],
        tmp_path / "merge_artifacts",
        src_static_dirs=[static_a, static_b],
        out_static_dir=out_static,
    )

    assert result.summary["artifacts"]["csv_files"] == 1
    assert result.summary["artifacts"]["video_files"] == 1
    csv_text = (out_static / "csv" / "episode_000001_ds1.csv").read_text()
    assert "0.0,1,0,2,0" in csv_text
    assert (out_static / "videos" / "front" / "episode_000002_h264.mp4").read_bytes() == b"video"
    labels = [json.loads(line) for line in (out_static / "labeling" / "labels.jsonl").read_text().splitlines()]
    assert labels == [{"episode_index": 1, "task": "pick duck", "selected": {"left": 1}}]
    reviewed = [json.loads(line) for line in (out_static / "labeling" / "labels_reviewed.jsonl").read_text().splitlines()]
    assert reviewed[0]["episode_index"] == 1
    tags = [json.loads(line) for line in (out_static / "tagging" / "tags.jsonl").read_text().splitlines()]
    assert tags == [{"episode_index": 2, "tags": {"background": "sofa"}}]
    assert (out_static / "labeling" / "vis" / "episode_000001.png").read_bytes() == b"png"
    flags = json.loads((out_static / "flagged_episodes.json").read_text())
    assert flags["flagged_episodes"] == [2]
    quality_flags = json.loads((out_static / "quality_flagged_episodes.json").read_text())
    assert quality_flags["flagged_episodes"] == [2]
    assert quality_flags["flag_reasons"]["2"][0]["reason"] == "early_gripper_transition"
    issues = json.loads((out_static / "annotation_issues.json").read_text())
    assert issues == [{"episode": 2, "type": "quality_flag", "reason": "early_gripper_transition"}]
    assert json.loads((out_static / "trim_annotations.json").read_text()) == {"1": {"start": 1.0}}
    assert json.loads((out_static / "subtask_annotations.json").read_text()) == {"2": {"stage": "place"}}
    pending = json.loads((out_static / "prompt_assignments_pending.json").read_text())
    assert pending["assignments"] == [{"episode_index": 2, "selected_task": "pick dog"}]

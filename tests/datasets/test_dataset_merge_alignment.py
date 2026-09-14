import csv
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lerobot.data_platform.cli import run_precompute
from lerobot.data_platform.precompute.data_profile import (
    profile_from_data_version,
    resolve_data_profile,
    write_data_profile,
)
from lerobot.data_platform.precompute.dataset_io import V3DatasetMetadata, read_episode_table
from lerobot.data_platform.precompute.preprocess.common import load_json, load_jsonl, write_json, write_jsonl
from lerobot.data_platform.precompute.preprocess.dataset_merge import run_merge
from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3, validate_v3_dataset


def _make_named_dataset(
    root: Path, action_names: list[str], state_names: list[str], *, offset: int = 0, state_key: str = "state"
) -> None:
    semantic_values = {"left": 10, "right": 20, "grip": 30, "body": 90}
    features = {
        key: {"dtype": dtype, "shape": [1], "names": None}
        for key, dtype in (
            ("episode_index", "int64"),
            ("frame_index", "int64"),
            ("index", "int64"),
            ("task_index", "int64"),
            ("timestamp", "float32"),
            ("subtask_state", "int32"),
            ("untouched", "float32"),
        )
    }
    for field, names in (("action", action_names), (state_key, state_names)):
        features[field] = {
            "dtype": "float32",
            "shape": [len(names)],
            "names": names,
            "units": ["rad"] * len(names),
        }
    info = {
        "robot_type": "test_robot",
        "fps": 10,
        "codebase_version": "v2.1",
        "total_episodes": 2,
        "total_frames": 5,
        "total_tasks": 1,
        "total_chunks": 1,
        "chunks_size": 1000,
        "splits": {"train": "0:2"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "features": features,
    }
    write_json(root / "meta" / "info.json", info)
    write_data_profile(
        root, profile_from_data_version("DVT2", features, resolution_source="test", confirmed=True)
    )
    write_jsonl(root / "meta" / "tasks.jsonl", [{"task_index": 0, "task": "test task"}])
    episodes, stats = [], []
    frame_offset = 0
    for episode, length in enumerate((2, 3)):
        columns = {
            "episode_index": pa.array([episode] * length, type=pa.int64()),
            "frame_index": pa.array(list(range(length)), type=pa.int64()),
            "index": pa.array(list(range(frame_offset, frame_offset + length)), type=pa.int64()),
            "task_index": pa.array([0] * length, type=pa.int64()),
            "timestamp": pa.array([frame / 10 for frame in range(length)], type=pa.float32()),
            "subtask_state": pa.array([0] * length, type=pa.int32()),
            "untouched": pa.array([42] * length, type=pa.float32()),
        }
        for field, names in (("action", action_names), (state_key, state_names)):
            values = [[offset + semantic_values[name] + frame for name in names] for frame in range(length)]
            columns[field] = pa.array(values, type=pa.list_(pa.float32(), len(names)))
        path = root / "data" / "chunk-000" / f"episode_{episode:06d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(columns), path)
        episode_stats = {}
        for field, values in columns.items():
            array = np.asarray(values.to_pylist(), dtype=np.float64).reshape(length, -1)
            episode_stats[field] = {
                "min": array.min(axis=0).tolist(),
                "max": array.max(axis=0).tolist(),
                "mean": array.mean(axis=0).tolist(),
                "std": array.std(axis=0).tolist(),
                "count": [length],
            }
        episodes.append({"episode_index": episode, "tasks": ["test task"], "length": length})
        stats.append({"episode_index": episode, "stats": episode_stats})
        frame_offset += length
    write_jsonl(root / "meta" / "episodes.jsonl", episodes)
    write_jsonl(root / "meta" / "episodes_stats.jsonl", stats)


def _snapshot(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    "formats,workers,state_key",
    [
        ("v21", 1, "state"),
        ("v21", 3, "observation.state"),
        ("mixed", 2, "state"),
        ("v3", 2, "state"),
    ],
)
def test_merge_min_aligns_values_stats_and_preserves_sources(tmp_path: Path, formats, workers, state_key):
    roots = [tmp_path / "a", tmp_path / "b"]
    _make_named_dataset(roots[0], ["left", "right", "grip"], ["left", "body", "right"], state_key=state_key)
    _make_named_dataset(
        roots[1], ["right", "body", "grip", "left"], ["right", "left"], offset=100, state_key=state_key
    )
    for position in [1] if formats == "mixed" else [0, 1] if formats == "v3" else []:
        roots[position] = run_convert_v3(roots[position], tmp_path / f"v3_{position}", workers=1).out_root
    snapshots = [_snapshot(root) for root in roots]
    output = tmp_path / "merged"
    result = run_merge(roots, output, dimension_policy="min", workers=workers)
    info = load_json(output / "meta" / "info.json")
    assert info["features"]["action"]["names"] == ["left", "right", "grip"]
    assert info["features"][state_key]["names"] == ["right", "left"]
    assert info["features"]["action"]["shape"] == [3]
    assert info["features"][state_key]["shape"] == [2]
    tables = []
    if formats == "v21":
        tables = [pq.read_table(path) for path in sorted((output / "data").rglob("*.parquet"))]
        episode_stats = [row["stats"] for row in load_jsonl(output / "meta" / "episodes_stats.jsonl")]
    else:
        validate_v3_dataset(output)
        meta = V3DatasetMetadata("local/merged", output)
        tables = [read_episode_table(output, meta, episode) for episode in range(4)]
        episode_stats = [meta.episodes_stats[episode] for episode in range(4)]
        assert pq.read_table(next((output / "data").rglob("*.parquet"))).num_rows == 10
    for episode, table in enumerate(tables):
        offset = 0 if episode < 2 else 100
        expected_action = [
            [offset + 10 + frame, offset + 20 + frame, offset + 30 + frame] for frame in range(table.num_rows)
        ]
        expected_state = [[offset + 20 + frame, offset + 10 + frame] for frame in range(table.num_rows)]
        assert table["action"].to_pylist() == expected_action
        assert table[state_key].to_pylist() == expected_state
        assert table["untouched"].to_pylist() == [42] * table.num_rows
        assert table["episode_index"].to_pylist() == [episode] * table.num_rows
        np.testing.assert_allclose(episode_stats[episode]["action"]["mean"], np.mean(expected_action, axis=0))
    np.testing.assert_allclose(
        load_json(output / "meta" / "stats.json")["action"]["mean"],
        np.mean([row for table in tables for row in table["action"].to_pylist()], axis=0),
    )
    assert resolve_data_profile(output).robot_profile == "h10w_dvt2"
    assert result.summary["dimension_alignment"][1]["fields"]["action"]["source_indices"] == [3, 0, 2]
    assert result.summary["dimension_alignment"][1]["fields"]["action"]["dropped_names"] == ["body"]
    assert [_snapshot(root) for root in roots] == snapshots


def test_merge_min_dry_run_reports_mapping_without_creating_output(tmp_path: Path):
    roots = [tmp_path / "a", tmp_path / "b"]
    _make_named_dataset(roots[0], ["left", "right"], ["left"])
    _make_named_dataset(roots[1], ["right", "body", "left"], ["left"])
    snapshots = [_snapshot(root) for root in roots]
    result = run_merge(roots, tmp_path / "merged", dimension_policy="min", dry_run=True)
    assert result.summary["dimension_alignment"][1]["fields"]["action"]["source_indices"] == [2, 0]
    assert not result.out_root.exists()
    assert [_snapshot(root) for root in roots] == snapshots


@pytest.mark.parametrize(
    "problem",
    [
        "missing_names",
        "duplicate_names",
        "missing_dimension",
        "missing_field",
        "units",
        "robot",
        "gripper",
        "fps",
        "custom_schema",
    ],
)
def test_merge_min_rejects_ambiguous_or_incompatible_metadata(tmp_path: Path, problem):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        _make_named_dataset(root, ["left", "right"], ["left"])
    info = load_json(roots[1] / "meta" / "info.json")
    action = info["features"]["action"]
    if problem == "missing_names":
        action["names"] = None
    elif problem == "duplicate_names":
        action["names"] = ["left", "left"]
    elif problem == "missing_dimension":
        action["names"] = ["left", "body"]
    elif problem == "missing_field":
        del info["features"]["action"]
    elif problem == "units":
        action["units"] = ["deg", "rad"]
    elif problem == "fps":
        info["fps"] = 20
    else:
        profile_path = roots[1] / "meta" / "data_profile.json"
        profile = load_json(profile_path)
        profile[
            {"robot": "robot_profile", "gripper": "gripper_encoding", "custom_schema": "signal_schema"}[
                problem
            ]
        ] = "different"
        write_json(profile_path, profile)
    write_json(roots[1] / "meta" / "info.json", info)
    with pytest.raises(ValueError):
        run_merge(roots, tmp_path / "merged", dimension_policy="min")
    assert not (tmp_path / "merged").exists()


def test_merge_min_reorders_equal_dimensions_and_strict_rejects_order(tmp_path: Path):
    roots = [tmp_path / "a", tmp_path / "b"]
    _make_named_dataset(roots[0], ["left", "right"], ["left"])
    _make_named_dataset(roots[1], ["right", "left"], ["left"])
    info = load_json(roots[1] / "meta" / "info.json")
    info["features"]["action"]["names"] = {"joints": ["right", "left"]}
    write_json(roots[1] / "meta" / "info.json", info)
    with pytest.raises(ValueError, match="names/order mismatch"):
        run_merge(roots, tmp_path / "strict")
    result = run_merge(roots, tmp_path / "min", dimension_policy="min", exclude_episodes=[[1], [1]])
    table = pq.read_table(result.out_root / "data/chunk-000/episode_000001.parquet")
    assert table["action"].to_pylist() == [[10, 20], [11, 21]]
    assert table["index"].to_pylist() == [2, 3]


def test_merge_min_rejects_bad_rows_and_cleans_output(tmp_path: Path):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        _make_named_dataset(root, ["left", "right"], ["left"])
    path = roots[1] / "data/chunk-000/episode_000001.parquet"
    table = pq.read_table(path)
    table = table.set_column(table.column_names.index("action"), "action", pa.array([[1], [2], [3]]))
    pq.write_table(table, path)
    snapshots = [_snapshot(root) for root in roots]
    with pytest.raises(ValueError, match="actual vector dimensions"):
        run_merge(roots, tmp_path / "merged", dimension_policy="min", workers=2)
    assert not (tmp_path / "merged").exists()
    assert [_snapshot(root) for root in roots] == snapshots


def test_merge_min_rebuilds_csv_from_aligned_output(tmp_path: Path):
    roots = [tmp_path / "a", tmp_path / "b"]
    _make_named_dataset(roots[0], ["left", "right"], ["grip"])
    _make_named_dataset(roots[1], ["right", "body", "left"], ["grip"], offset=100)
    source_static = tmp_path / "source_static"
    (source_static / "csv").mkdir(parents=True)
    (source_static / "csv/episode_000000_ds1.csv").write_text("stale,body\n1,2\n")
    output_static = tmp_path / "viewer/static"
    result = run_merge(
        roots,
        tmp_path / "merged",
        dimension_policy="min",
        src_static_dirs=[None, source_static],
        out_static_dir=output_static,
    )
    assert not (output_static / "csv").exists()
    run_precompute(
        root=result.out_root,
        output_dir=output_static.parent,
        prepare_videos=False,
        prepare_workers=1,
        show_progress=False,
    )
    with (output_static / "csv/episode_000002_ds1.csv").open() as handle:
        reader = csv.DictReader(handle)
        row = next(reader)
        assert "body" not in reader.fieldnames
        assert float(row["left"]) == 110
        assert float(row["right"]) == 120
    assert (source_static / "csv/episode_000000_ds1.csv").read_text() == "stale,body\n1,2\n"


@pytest.mark.parametrize("policy", ["strict", "min"])
@pytest.mark.parametrize("names", [None, [], ["joint"], ["joint", "joint"]])
def test_merge_identical_unnamed_signals_preserves_values(tmp_path, policy, names):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        _make_named_dataset(root, ["left", "right"], ["left"])
        info = load_json(root / "meta/info.json")
        info["features"]["action"]["names"] = names
        write_json(root / "meta/info.json", info)
    snapshots = [_snapshot(root) for root in roots]
    output = tmp_path / "merged"
    run_merge(roots, output, dimension_policy=policy)
    assert load_json(output / "meta/info.json")["features"]["action"]["names"] == names
    table = pq.read_table(output / "data/chunk-000/episode_000002.parquet")
    assert table["action"].to_pylist() == [[10, 20], [11, 21]]
    assert [_snapshot(root) for root in roots] == snapshots

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


@pytest.mark.parametrize("policy,target_dim", [("min", 17), ("pad", 20)])
@pytest.mark.parametrize("v3", [False, True])
def test_explicit_prefix_tail_merge_17_and_20(tmp_path, policy, target_dim, v3):
    from lerobot.data_platform.precompute.preprocess.merge_alignment import numeric_stats

    roots = [tmp_path / "short", tmp_path / "wide"]
    mappings = []
    for root, dimension in zip(roots, [17, 20], strict=True):
        _make_named_dataset(root, ["left", "right"], ["grip"])
        info = load_json(root / "meta/info.json")
        names = [f"joint_{i}" for i in range(16)] + [f"extra_{i}" for i in range(dimension - 17)] + ["tail"]
        mappings.append(dict.fromkeys(("action", "state"), names))
        episode_stats = load_jsonl(root / "meta/episodes_stats.jsonl")
        for field in ("action", "state"):
            info["features"][field].update(shape=[dimension], names=[field], units=["rad"] * dimension)
        write_json(root / "meta/info.json", info)
        for episode, path in enumerate(sorted((root / "data").rglob("*.parquet"))):
            table = pq.read_table(path)
            values = [[float(i + 100 * episode) for i in range(dimension)] for _ in range(table.num_rows)]
            for field in ("action", "state"):
                table = table.set_column(
                    table.column_names.index(field),
                    field,
                    pa.array(values, type=pa.list_(pa.float32(), dimension)),
                )
                episode_stats[episode]["stats"][field] = numeric_stats(values)
            pq.write_table(table, path)
        write_jsonl(root / "meta/episodes_stats.jsonl", episode_stats)
    if v3:
        roots[1] = run_convert_v3(roots[1], tmp_path / "wide_v3", workers=1).out_root
    before = [_snapshot(root) for root in roots]
    preview = run_merge(
        roots,
        tmp_path / "preview",
        dry_run=True,
        dimension_policy=policy,
        dimension_names=mappings,
        padding_value=-1 if policy == "pad" else 0,
    )
    assert not (tmp_path / "preview").exists()
    plan = preview.summary["dimension_alignment"][0]["fields"]["action"]
    assert plan["target_dim"] == target_dim
    assert plan["source_indices"][-1] == 16
    if policy == "pad":
        assert plan["source_indices"][16:19] == [None, None, None]
    result = run_merge(
        roots,
        tmp_path / "output",
        dimension_policy=policy,
        dimension_names=mappings,
        padding_value=-1 if policy == "pad" else 0,
        workers=1,
    )
    info = load_json(result.out_root / "meta/info.json")
    assert info["features"]["action"]["shape"] == [target_dim]
    assert info["features"]["action"]["names"][-1] == "tail"
    if v3:
        meta = V3DatasetMetadata(result.repo_id, result.out_root)
        first = read_episode_table(result.out_root, meta, 0)
        third = read_episode_table(result.out_root, meta, 2)
        validate_v3_dataset(result.out_root)
    else:
        first = pq.read_table(result.out_root / "data/chunk-000/episode_000000.parquet")
        third = pq.read_table(result.out_root / "data/chunk-000/episode_000002.parquet")
    expected_short = list(range(16)) + ([-1] * 3 if policy == "pad" else []) + [16]
    expected_wide = list(range(16)) + (list(range(16, 19)) if policy == "pad" else []) + [19]
    for field in ("action", "state"):
        assert first[field].to_pylist()[0] == expected_short
        assert third[field].to_pylist()[0] == expected_wide
        stats = load_json(result.out_root / "meta/stats.json")[field]
        assert len(stats["mean"]) == target_dim
    assert first["untouched"].to_pylist() == [42, 42]
    assert [_snapshot(root) for root in roots] == before


def test_named_padding_aligns_units_and_reports_padded_signals(tmp_path):
    roots = [tmp_path / "short", tmp_path / "wide"]
    _make_named_dataset(roots[0], ["left", "right"], ["grip"])
    _make_named_dataset(roots[1], ["right", "body", "left"], ["grip"])
    result = run_merge(roots, tmp_path / "output", dimension_policy="pad", workers=1)
    first = pq.read_table(result.out_root / "data/chunk-000/episode_000000.parquet")
    assert first["action"].to_pylist()[0] == [20, 0, 10]
    assert result.summary["dimension_alignment"][0]["fields"]["action"]["padded_names"] == ["body"]
    assert load_json(result.out_root / "meta/info.json")["features"]["action"]["units"] == ["rad"] * 3


@pytest.mark.parametrize(
    "mapping", [[{"action": ["x"]}, {}], [{"action": ["x", "x"]}, {}], [{"unknown": ["x"]}, {}]]
)
def test_explicit_mapping_rejects_invalid_names_without_writing(tmp_path, mapping):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        _make_named_dataset(root, ["left", "right"], ["grip"])
    with pytest.raises(ValueError):
        run_merge(roots, tmp_path / "output", dimension_policy="min", dimension_names=mapping)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("dimensions,prefix,tail", [([5, 8], 3, 2), ([9, 12, 10], 6, 3), ([2, 7], 1, 1)])
@pytest.mark.parametrize("policy", ["min", "pad"])
def test_explicit_alignment_supports_arbitrary_dimensions(dimensions, prefix, tail, policy):
    from lerobot.data_platform.precompute.preprocess.merge_alignment import (
        plan_signal_alignment,
        project_signals,
    )

    infos, mappings = [], []
    for dimension in dimensions:
        infos.append(
            {
                "features": {
                    "observation.state": {"shape": [dimension], "dtype": "float32", "names": ["states"]}
                }
            }
        )
        names = (
            [f"head{i}" for i in range(prefix)]
            + [f"middle{i}" for i in range(dimension - prefix - tail)]
            + [f"tail{i}" for i in range(tail)]
        )
        mappings.append({"observation.state": names})
    aligned, projections = plan_signal_alignment(infos, policy, mappings, -7 if policy == "pad" else 0)
    target_dimension = min(dimensions) if policy == "min" else max(dimensions)
    for dimension, info, projection in zip(dimensions, aligned, projections, strict=True):
        assert info["features"]["observation.state"]["shape"] == [target_dimension]
        table = pa.table(
            {"observation.state": pa.array([list(range(dimension))], type=pa.list_(pa.float32(), dimension))}
        )
        output, stats = project_signals(table, projection)
        values = output["observation.state"].to_pylist()[0]
        assert values[:prefix] == list(range(prefix))
        assert values[-tail:] == list(range(dimension - tail, dimension))
        assert len(stats["observation.state"]["mean"]) == target_dimension
        if policy == "pad" and dimension < target_dimension:
            assert values[dimension - tail : target_dimension - tail] == [-7] * (target_dimension - dimension)


@pytest.mark.parametrize("dtype,fill", [("uint8", -1), ("int8", 128), ("int32", 0.5), ("float32", 1e100)])
def test_padding_value_must_fit_signal_type(dtype, fill):
    from lerobot.data_platform.precompute.preprocess.merge_alignment import plan_signal_alignment

    infos = [
        {"features": {"action": {"shape": [len(names)], "names": names, "dtype": dtype}}}
        for names in (["x"], ["x", "y"])
    ]
    with pytest.raises(ValueError, match="padding"):
        plan_signal_alignment(infos, "pad", padding_value=fill)


@pytest.mark.parametrize("policy", ["min", "pad"])
@pytest.mark.parametrize("v3", [False, True])
def test_index_merge_rewrites_data_stats_and_preserves_sources(tmp_path, policy, v3):
    roots = [tmp_path / "a", tmp_path / "b"]
    _make_named_dataset(roots[0], ["left", "right"], ["grip"])
    _make_named_dataset(roots[1], ["left", "body", "right"], ["grip"], offset=100)
    for root in roots:
        info = load_json(root / "meta/info.json")
        info["features"]["action"]["names"] = ["actions"]
        write_json(root / "meta/info.json", info)
    if v3:
        roots[1] = run_convert_v3(roots[1], tmp_path / "v3", workers=1).out_root
    before = [_snapshot(root) for root in roots]
    mappings = [
        {"action": [1] if policy == "min" else [0, None, 1], "state": [0]},
        {"action": [2] if policy == "min" else [0, 1, 2], "state": [0]},
    ]
    output = tmp_path / "merged"
    preview = run_merge(roots, output, dimension_policy=policy, dimension_indices=mappings, dry_run=True)
    assert not output.exists()
    assert (
        preview.summary["dimension_alignment"][0]["fields"]["action"]["source_indices"]
        == mappings[0]["action"]
    )
    result = run_merge(roots, output, dimension_policy=policy, dimension_indices=mappings, workers=1)
    info = load_json(output / "meta/info.json")
    assert info["features"]["action"]["shape"] == [1 if policy == "min" else 3]
    for episode, expected in (
        (0, [20] if policy == "min" else [10, 0, 20]),
        (2, [120] if policy == "min" else [110, 190, 120]),
    ):
        table = (
            read_episode_table(output, V3DatasetMetadata("local/merged", output), episode)
            if v3
            else pq.read_table(output / f"data/chunk-000/episode_{episode:06d}.parquet")
        )
        assert table["action"].to_pylist()[0] == expected
        assert table["untouched"].to_pylist()[0] == 42
    tables = [
        read_episode_table(output, V3DatasetMetadata("local/merged", output), episode)
        if v3
        else pq.read_table(output / f"data/chunk-000/episode_{episode:06d}.parquet")
        for episode in range(4)
    ]
    np.testing.assert_allclose(
        load_json(output / "meta/stats.json")["action"]["mean"],
        np.mean([row for table in tables for row in table["action"].to_pylist()], axis=0),
    )
    assert result.summary["dimension_indices"] == mappings
    assert [_snapshot(root) for root in roots] == before


@pytest.mark.parametrize(
    "policy,mappings,error",
    [
        ("min", [{"action": [0, 0]}, {"action": [0, 1]}], "duplicates"),
        ("min", [{"action": [True]}, {"action": [0]}], "integers"),
        ("min", [{"action": [None]}, {"action": [0]}], "integers"),
        ("min", [{"action": [2]}, {"action": [0]}], "exceeds"),
        ("min", [{"action": []}, {"action": []}], "at least one"),
        ("min", [{"action": [0]}, {"action": [0, 1]}], "same output"),
        ("pad", [{"action": [0, None]}, {"action": [0, 1]}], "preserve every"),
        ("pad", [{"action": [0, None, 1]}, {"action": [0, None, 1]}], "real signal"),
    ],
)
def test_index_mapping_rejects_invalid_positions(policy, mappings, error):
    from lerobot.data_platform.precompute.preprocess.merge_alignment import plan_signal_alignment

    infos = [{"features": {"action": {"dtype": "float32", "shape": [2], "names": ["actions"]}}}] * 2
    with pytest.raises(ValueError, match=error):
        plan_signal_alignment(infos, policy, dimension_indices=mappings)

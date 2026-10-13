"""End-effector checks preserve source data and produce reviewable evidence."""

import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lerobot.data_platform.curation_execution import validate_parameters
from lerobot.data_platform.precompute.data_profile import profile_from_info, quality_check_capabilities
from lerobot.data_platform.precompute.preprocess.quality_flags import run_quality_flag_detection
from lerobot.data_platform.precompute.quality_eef import motion_steps
from tests.datasets.test_lifecycle import _snapshot


def make_eef(root, version="v2.1", order="xyzw"):
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    names = ["x", "y", "z"] + (["qx", "qy", "qz", "qw"] if order == "xyzw" else ["qw", "qx", "qy", "qz"])
    n = 600
    values = np.zeros((n, 7))
    values[:, names.index("qw")] = 1
    values[::2, names.index("qw")] = -1
    values[:, 0] = np.arange(n) * 0.0001
    values[100, 0] += 5
    values[300:, 0] += 100  # Not a jump: new episode.
    features = {"left_arm_quaternion_pose": {"dtype": "float32", "shape": [7], "names": names}}
    for key in ("timestamp", "episode_index", "frame_index", "index", "task_index"):
        features[key] = {"dtype": "float32" if key == "timestamp" else "int64", "shape": [1]}
    table = pa.table(
        {
            "left_arm_quaternion_pose": values.tolist(),
            "timestamp": np.tile(np.arange(300) / 30, 2),
            "episode_index": np.repeat([0, 1], 300),
            "frame_index": np.tile(np.arange(300), 2),
            "index": np.arange(n),
            "task_index": np.zeros(n, dtype=int),
        }
    )
    info = {
        "robot_type": "UMI" if order == "wxyz" else "genrobot_das",
        "codebase_version": version,
        "fps": 30,
        "total_episodes": 2,
        "total_frames": n,
        "total_tasks": 1,
        "chunks_size": 1000,
        "features": features,
        "splits": {"train": "0:2"},
    }
    rows = [{"episode_index": ep, "length": 300, "tasks": ["Pick object"]} for ep in range(2)]
    if version == "v2.1":
        info["data_path"] = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
        for ep in range(2):
            pq.write_table(table.slice(ep * 300, 300), root / f"data/chunk-000/episode_{ep:06d}.parquet")
        (root / "meta/episodes.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        (root / "meta/episodes_stats.jsonl").write_text(
            "\n".join(json.dumps({"episode_index": ep, "stats": {}}) for ep in range(2)) + "\n"
        )
        (root / "meta/tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": "Pick object"}) + "\n")
    else:
        info["data_path"] = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
        pq.write_table(table, root / "data/chunk-000/file-000.parquet", row_group_size=75)
        (root / "meta/episodes/chunk-000").mkdir(parents=True)
        for ep, row in enumerate(rows):
            row.update(
                {
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                    "dataset_from_index": ep * 300,
                    "dataset_to_index": (ep + 1) * 300,
                }
            )
        pq.write_table(pa.Table.from_pylist(rows), root / "meta/episodes/chunk-000/file-000.parquet")
        pq.write_table(pa.table({"task_index": [0], "task": ["Pick object"]}), root / "meta/tasks.parquet")
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/stats.json").write_text("{}")
    return root


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
@pytest.mark.parametrize("order", ["xyzw", "wxyz"])
def test_eef_spike_sign_equivalence_and_source_preservation(tmp_path, version, order):
    root = make_eef(tmp_path / "data", version, order)
    before = _snapshot(root)
    static = tmp_path / "static"
    result = run_quality_flag_detection(root, static, checks=["eef"], workers=2)
    issues = json.loads((static / "annotation_issues.json").read_text())
    assert [(x["episode"], x["reason"], x["frame_intervals"]) for x in issues] == [
        (0, "eef_position_jump", [[100, 102]])
    ]
    assert issues[0]["severity"] == "candidate"
    assert issues[0]["unit"] == "source_position_unit"
    assert issues[0]["time_intervals"][0][0] == pytest.approx(100 / 30)
    assert result.total_frames == 600
    assert result.summary["qc_config"]["eef"]["local_ratio"] == 8
    # Rerun only episode 1: episode 0 evidence and human flags survive.
    (static / "manual_flagged_episodes.json").write_text('{"flagged_episodes":[1]}')
    run_quality_flag_detection(root, static, episodes=[1], checks=["eef"], workers=1)
    assert json.loads((static / "annotation_issues.json").read_text()) == issues
    run_quality_flag_detection(root, static, checks=["s3"], workers=1)
    assert issues[0] in json.loads((static / "annotation_issues.json").read_text())
    assert before == _snapshot(root)


def test_rotation_and_bad_times_are_not_infinite_motion():
    values = np.zeros((4, 7))
    values[:, -1] = [1, -1, 0, 0]
    values[2, 5] = 1
    steps, angles, norms = motion_steps(values, np.array([0, 0.1, 0.2, 0.2]))
    assert angles[0] == 0
    assert angles[1] == pytest.approx(180)
    assert np.isnan(angles[2]) and np.isnan(steps[2]) and norms[3] == 0
    steps, angles, _ = motion_steps(values, np.array([0, 0.1, 1.3, 1.3]), max_dt=0.2)
    assert np.isnan(steps[1]) and np.isnan(angles[1])


def test_v3_split_episode_and_invalid_pose_timing(tmp_path):
    root = make_eef(tmp_path / "data", "v3.0")
    path = root / "data/chunk-000/file-000.parquet"
    data = pq.read_table(path).to_pydict()
    data["left_arm_quaternion_pose"][50] = [0] * 7
    data["left_arm_quaternion_pose"][60] = [float("nan")] * 7
    data["timestamp"][70] = data["timestamp"][69]
    table = pa.table(data)
    pq.write_table(table.slice(0, 150), path)
    pq.write_table(table.slice(150), path.with_name("file-001.parquet"))
    # Respect the declared v3 shard layout.
    (root / "data/chunk-000").rename(root / "data/shard-000")
    info = json.loads((root / "meta/info.json").read_text())
    info["data_path"] = info["data_path"].replace("chunk-", "shard-")
    (root / "meta/info.json").write_text(json.dumps(info))
    result = run_quality_flag_detection(root, tmp_path / "static", checks=["eef"])
    issues = json.loads((tmp_path / "static/annotation_issues.json").read_text())
    assert {i["reason"] for i in issues} == {
        "invalid_quaternion",
        "invalid_pose",
        "invalid_timestamps",
        "eef_position_jump",
    }
    assert result.total_frames == 600
    json.dumps(issues, allow_nan=False)


def test_eef_requires_named_pose_not_vector_length(tmp_path):
    root = make_eef(tmp_path / "data")
    info = json.loads((root / "meta/info.json").read_text())
    assert quality_check_capabilities(profile_from_info(info), info["features"])["eef"]["available"]
    info["features"]["left_arm_quaternion_pose"].pop("names")
    assert not quality_check_capabilities(profile_from_info(info), info["features"])["eef"]["available"]


@pytest.mark.parametrize(
    "config",
    [
        {"quantile": 1},
        {"local_ratio": float("nan")},
        {"window": 4},
        {"window": True},
        {"quaternion_tolerance": -1},
        {"max_speed": 1},
    ],
)
def test_eef_config_validated_at_submission(config):
    with pytest.raises(ValueError):
        validate_parameters("curation.quality", {"checks": ["eef"], "qc_config": {"eef": config}})

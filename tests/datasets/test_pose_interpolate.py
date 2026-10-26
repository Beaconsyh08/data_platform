"""Pose repairs preserve source data, unrelated shard rows and complete statistics."""

import json

import numpy as np
import pyarrow.parquet as pq
import pytest

from lerobot.data_platform.precompute.preprocess.pose_interpolate import run_pose_interpolate
from lerobot.data_platform.precompute.preprocess.quality_flags import run_quality_flag_detection
from tests.datasets.test_lifecycle import _snapshot
from tests.datasets.test_quality_eef import make_eef


def repairs():
    return [
        {
            "episode_index": 0,
            "field": "left_arm_quaternion_pose",
            "start_frame": 100,
            "end_frame": 101,
            "position_unit": "m",
        }
    ]


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
@pytest.mark.parametrize("order", ["xyzw", "wxyz"])
def test_pose_interpolation_independent_output_and_shared_shard(tmp_path, version, order):
    source = make_eef(tmp_path / "source", version, order)
    before = _snapshot(source)
    target = tmp_path / "repaired"
    result = run_pose_interpolate(source, repairs=repairs(), out_root=target)
    assert result.total_frames == 600
    assert _snapshot(source) == before
    filename = "file-000.parquet" if version == "v3.0" else "episode_000000.parquet"
    original = pq.read_table(source / "data/chunk-000" / filename).to_pydict()
    changed = pq.read_table(target / "data/chunk-000" / filename).to_pydict()
    for field in original:
        if field != "left_arm_quaternion_pose":
            assert original[field] == changed[field]
    poses = np.asarray(changed["left_arm_quaternion_pose"])
    assert poses[100, 0] == pytest.approx(0.01)
    assert np.allclose(np.linalg.norm(poses[:, 3:], axis=1), 1)
    assert original["left_arm_quaternion_pose"][:100] == changed["left_arm_quaternion_pose"][:100]
    assert original["left_arm_quaternion_pose"][101:] == changed["left_arm_quaternion_pose"][101:]
    if version == "v2.1":
        assert (source / "data/chunk-000/episode_000001.parquet").read_bytes() == (
            target / "data/chunk-000/episode_000001.parquet"
        ).read_bytes()
        stats = [json.loads(row) for row in (target / "meta/episodes_stats.jsonl").read_text().splitlines()]
        assert [row["episode_index"] for row in stats] == [0, 1]
    assert len(result.episode_lineage) == 2
    with pytest.raises(FileExistsError):
        run_pose_interpolate(source, repairs=repairs(), out_root=target)
    assert _snapshot(source) == before


def test_dry_run_and_invalid_anchor_leave_no_output(tmp_path):
    source = make_eef(tmp_path / "source")
    before = _snapshot(source)
    target = tmp_path / "output"
    run_pose_interpolate(source, repairs=repairs(), out_root=target, dry_run=True)
    assert not target.exists()
    with pytest.raises(ValueError, match="anchors"):
        run_pose_interpolate(source, repairs=[{**repairs()[0], "end_frame": 300}], out_root=target)
    assert not target.exists()
    assert not list(tmp_path.glob(".output.building-*"))
    assert _snapshot(source) == before


def test_laundry_strategy_has_explicit_units_and_provenance(tmp_path):
    source = make_eef(tmp_path / "source")
    before = _snapshot(source)
    static = tmp_path / "static"
    run_quality_flag_detection(
        source,
        static,
        checks=["eef"],
        qc_config={
            "eef": {"strategy": "laundry_spike", "position_unit": "m"},
        },
    )
    issues = json.loads((static / "annotation_issues.json").read_text())
    jumps = [row for row in issues if row["reason"] == "eef_position_jump"]
    assert jumps and jumps[0]["unit"] == "m/s"
    assert jumps[0]["metrics"]["source_commit"].startswith("6ba72ff6")
    assert _snapshot(source) == before
    with pytest.raises(ValueError, match="explicit position units"):
        run_quality_flag_detection(
            source,
            tmp_path / "invalid",
            checks=["eef"],
            qc_config={
                "eef": {"strategy": "laundry_spike"},
            },
        )


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_manifest_pose_recipe_follows_stable_uid_after_episode_reindex(tmp_path, version):
    from dataclasses import asdict

    from lerobot.data_platform.lifecycle import EpisodeRef, LifecycleStore, materialize_manifest
    from tests.datasets.test_lifecycle import _publish

    source = make_eef(tmp_path / "source", version)
    before = _snapshot(source)
    store = LifecycleStore(tmp_path / "ledger")
    base = store.ingest(source, "local/poses")
    uid = base.uid_by_index()[1]
    manifest = store.create_manifest(
        base.version_id,
        exclude=[asdict(EpisodeRef(base.version_id, base.uid_by_index()[0]))],
        repair_recipes=[
            {
                "op": "pose_interpolate",
                "episode_refs": [asdict(EpisodeRef(base.version_id, uid))],
                "params": {"repairs": [{**repairs()[0], "episode_index": 1}]},
            }
        ],
    )
    _publish(store, manifest.manifest_id)
    output = tmp_path / "curated"
    derived, run = materialize_manifest(store, manifest.manifest_id, output, workers=1)
    assert derived.uid_by_index() == {0: uid}
    assert run["status"] == "committed"
    assert _snapshot(source) == before


def test_symlinked_data_cannot_modify_original(tmp_path):
    source = make_eef(tmp_path / "source")
    external = tmp_path / "external"
    (source / "data").rename(external)
    (source / "data").symlink_to(external, target_is_directory=True)
    original = {path.name: path.read_bytes() for path in external.rglob("*.parquet")}
    with pytest.raises(ValueError, match="symlinks"):
        run_pose_interpolate(source, repairs=repairs(), out_root=tmp_path / "output")
    assert {path.name: path.read_bytes() for path in external.rglob("*.parquet")} == original
    assert not (tmp_path / "output").exists()

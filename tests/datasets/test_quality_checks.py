"""Quality checks preserve sources and evidence from unrelated detectors/episodes."""

import json
from types import SimpleNamespace

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lerobot.data_platform.curation_execution import validate_parameters
from lerobot.data_platform.precompute.data_profile import profile_from_info, quality_check_capabilities
from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3
from lerobot.data_platform.precompute.preprocess.quality_flags import run_quality_flag_detection
from lerobot.data_platform.precompute.quality_signals import SignalQualityConfig, detect_signal_quality
from lerobot.data_platform.precompute.quality_video import VideoQualityConfig, scan_camera_quality
from tests.datasets.test_lifecycle import _snapshot
from tests.datasets.test_preprocess_ops import _add_v21_stat_counts, _make_dataset


@pytest.mark.parametrize(
    "parameters",
    [
        {"checks": []},
        {"checks": ["s1", "s1"]},
        {"checks": ["unknown"]},
        {"video_workers": True},
        {"video_workers": 17},
        {"video_mode": "preview"},
        {"qc_config": {"schema_version": True}},
        {"qc_config": {"s1": {"max_step": float("nan")}}},
        {"qc_config": {"s3": {"bounds": [{"field": "action", "dimension": 0, "min": 0}]}}},
        {"qc_config": {"c3": {"black_fraction": 2}}},
    ],
)
def test_invalid_quality_config_rejected(parameters):
    with pytest.raises(ValueError):
        validate_parameters("curation.quality", parameters)


def test_s1_uses_time_and_excludes_grippers():
    timestamps = np.array([0, 0.1, 0.2, 0.2, 0.4])
    action = np.zeros((5, 16))
    action[1:, 0] = 1
    action[:, 7] = [0, 100, 0, 100, 0]
    action[3:, 1] = 10  # A discontinuity at a duplicate timestamp must not imply infinite speed.
    issues = detect_signal_quality(
        0,
        timestamps,
        {"action": action.tolist()},
        {"action": {"dtype": "float32", "shape": [16]}},
        ["s1"],
        SignalQualityConfig(),
    )
    jumps = [issue for issue in issues if issue.reason == "joint_jump"]
    assert [(issue.dimension, issue.frame_intervals) for issue in jumps] == [(0, ((1, 2),))]
    assert any(issue.reason == "invalid_timestamps" for issue in issues)
    assert jumps[0].metrics["observed_speed"] == pytest.approx(10)


def test_s3_distinguishes_raw_nonfinite_missing_and_ragged_values():
    features = {"sensor": {"dtype": "float32", "shape": [2], "unit": "m"}}
    config = SignalQualityConfig.parse(
        {}, {"bounds": [{"field": "sensor", "dimension": 0, "unit": "m", "min": 0, "max": 1}]}
    )
    config.validate_features(features)
    issues = detect_signal_quality(
        0,
        np.arange(5) / 10,
        {"sensor": [[0, 0], [2, float("inf")], [float("nan"), 0], [0], [None, 0]]},
        features,
        ["s3"],
        config,
    )
    reasons = {issue.reason: issue for issue in issues}
    assert reasons["signal_shape_mismatch"].frame_intervals == ((3, 4),)
    assert reasons["missing_value"].frame_intervals == ((4, 5),)
    assert reasons["out_of_bounds"].frame_intervals == ((1, 2),)
    assert {
        interval for issue in issues if issue.reason == "non_finite" for interval in issue.frame_intervals
    } == {(1, 2), (2, 3)}
    json.dumps([issue.to_dict() for issue in issues], allow_nan=False)
    with pytest.raises(ValueError, match="unit"):
        config.validate_features({"sensor": {**features["sensor"], "unit": "mm"}})


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_selected_detector_rerun_retains_other_evidence_and_manual_flags(tmp_path, version):
    source = tmp_path / "source"
    _make_dataset(source)
    if version == "v3.0":
        _add_v21_stat_counts(source)
        source = run_convert_v3(source, tmp_path / "v3").out_root
    before = _snapshot(source)
    static = tmp_path / "static"
    static.mkdir()
    existing = [
        {"episode": 0, "type": "quality_flag", "detector": "s3", "reason": "old_s3"},
        {"episode": 0, "type": "quality_flag", "detector": "c3", "camera": "front", "reason": "black_frame"},
        {"episode": 0, "type": "quality_flag", "reason": "wrong_prompt", "manual": True},
        {"episode": 1, "type": "quality_flag", "detector": "s1", "reason": "other_episode"},
    ]
    (static / "annotation_issues.json").write_text(json.dumps(existing))
    (static / "manual_flagged_episodes.json").write_text(json.dumps({"flagged_episodes": [0]}))
    run_quality_flag_detection(
        source, static, episodes=[0], data_version="DVT2", checks=["s1"], overwrite=True, workers=1
    )
    first = json.loads((static / "annotation_issues.json").read_text())
    assert all(issue in first for issue in existing)
    assert any(issue.get("detector") == "s1" and issue["episode"] == 0 for issue in first)
    run_quality_flag_detection(
        source,
        static,
        episodes=[0],
        data_version="DVT2",
        checks=["s1"],
        qc_config={"s1": {"max_step": 100, "max_speed": 100}},
        workers=2,
    )
    assert json.loads((static / "annotation_issues.json").read_text()) == sorted(
        existing,
        key=lambda issue: (
            issue["episode"],
            issue.get("detector", "legacy"),
            issue.get("camera", ""),
            issue["reason"],
            "",
        ),
    )
    reasons = json.loads((static / "quality_flagged_episodes.json").read_text())["flag_reasons"]["0"]
    assert any(reason.get("camera") == "front" and reason.get("detector") == "c3" for reason in reasons)
    # A legacy caller must also retain newer detector evidence once it exists.
    run_quality_flag_detection(source, static, episodes=[0], data_version="DVT2", workers=1)
    assert all(issue in json.loads((static / "annotation_issues.json").read_text()) for issue in existing)
    assert before == _snapshot(source)


def test_umi_s3_allowed_but_legacy_and_s1_stay_unavailable(tmp_path):
    source = tmp_path / "source"
    _make_dataset(source)
    info = json.loads((source / "meta/info.json").read_text())
    info["robot_type"] = "umi"
    (source / "meta/info.json").write_text(json.dumps(info))
    caps = quality_check_capabilities(profile_from_info(info), info["features"])
    assert caps["s3"]["available"]
    assert not caps["s1"]["available"] and not caps["legacy"]["available"]
    run_quality_flag_detection(source, tmp_path / "static", checks=["s3"], workers=1)
    with pytest.raises(ValueError, match="legacy"):
        run_quality_flag_detection(source, tmp_path / "legacy", workers=1)


def _video(path, values):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        for value in values:
            frame = av.VideoFrame.from_ndarray(np.full((32, 32, 3), value, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def test_c3_shared_video_stays_inside_episode_and_reports_coverage(tmp_path):
    path = tmp_path / "shared.mp4"
    _video(path, [0, 0, 200, 200, 200])
    meta = SimpleNamespace(
        info={"fps": 10},
        features={"front": {"dtype": "video"}},
        get_video_file_path=lambda *args: path.name,
        get_video_time_range=lambda *args: (0.2, 0.5),
    )
    times = np.arange(3) / 10
    full = scan_camera_quality(tmp_path, meta, 1, "front", times, "full", VideoQualityConfig())
    assert full.status == "complete"
    assert full.coverage()["coverage_fraction"] == 1
    assert not any(issue.reason == "black_frame" for issue in full.issues)
    sampled = scan_camera_quality(tmp_path, meta, 1, "front", times, "sampled", VideoQualityConfig())
    assert sampled.status == "sampled" and sampled.checked_frames == 2
    assert sampled.coverage()["coverage_fraction"] == pytest.approx(2 / 3)
    path.write_bytes(b"broken video")
    failed = scan_camera_quality(tmp_path, meta, 1, "front", times, "full", VideoQualityConfig())
    assert failed.status == "error" and failed.checked_frames == 0
    assert failed.issues[0].reason == "media_decode_error"


def test_c3_missing_images_are_errors_and_retain_previous_evidence(tmp_path):
    source = tmp_path / "source"
    _make_dataset(source)
    info = json.loads((source / "meta/info.json").read_text())
    info["features"]["front"] = {"dtype": "image", "shape": [3, 32, 32]}
    (source / "meta/info.json").write_text(json.dumps(info))
    path = source / "data/chunk-000/episode_000000.parquet"
    table = pq.read_table(path).append_column(
        "front", pa.array([{"path": "missing.png", "bytes": None}, {"path": "missing.png", "bytes": None}])
    )
    pq.write_table(table, path)
    before = _snapshot(source)
    static = tmp_path / "static"
    static.mkdir()
    previous = {
        "episode": 0,
        "type": "quality_flag",
        "detector": "c3",
        "camera": "front",
        "reason": "black_frame",
    }
    (static / "annotation_issues.json").write_text(json.dumps([previous]))
    result = run_quality_flag_detection(source, static, episodes=[0], checks=["c3"], video_mode="full")
    assert result.summary["video_error_streams"] == 1
    assert previous in json.loads((static / "annotation_issues.json").read_text())
    assert json.loads((static / "quality_coverage.json").read_text())["streams"][0]["status"] == "incomplete"
    assert before == _snapshot(source)

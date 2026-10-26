"""Profile recognition follows robot identity without changing stored metadata."""

import json

import pytest

from lerobot.data_platform.precompute.data_profile import (
    default_processing_profile,
    profile_from_info,
    quality_check_capabilities,
    resolve_processing_profile,
)


def h10w_info():
    features = {key: {"dtype": "float32", "shape": [16]} for key in ("action", "state")}
    unknown = profile_from_info({"robot_type": "unrecognized_robot", "features": features})
    return {"robot_type": "h10_w", "features": features, "data_profile": unknown.to_dict()}


@pytest.mark.parametrize("robot", ["h10w", "h10_w", " H10_W "])
@pytest.mark.parametrize("recorded", [False, True])
def test_h10w_alias_and_previously_unrecognized_metadata(robot, recorded):
    info = h10w_info()
    info["robot_type"] = robot
    if not recorded:
        info.pop("data_profile")
    before = json.dumps(info)
    source = profile_from_info(info)
    profile = default_processing_profile(source, info["features"])
    assert profile.legacy_data_version == "DVT2"
    checks = quality_check_capabilities(profile, info["features"])
    assert checks["legacy"]["available"] and checks["s1"]["available"]
    assert json.dumps(info) == before


@pytest.mark.parametrize("sidecar", [False, True])
def test_h10w_stored_unknown_profile_recovers_without_writes(tmp_path, sidecar):
    info = h10w_info()
    meta = tmp_path / "meta"
    meta.mkdir()
    if sidecar:
        (meta / "data_profile.json").write_text(json.dumps(info.pop("data_profile")))
    (meta / "info.json").write_text(json.dumps(info))
    before = {p.name: p.read_bytes() for p in meta.iterdir()}
    assert resolve_processing_profile(tmp_path).legacy_data_version == "DVT2"
    assert resolve_processing_profile(tmp_path, data_version_override="DVT1").legacy_data_version == "DVT1"
    assert before == {p.name: p.read_bytes() for p in meta.iterdir()}


@pytest.mark.parametrize("protected", ["explicit", "confirmed", "other_robot", "no_joint_signals"])
def test_only_failed_h10w_inference_is_retried(protected):
    info = h10w_info()
    if protected == "explicit":
        info["data_profile"]["resolution_source"] = "explicit"
    elif protected == "confirmed":
        info["data_profile"]["confirmed"] = True
    elif protected == "other_robot":
        info["robot_type"] = "another_robot"
    else:
        info["features"] = {}
    assert profile_from_info(info).to_dict() == info["data_profile"]


def test_eef_quality_explains_missing_joint_signals():
    features = {
        "left_arm_quaternion_pose": {"dtype": "float32", "shape": [7]},
        "left_gripper_width": {"dtype": "float32", "shape": [1], "unit": "m"},
        "observation.images.left_wrist": {"dtype": "video", "shape": [256, 316, 3]},
    }
    profile = profile_from_info(
        {
            "robot_type": "genrobot_das",
            "features": features,
            "data_profile": {
                "robot_profile": "genrobot_das",
                "signal_schema": "genrobot_das_bimanual_eef_v1",
                "gripper_encoding": "width_m",
                "confirmed": True,
                "resolution_source": "genrobot_mcap_importer",
            },
        }
    )
    assert default_processing_profile(profile, features) == profile
    checks = quality_check_capabilities(profile, features)
    for name in ("legacy", "s1"):
        assert not checks[name]["available"]
        assert "no joint-angle" in checks[name]["reason"]
    assert checks["s3"]["available"] and checks["c3"]["available"]

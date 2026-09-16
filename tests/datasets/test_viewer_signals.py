"""Viewer semantics and cache compatibility must not alter source signal values."""

import csv
import io

import pytest
from flask import Flask

from lerobot.data_platform.precompute.viewer_signals import viewer_csv_labels, viewer_signal_presentation
from lerobot.data_platform.viewer import _columns_from_csv_header, _serve_csv_stripped


@pytest.mark.parametrize("robot_type", ["UMI-GripperBody-Head", ""])
def test_legacy_packed_csv_is_disambiguated_without_rewriting_cache(tmp_path, robot_type):
    names = [
        f"{part}_{axis}"
        for part in ("left", "right", "head")
        for axis in ("x", "y", "z", "qw", "qx", "qy", "qz")
    ]
    names += ["left_gripper", "right_gripper"]
    features = {key: {"dtype": "float32", "shape": [23], "names": names} for key in ("state", "action")}
    header = ["timestamp", *names, *names, "stage"]
    values = [str(i / 10) for i in range(len(header))]
    path = tmp_path / "episode.csv"
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows([header, values])
    path.write_text(output.getvalue())
    before = path.read_bytes()
    columns = _columns_from_csv_header(path, features)
    description = viewer_signal_presentation(features, columns, robot_type)
    assert description["mode"] == "end_effector"
    assert len(description["series"]) == 46
    with Flask(__name__).test_request_context():
        response = _serve_csv_stripped(path, None, features)
        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
    assert rows[0] == ["timestamp", *[label for c in columns for label in c["value"]]]
    assert len(rows[0]) == len(set(rows[0]))
    assert rows[1] == values
    assert path.read_bytes() == before
    # Component mapping follows declared names even when the vector order is different.
    gripper = next(s for s in description["series"] if s["label"] == "state.left_gripper")
    assert (gripper["index"], gripper["part"], gripper["component"]) == (21, "left", "gripper")


def test_ambiguous_csv_without_matching_metadata_requires_cache_rebuild():
    with pytest.raises(ValueError, match="rebuild"):
        viewer_csv_labels(["timestamp", "left_x", "left_x"])
    features = {"state": {"dtype": "float32", "shape": [2], "names": ["left_x", "right_x"]}}
    with pytest.raises(ValueError, match="rebuild"):
        viewer_csv_labels(["timestamp", "left_x", "left_x"], features)


def test_vector_length_alone_does_not_select_end_effector_display():
    features = {"state": {"dtype": "float32", "shape": [23], "names": [f"index_{i}" for i in range(23)]}}
    columns = [{"key": "state", "value": [f"state_{i}" for i in range(23)]}]
    presentation = viewer_signal_presentation(features, columns)
    assert presentation["mode"] == "joint"
    assert all(not signal["part"] for signal in presentation["series"])


@pytest.mark.parametrize("robot", ["UMI-GripperBody-Head", "UMI-Gripper-Head", "umi"])
def test_packed_pose_uses_named_semantics_not_vector_dimensions(robot):
    features = {"state": {"dtype": "float32", "shape": [3], "names": ["right_qz", "left_x", "head_qw"]}}
    columns = [{"key": "state", "value": ["state_0", "state_1", "state_2"]}]
    presentation = viewer_signal_presentation(features, columns, robot)
    assert presentation["mode"] == "end_effector"
    assert [(s["part"], s["component"]) for s in presentation["series"]] == [
        ("right", "qz"),
        ("left", "x"),
        ("head", "qw"),
    ]

"""Local signal evidence remains deterministic and does not invent semantic judgments."""

import json

import pytest

from lerobot.data_platform.temporal_caption_signals import analyze_signals, boundary_matrix, signal_audit


def metadata(values, key="action"):
    return {
        "fps": 30,
        "frame_count": len(values),
        "signals": [{"frame": i, key: value} for i, value in enumerate(values)],
    }


def partition(*frames):
    return {"segments": [{"start_frame": f} for f in (0, *frames)]}


def test_full_rate_step_and_unit_scaling():
    meta = metadata([[0, 0]] * 30 + [[1, 0]] * 30)
    analysis = analyze_signals(meta)
    assert any(e.frame == 30 and e.kind == "change" and e.dimension == 0 for e in analysis.events)
    assert all(e.dimension == 0 for e in analysis.events)
    scaled = analyze_signals(metadata([[0, 0]] * 30 + [[1000, 0]] * 30))
    assert [(e.frame, e.kind) for e in analysis.events] == [(e.frame, e.kind) for e in scaled.events]
    assert json.dumps(analysis.to_dict(), allow_nan=False)
    assert analysis == analyze_signals(meta)


def test_motion_start_stop_constant_and_missing_samples():
    analysis = analyze_signals(metadata([0.0] * 20 + list(range(1, 21)) + [20.0] * 20, "state"))
    assert any(e.frame == 20 and e.kind == "motion_start" for e in analysis.events)
    assert any(e.frame == 40 and e.kind == "motion_stop" for e in analysis.events)
    assert not analyze_signals(metadata([3.0] * 60)).events
    # Missing data is never interpreted as a numerical jump.
    missing = analyze_signals(metadata([0.0] * 20 + [None, float("nan")] + [1.0] * 20))
    assert not missing.events
    assert json.dumps(missing.to_dict(), allow_nan=False)


def test_disabled_invalid_alignment_and_shapes():
    meta = metadata([0] * 30 + [1] * 30)
    meta["include_signals"] = False
    analysis = analyze_signals(meta)
    assert not analysis.enabled and not analysis.events
    assert boundary_matrix(partition(30), partition(30), analysis, 30)["signal_status"] == "disabled"
    meta.pop("include_signals")
    meta["signals"][1]["frame"] = 3
    with pytest.raises(ValueError, match="align"):
        analyze_signals(meta)
    bad = analyze_signals(metadata([[0], [1, 2], [3]]))
    assert not bad.events and bad.channels[0]["status"] == "invalid_shape"


def test_matrix_provenance_bounded_clusters_and_audit():
    analysis = analyze_signals(metadata([0] * 30 + [1] * 30))
    matrix = boundary_matrix(partition(28), partition(31, 50), analysis, 30)
    row = next(r for r in matrix["rows"] if "signal" in r["sources"])
    assert row["status"] == "temporal_support"
    assert set(row["sources"]) == {"visual", "joint", "signal"}
    assert any(e.get("feature") == "action" for e in row["evidence"])
    assert any(r["status"] == "hypothesis_disagreement" for r in matrix["rows"])
    assert not signal_audit(partition(30), analysis, 30)["unmatched_events"]
    assert signal_audit(partition(50), analysis, 30)["unmatched_events"]
    empty = analyze_signals(metadata([0] * 60))
    chained = boundary_matrix(partition(10, 24), partition(17), empty, 30)
    assert len(chained["rows"]) == 2
    assert all(r["range"][1] - r["range"][0] <= 8 for r in chained["rows"])


def test_gradual_gripper_crossing_and_signal_only_candidate():
    analysis = analyze_signals(metadata(list(range(60)), "left_gripper_pos"))
    assert any(e.frame == 30 and e.kind == "midrange_rise" for e in analysis.events)
    matrix = boundary_matrix(partition(), partition(), analysis, 30)
    assert all(row["status"] == "signal_only" for row in matrix["rows"])

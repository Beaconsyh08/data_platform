"""Blind review, evidence sampling, bounded repair and immutable input receipts."""

import copy
import json

import pytest
from PIL import Image

from lerobot.data_platform.temporal_caption_fusion import (
    focus_frames,
    infer_fusion,
    validate_partition,
    validate_review,
)
from lerobot.data_platform.temporal_caption_jobs import validate_run
from tests.datasets.test_temporal_caption import result


def bundle(tmp_path):
    root = tmp_path / "bundle"
    (root / "frames").mkdir(parents=True)
    for i in range(12):
        Image.new("RGB", (224, 248), "red").save(root / "frames" / f"{i:06d}.jpg")
    (root / "video.mp4").write_bytes(b"video")
    (root / "episode.json").write_text(
        json.dumps(
            {
                "source_digest": "test-source",
                "dataset_name": "source",
                "episode_index": 25,
                "frame_count": 12,
                "fps": 30,
                "task_hint": ["Move"],
                "cameras": ["head"],
                "signals": [
                    {"frame": i, "left_arm_pose": [i, 0, 0], "left_gripper_pos": float(i > 5)}
                    for i in range(12)
                ],
            }
        )
    )
    return root


def partition():
    value = result()
    for segment in value["segments"]:
        segment["boundary_interval"] = [segment["start_frame"], segment["start_frame"]]
    return value


class Model:
    def __init__(self, unresolved=False):
        self.calls = []
        self.unresolved = unresolved
        self.reviews = 0

    def post_chat_completion(self, payload):
        self.calls.append(copy.deepcopy(payload))
        prompt = payload["messages"][0]["content"][0]["text"]
        if "Audit the proposed annotation" in prompt:
            self.reviews += 1
            issues = (
                [
                    {
                        "start_frame": 0,
                        "end_frame": 6,
                        "severity": "minor",
                        "kind": "boundary",
                        "description": "Check transition",
                        "suggestion": "Retain ambiguity",
                    }
                ]
                if self.reviews == 1 or self.unresolved
                else []
            )
            value = {"issues": issues, "notes": "Visibility limited"}
        elif "Do not propose a caption partition" in prompt:
            value = {
                "scene": "scene",
                "objects": [],
                "events": [
                    {
                        "start_frame": 0,
                        "end_frame": 12,
                        "description": "Movement",
                        "evidence": "head frames",
                        "uncertainty": "",
                    }
                ],
                "limitations": [],
            }
        else:
            value = partition()
        return {"choices": [{"message": {"content": json.dumps(value)}}], "usage": {"total_tokens": 10}}


@pytest.mark.parametrize("unresolved", [False, True])
def test_pipeline_blindness_repairs_and_cached_resume(tmp_path, unresolved):
    source = bundle(tmp_path)
    out = tmp_path / "result"
    model = Model(unresolved)
    infer_fusion(source, out, "node/source", client=model)
    artifacts = validate_run(out, key="node/source", options={"episode_index": 25, "scheme": "fusion_review"})
    final = next(a for a in artifacts if a["variant"] == "fusion_reviewed")
    assert final["review"]["human_reviewed"] is False
    assert final["review"]["status"] == ("unresolved" if unresolved else "no_issues_detected")
    assert len(final["review"]["repairs"]) == (2 if unresolved else 1)
    blind = [
        c for c in model.calls if "Actively look for short missed" in c["messages"][0]["content"][0]["text"]
    ]
    assert len(blind) == 1
    observations = json.loads(blind[0]["messages"][0]["content"][0]["text"].split("OBSERVATIONS:")[1])
    assert not {"candidate", "visual_hypothesis", "event_hypothesis", "global"}.intersection(observations)
    before = len(model.calls)
    infer_fusion(source, out, "node/source", client=model)
    assert len(model.calls) == before
    with pytest.raises(ValueError, match="changed inputs"):
        infer_fusion(source, out, "other/source", client=model)


def test_uncertainty_and_review_ranges_rejected():
    value = partition()
    value["segments"][1]["boundary_interval"] = [7, 8]
    with pytest.raises(ValueError, match="uncertainty"):
        validate_partition(value, 12)
    with pytest.raises(ValueError, match="frame range"):
        validate_review({"issues": [{"start_frame": 0, "end_frame": 13}]}, 12)
    assert set(range(10, 71, 3)) <= set(focus_frames(100, 30, [40]))


def test_structural_retry_receives_exact_failed_boundary_and_response(tmp_path):
    class InvalidOnce(Model):
        failed = False

        def post_chat_completion(self, payload):
            response = super().post_chat_completion(payload)
            value = json.loads(response["choices"][0]["message"]["content"])
            if "segments" in value and not self.failed:
                self.failed = True
                value["segments"][1]["boundary_interval"] = [8, 10]
                response["choices"][0]["message"]["content"] = json.dumps(value)
            return response

    model = InvalidOnce()
    infer_fusion(bundle(tmp_path), tmp_path / "output", "node/source", client=model)
    corrective = [c["messages"][0]["content"][-1].get("text", "") for c in model.calls]
    assert any("segment start 6" in value and "Previous response:" in value for value in corrective)
    assert (tmp_path / "output/invalid-02-visual-0.json").is_file()


def test_visual_only_fusion_has_no_derived_signal_evidence(tmp_path):
    from lerobot.data_platform.temporal_caption_fusion import signal_evidence

    metadata = json.loads((bundle(tmp_path) / "episode.json").read_text())
    metadata["include_signals"] = False
    evidence = signal_evidence(metadata)
    assert evidence["signals"] == []
    assert evidence["events"] == []


def test_v2_local_evidence_reaches_fusion_and_review_without_extra_calls(tmp_path):
    model = Model()
    source, out = bundle(tmp_path), tmp_path / "output"
    infer_fusion(source, out, "node/source", client=model)
    final = json.loads((out / "fusion_reviewed.json").read_text())
    assert final["pipeline_version"] == "fusion-review-2"
    assert final["cross_modal"]["local_analysis"]["events"]
    assert final["cross_modal"]["boundary_matrix"]["rows"]
    assert len(final["cross_modal"]["segment_support"]) == len(final["result"]["segments"])
    for stage in final["review"]["stages"]:
        prompt = (out / f"stage-{stage['stage']}.prompt.txt").read_text()
        obs = json.loads(prompt.split("OBSERVATIONS:\n")[1])
        if stage["stage"].startswith("06-blind"):
            assert not {"signals", "local_signal_analysis", "boundary_matrix", "candidate"} & obs.keys()
        if stage["stage"] == "05-fusion":
            assert obs["boundary_matrix"] == final["cross_modal"]["boundary_matrix"]
        if stage["stage"] in {"07-review", "09-repair-0", "10-verify-0"}:
            assert "signal_audit" in obs
    # Seven base API stages plus one local recheck/repair/verify cycle; no signal API stage.
    assert len(model.calls) == 10
    meta = json.loads((source / "episode.json").read_text())
    meta["include_signals"] = False
    (source / "episode.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="changed inputs"):
        infer_fusion(source, out, "node/source", client=model)


def test_v2_published_evidence_is_validated_and_v1_remains_readable(tmp_path):
    source, out = bundle(tmp_path), tmp_path / "output"
    infer_fusion(source, out, "node/source", client=Model())
    path = out / "fusion_reviewed.json"
    artifact = json.loads(path.read_text())
    artifact["cross_modal"]["boundary_matrix"]["rows"][0]["status"] = "invented"
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="Cross-modal evidence"):
        validate_run(out)
    artifact["pipeline_version"] = "fusion-review-1"
    artifact.pop("cross_modal")
    path.write_text(json.dumps(artifact))
    assert len(validate_run(out)) == 2


def test_v2_visual_only_complete_pipeline(tmp_path):
    source, out = bundle(tmp_path), tmp_path / "output"
    path = source / "episode.json"
    meta = json.loads(path.read_text())
    meta["include_signals"] = False
    path.write_text(json.dumps(meta))
    infer_fusion(source, out, "node/source", client=Model())
    final = next(a for a in validate_run(out) if a["variant"] == "fusion_reviewed")
    cross = final["cross_modal"]
    assert not cross["local_analysis"]["events"]
    assert cross["boundary_matrix"]["signal_status"] == "disabled"
    assert not any(r["status"] == "temporal_support" for r in cross["boundary_matrix"]["rows"])


def test_long_episode_fusion_uses_bounded_overview_and_full_blind_audit(tmp_path):
    source = bundle(tmp_path)
    meta = json.loads((source / "episode.json").read_text())
    meta.update(frame_count=180, fps=3, signals=[{"frame": i} for i in range(180)])
    (source / "episode.json").write_text(json.dumps(meta))
    image = (source / "frames/000000.jpg").read_bytes()
    for i in range(12, 180):
        (source / f"frames/{i:06d}.jpg").write_bytes(image)

    class LongModel(Model):
        def post_chat_completion(self, payload):
            response = super().post_chat_completion(payload)
            value = json.loads(response["choices"][0]["message"]["content"])
            if "segments" in value:
                value["segments"][-1]["end_frame"] = 180
            response["choices"][0]["message"]["content"] = json.dumps(value)
            return response

    out = tmp_path / "output"
    infer_fusion(source, out, "node/source", client=LongModel())
    final = next(a for a in validate_run(out) if a["variant"] == "fusion_reviewed")
    stages = final["review"]["stages"]
    wide = next(s for s in stages if s["stage"] == "02-visual")["frames"]
    assert len(wide) == 160 and wide[0] == 0 and wide[-1] == 179
    blind = {f for s in stages if s["stage"].startswith("06-blind-audit") for f in s["frames"]}
    assert blind == set(range(180))

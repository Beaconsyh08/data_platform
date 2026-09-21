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

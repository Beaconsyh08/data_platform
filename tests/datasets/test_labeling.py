import json
from pathlib import Path

import lerobot.data_platform.precompute.labeling.qwen_remote as qwen_remote
import lerobot.data_platform.precompute.labeling.runner as labeling_runner
from lerobot.data_platform.precompute.labeling import normalize_object_name, parse_task, select_bbox
from lerobot.data_platform.precompute.labeling.bbox_select import select_bbox_with_context
from lerobot.data_platform.precompute.labeling.qwen_dashscope import (
    DASHSCOPE_MODELS,
    QwenDashScopeDetector,
    normalize_base_url as normalize_dashscope_base_url,
)
from lerobot.data_platform.precompute.labeling.runner import (
    _context_warnings_for_labeling,
    _tag_context_for_labeling,
    _target_prompt_for_backend,
    _value_is_zero,
    labeling_task_type,
    run_labeling,
    sample_episodes_by_task_type,
)
from lerobot.data_platform.precompute.labeling.qwen_remote import (
    DEFAULT_QWEN_ENDPOINT,
    PUBLIC_QWEN_ENDPOINT,
    QwenRemoteDetector,
    normalize_endpoint,
    parse_qwen_detections,
)
from lerobot.data_platform.precompute.labeling.review import (
    available_label_variants,
    labels_path,
    load_episode_record,
    load_labels_jsonl,
    migrate_latest_labels_to_variant,
    reason,
    remove_reviewed_record,
    resolved_labels_path,
    reviewed_path,
    save_reviewed_record,
    source_path,
    uncertainty,
)


def _det(left, top, right, bottom, confidence=0.5, label="obj"):
    return {
        "bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
        "confidence": confidence,
        "label": label,
    }


def test_labeling_tag_context_marks_prompt_action_mismatch():
    context = _tag_context_for_labeling(
        {
            "arm": "left",
            "object_count": 2,
            "background": "sofa",
            "prompt_action_match": "mismatch",
            "ignored": "value",
        }
    )

    assert context == {
        "arm": "left",
        "object_count": 2,
        "background": "sofa",
        "prompt_action_match": "mismatch",
    }
    assert _context_warnings_for_labeling(context) == ["prompt_action_mismatch"]


def test_parse_task_single_absolute_relative_and_typo():
    assert parse_task("Pick up the red cube") == {
        "target": "red cube",
        "direction": None,
        "reference": None,
    }
    assert parse_task("Pick up red cube") == {
        "target": "red cube",
        "direction": None,
        "reference": None,
    }
    assert parse_task("Pick up the dinasour on the left.") == {
        "target": "dinosaur",
        "direction": "left",
        "reference": None,
    }
    assert parse_task("Pick up dinasour on the left.") == {
        "target": "dinosaur",
        "direction": "left",
        "reference": None,
    }
    assert parse_task("Pick up the brown dog to the right of the yellow duck") == {
        "target": "brown dog",
        "direction": "right",
        "reference": "yellow duck",
    }
    assert parse_task("Pick up brown dog to the right of yellow duck") == {
        "target": "brown dog",
        "direction": "right",
        "reference": "yellow duck",
    }
    assert parse_task("Give me blue ball") == {
        "action": "give",
        "target": "blue ball",
        "direction": None,
        "reference": None,
    }
    assert parse_task("Place the cube on the table") is None


def test_qwen_target_prompt_detects_all_target_class_objects():
    parsed = parse_task("Pick up the brown dog to the right of the yellow duck")
    expected = "dog. grey dog. gray dog. beige bear. light brown bear. brown bear"
    assert _target_prompt_for_backend(parsed, "grounding_dino") == expected
    assert _target_prompt_for_backend(parsed, "qwen_remote") == expected
    assert normalize_object_name("beige bear") == "brown dog"
    assert normalize_object_name("light brown bear") == "brown dog"

    parsed = parse_task("Pick up the dinasour on the left")
    assert _target_prompt_for_backend(parsed, "qwen_remote") == "dinosaur"


def test_dashscope_model_list_includes_qwen_short_names():
    assert "qwen3.6-plus" in DASHSCOPE_MODELS
    assert "qwen3.7-plus" in DASHSCOPE_MODELS
    assert "qwen3.6-flash" in DASHSCOPE_MODELS


def test_trial_sampling_by_task_type(tmp_path: Path):
    class Meta:
        episodes = {}

    meta = Meta()
    tasks = []
    for idx in range(25):
        tasks.append("Pick up the red cube")
        tasks.append("Pick up the dinosaur on the left")
        tasks.append("Pick up the brown dog to the right of the yellow duck")
        tasks.append("Give me the blue ball")
    meta.episodes = {idx: {"tasks": [task]} for idx, task in enumerate(tasks)}

    assert labeling_task_type(parse_task("Pick up the red cube")) == "single"
    assert labeling_task_type(parse_task("Pick up the dinosaur on the left")) == "absolute"
    assert labeling_task_type(parse_task("Pick up the brown dog to the right of the yellow duck")) == "relative"
    assert labeling_task_type(parse_task("Give me the blue ball")) == "give"

    sample = sample_episodes_by_task_type(tmp_path, meta, per_type=20, seed=123)
    assert len(sample.episodes) == 80
    assert sample.counts == {"absolute": 20, "give": 20, "relative": 20, "single": 20}
    assert sample.available_counts == {"absolute": 25, "give": 25, "relative": 25, "single": 25}
    assert sample.seed == 123
    assert sample.episodes != list(range(80))
    assert sample.episodes == sample_episodes_by_task_type(tmp_path, meta, per_type=20, seed=123).episodes


def test_trial_sampling_skips_place_tasks(tmp_path: Path):
    class Meta:
        episodes = {
            0: {"tasks": ["Place the yellow duck on the table"]},
            1: {"tasks": ["Pick up the yellow duck"]},
        }

    sample = sample_episodes_by_task_type(tmp_path, Meta(), per_type=20, seed=123)

    assert sample.episodes == [1]
    assert sample.available_counts == {"single": 1}


def test_select_bbox_single_absolute_and_relative():
    left = _det(0, 0, 10, 10, 0.4)
    right = _det(90, 0, 100, 10, 0.3)
    assert select_bbox([right, left], None, None) == (right, True)
    assert select_bbox([right, left], None, "left") == (left, True)
    assert select_bbox([left, right], None, "right") == (right, True)

    target_near = _det(40, 0, 50, 10, 0.6)
    target_far = _det(0, 0, 10, 10, 0.7)
    reference = _det(60, 0, 70, 10, 0.8)
    selected, ok = select_bbox([target_far, target_near], [reference], "left")
    assert selected == target_near
    assert ok is True

    selected, ok = select_bbox([target_far], [reference], "right")
    assert selected is None
    assert ok is False


def test_select_bbox_with_context_uses_arm_and_last_frame_for_single_pick():
    left = _det(0, 0, 10, 10, 0.9)
    right = _det(90, 0, 100, 10, 0.8)
    left_last = _det(1, 0, 11, 10, 0.9)
    right_moved_last = _det(40, 0, 50, 10, 0.8)

    selected, ok, method = select_bbox_with_context(
        [left, right],
        None,
        None,
        arm="left",
        detections_target_last=[left_last, right_moved_last],
    )

    assert selected == right
    assert ok is True
    assert method == "last_frame_motion_then_left_hand_nearest"

    selected, ok, method = select_bbox_with_context([left, right], None, None, arm="right")
    assert selected == right
    assert ok is True
    assert method == "right_hand_nearest"


def test_qwen_spatial_result_still_uses_direction_postprocess():
    parsed = parse_task("Pick up the brown dog to the right of the yellow duck")
    assert "to the right of" not in _target_prompt_for_backend(parsed, "qwen_remote")
    qwen_target_left_of_ref = _det(10, 0, 20, 10, 0.9, "dog")
    reference = _det(60, 0, 70, 10, 0.8, "yellow duck")
    selected, ok = select_bbox([qwen_target_left_of_ref], [reference], parsed["direction"])
    assert selected is None
    assert ok is False


def test_exist_label_zero_is_skipped_by_review_scoring():
    assert _value_is_zero(0) is True
    assert _value_is_zero([0, 0]) is True
    assert _value_is_zero([0, 1]) is False
    record = {
        "parsed": {"target": "cube", "direction": None, "reference": None},
        "selected": None,
        "detections_target": [],
        "skip_reason": "exist_label_zero",
    }
    assert uncertainty(record) == -1
    assert reason(record) == "exist_label=0"


def test_uncertainty_and_reason():
    assert uncertainty({"parsed": None}) == -1
    assert reason({"parsed": None}) == "not Pick-up"

    missing = {
        "parsed": {"target": "cube", "direction": None, "reference": None},
        "selected": None,
        "detections_target": [],
    }
    assert uncertainty(missing) == 100
    assert reason(missing) == "no detection"

    close = {
        "parsed": {"target": "cube", "direction": None, "reference": None},
        "selected": _det(0, 0, 10, 10, 0.6),
        "relation_satisfied": True,
        "detections_target": [_det(0, 0, 10, 10, 0.6), _det(20, 0, 30, 10, 0.58)],
    }
    assert uncertainty(close) == 65
    assert reason(close) == "close top-2 (d=0.02)"

    vlm = {
        "parsed": {"target": "cube", "direction": None, "reference": None},
        "selected": _det(0, 0, 10, 10, 1.0),
        "relation_satisfied": True,
        "detections_target": [_det(0, 0, 10, 10, 1.0)],
    }
    vlm["detections_target"][0]["confidence_source"] = "vlm_implicit"
    assert uncertainty(vlm) == 30
    assert reason(vlm) == "vlm implicit conf"

    vlm_self = {
        "parsed": {"target": "cube", "direction": None, "reference": None},
        "selected": _det(0, 0, 10, 10, 0.42),
        "relation_satisfied": True,
        "detections_target": [_det(0, 0, 10, 10, 0.42)],
    }
    vlm_self["detections_target"][0]["confidence_source"] = "vlm_self_reported"
    assert uncertainty(vlm_self) == 85
    assert reason(vlm_self) == "low vlm self-conf 0.42"


def test_qwen_remote_detection_parsing_and_client_mock(monkeypatch):
    parsed = parse_qwen_detections(
        '```json\n[{"bbox_2d":[1,2,30,40],"label":"red cube"}]\n```',
        image_size=(64, 64),
    )
    assert parsed == [
        {
            "bbox": {"top": 2, "left": 1, "bottom": 40, "right": 30},
            "confidence": 1.0,
            "confidence_source": "vlm_implicit",
            "label": "red cube",
            "raw_bbox_2d": [1, 2, 30, 40],
            "bbox_coordinate_space": "pixel",
        }
    ]
    scaled = parse_qwen_detections(
        '[{"bbox_2d":[500,250,900,750],"label":"duck","confidence":0.73}]',
        image_size=(256, 256),
    )
    assert scaled[0]["bbox"] == {"top": 64, "left": 128, "bottom": 192, "right": 230}
    assert scaled[0]["confidence"] == 0.73
    assert scaled[0]["confidence_source"] == "vlm_self_reported"
    assert scaled[0]["bbox_coordinate_space"] == "qwen_0_1000"

    class DummyClient:
        def predict(self, **kwargs):
            assert kwargs["api_name"] == "/run_detection_streaming"
            assert '"confidence":0.0' in kwargs["user_prompt"]
            assert "Do not guess" in kwargs["user_prompt"]
            return ("preview", '[{"bbox_2d":[5,6,20,22],"label":"duck","confidence":"82%"}]')

    class DummyImage:
        size = (32, 32)

        def convert(self, mode):
            assert mode == "RGB"
            return self

        def save(self, fp, format=None, quality=None):
            fp.write(b"jpeg")

    monkeypatch.setattr(qwen_remote, "handle_file", lambda path: path)
    detector = QwenRemoteDetector(DummyClient(), "https://example.invalid/", "qwen-test", 1024, 9800)
    detected = detector.detect_for_prompt(DummyImage(), "duck")[0]
    assert detected["label"] == "duck"
    assert detected["confidence"] == 0.82


def test_qwen_remote_parses_gradio_detection_tuple():
    parsed = parse_qwen_detections(
        (
            {
                "path": "/tmp/vis.png",
                "url": None,
                "size": 123,
                "orig_name": "vis.png",
                "mime_type": "image/png",
                "is_stream": False,
                "meta": {},
            },
            '```json\n[{"bbox_2d":[100,200,300,400],"label":"duck"}]\n```',
            "done",
        ),
        image_size=(100, 50),
    )
    assert parsed[0]["bbox"] == {"top": 10, "left": 10, "bottom": 20, "right": 30}
    assert parsed[0]["label"] == "duck"


def test_qwen_remote_normalizes_public_endpoint_to_api_endpoint():
    assert normalize_endpoint(PUBLIC_QWEN_ENDPOINT) == DEFAULT_QWEN_ENDPOINT
    assert normalize_endpoint("https://example.com/api") == "https://example.com/api/"


def test_qwen_dashscope_detection_parsing_from_chat_completion():
    class DummyImage:
        size = (100, 50)

        def convert(self, mode):
            assert mode == "RGB"
            return self

        def save(self, fp, format=None, quality=None):
            fp.write(b"jpeg")

    class DummyDetector(QwenDashScopeDetector):
        def _post_chat_completion(self, payload):
            assert payload["model"] == "qwen3-vl-plus"
            content = payload["messages"][0]["content"]
            assert content[0]["type"] == "text"
            assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
            return {
                "choices": [
                    {
                        "message": {
                            "content": '```json\n[{"bbox_2d":[100,200,300,400],"label":"duck","confidence":0.9}]\n```'
                        }
                    }
                ]
            }

    detector = DummyDetector(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        api_key="test-key",
        model="qwen3-vl-plus",
    )
    assert normalize_dashscope_base_url(detector.base_url) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    detected = detector.detect_for_prompt(DummyImage(), "duck")[0]
    assert detected["bbox"] == {"top": 10, "left": 10, "bottom": 20, "right": 30}
    assert detected["confidence"] == 0.9


def test_label_jsonl_round_trip(tmp_path: Path):
    labeling_dir = tmp_path / "labeling"
    labeling_dir.mkdir()
    records = [
        {"episode_index": 1, "task": "Pick up the cube", "parsed": None, "selected": None},
        {"episode_index": 0, "task": "Pick up the duck", "parsed": None, "selected": None},
    ]
    labels_path(labeling_dir).write_text("".join(json.dumps(record) + "\n" for record in records))

    loaded = load_labels_jsonl(labeling_dir)
    assert sorted(loaded) == [0, 1]
    assert loaded[1]["task"] == "Pick up the cube"

    reviewed = dict(loaded[1])
    reviewed["selected"] = _det(1, 2, 3, 4, 1.0, "manual")
    reviewed["manual"] = True
    save_reviewed_record(labeling_dir, 1, reviewed)

    episode = load_episode_record(labeling_dir, 1)
    assert episode["original"]["selected"] is None
    assert episode["current"]["manual"] is True
    assert reviewed_path(labeling_dir).is_file()

    remove_reviewed_record(labeling_dir, 1)
    assert load_episode_record(labeling_dir, 1)["current"]["selected"] is None


def test_labeling_backend_variant_paths_and_migration(tmp_path: Path):
    labeling_dir = tmp_path / "labeling"
    labeling_dir.mkdir()
    record = {"episode_index": 0, "task": "Pick up the duck", "parsed": None, "selected": None}
    labels_path(labeling_dir).write_text(json.dumps(record) + "\n")
    source_path(labeling_dir).write_text(json.dumps({"backend": "qwen_remote"}) + "\n")

    migrate_latest_labels_to_variant(labeling_dir)

    assert labels_path(labeling_dir, "qwen_remote").is_file()
    assert resolved_labels_path(labeling_dir, "qwen_remote") == labels_path(labeling_dir, "qwen_remote")
    variants = available_label_variants(labeling_dir)
    assert variants[0]["id"] == "qwen_remote"
    assert variants[0]["is_latest"] is True


def test_labeling_variants_and_episode_record_fallback_to_reviewed_only(tmp_path: Path):
    labeling_dir = tmp_path / "labeling"
    labeling_dir.mkdir()
    record = {
        "episode_index": 7,
        "task": "Pick up the duck",
        "parsed": {"target": "duck", "direction": None, "reference": None},
        "detections_target": [],
        "selected": None,
        "reviewed": True,
    }
    reviewed_path(labeling_dir, "qwen_dashscope").write_text(json.dumps(record) + "\n")

    variants = available_label_variants(labeling_dir)
    assert variants
    assert variants[0]["id"] == "qwen_dashscope"
    assert variants[0]["labels_count"] == 1
    assert variants[0]["reviewed_count"] == 1

    episode = load_episode_record(labeling_dir, 7, variant="qwen_dashscope")
    assert episode is not None
    assert episode["original"]["task"] == "Pick up the duck"
    assert episode["current"]["reviewed"] is True


def test_run_labeling_missing_mode_keeps_existing_and_runs_only_missing(monkeypatch, tmp_path: Path):
    class Meta:
        repo_id = "local/test"
        features = {"observation.images.front": {"dtype": "image"}}
        episodes = {
            0: {"tasks": ["Pick up the yellow duck"], "length": 1},
            1: {"tasks": ["Pick up the yellow duck"], "length": 1},
            2: {"tasks": ["Pick up the yellow duck"], "length": 1},
        }

    calls = []

    class DummyDetector:
        model_id = "dummy-detector"
        device = "cpu"

        def detect_for_prompt(self, _image, prompt, **_kwargs):
            calls.append(prompt)
            return [_det(0, 0, 10, 10, 0.9, f"new:{prompt}")]

    static_dir = tmp_path / "static"
    labeling_dir = static_dir / "labeling"
    labeling_dir.mkdir(parents=True)
    old_record = {
        "episode_index": 0,
        "task": "Pick up the yellow duck",
        "parsed": {"target": "yellow duck", "direction": None, "reference": None},
        "detections_target": [_det(1, 1, 2, 2, 0.5, "old")],
        "selected": None,
    }
    reviewed_record = {
        "episode_index": 1,
        "task": "Pick up the yellow duck",
        "parsed": {"target": "yellow duck", "direction": None, "reference": None},
        "detections_target": [_det(3, 3, 4, 4, 0.5, "reviewed")],
        "selected": None,
    }
    labels_path(labeling_dir, "qwen_dashscope").write_text(json.dumps(old_record) + "\n")
    reviewed_path(labeling_dir, "qwen_dashscope").write_text(json.dumps(reviewed_record) + "\n")

    monkeypatch.setattr(labeling_runner, "load_detector", lambda *args, **kwargs: DummyDetector())
    monkeypatch.setattr(labeling_runner, "read_first_frame_image", lambda *args, **kwargs: (object(), "observation.images.front"))
    monkeypatch.setattr(labeling_runner, "_episode_exist_label_zero", lambda *args, **kwargs: False)

    result = run_labeling(
        tmp_path / "dataset",
        Meta(),
        [0, 1, 2],
        static_dir,
        backend="qwen_dashscope",
        qwen_model="qwen3.6-plus",
        output_variant="qwen_dashscope",
        run_mode="missing",
        workers=1,
        show_progress=False,
    )

    labels = load_labels_jsonl(labels_path(labeling_dir, "qwen_dashscope"))
    reviewed = load_labels_jsonl(reviewed_path(labeling_dir, "qwen_dashscope"))
    source = json.loads(source_path(labeling_dir, "qwen_dashscope").read_text())
    assert result.episodes == [2]
    assert sorted(labels) == [0, 2]
    assert labels[0]["detections_target"][0]["label"] == "old"
    assert labels[2]["detections_target"][0]["label"].startswith("new:")
    assert sorted(reviewed) == [1]
    assert len(calls) == 1
    assert source["run_mode"] == "missing"
    assert source["skipped_existing"] == 2


def test_run_labeling_full_mode_replaces_selected_and_clears_reviewed(monkeypatch, tmp_path: Path):
    class Meta:
        repo_id = "local/test"
        features = {"observation.images.front": {"dtype": "image"}}
        episodes = {0: {"tasks": ["Pick up the yellow duck"], "length": 1}}

    class DummyDetector:
        model_id = "dummy-detector"
        device = "cpu"

        def detect_for_prompt(self, _image, prompt, **_kwargs):
            return [_det(0, 0, 10, 10, 0.9, f"fresh:{prompt}")]

    static_dir = tmp_path / "static"
    labeling_dir = static_dir / "labeling"
    labeling_dir.mkdir(parents=True)
    old_record = {
        "episode_index": 0,
        "task": "Pick up the yellow duck",
        "parsed": {"target": "yellow duck", "direction": None, "reference": None},
        "detections_target": [_det(1, 1, 2, 2, 0.5, "old")],
        "selected": None,
    }
    labels_path(labeling_dir, "qwen_dashscope").write_text(json.dumps(old_record) + "\n")
    reviewed_path(labeling_dir, "qwen_dashscope").write_text(json.dumps({**old_record, "reviewed": True}) + "\n")

    monkeypatch.setattr(labeling_runner, "load_detector", lambda *args, **kwargs: DummyDetector())
    monkeypatch.setattr(labeling_runner, "read_first_frame_image", lambda *args, **kwargs: (object(), "observation.images.front"))
    monkeypatch.setattr(labeling_runner, "_episode_exist_label_zero", lambda *args, **kwargs: False)

    result = run_labeling(
        tmp_path / "dataset",
        Meta(),
        [0],
        static_dir,
        backend="qwen_dashscope",
        qwen_model="qwen3.6-plus",
        output_variant="qwen_dashscope",
        run_mode="full",
        workers=1,
        show_progress=False,
    )

    labels = load_labels_jsonl(labels_path(labeling_dir, "qwen_dashscope"))
    reviewed = load_labels_jsonl(reviewed_path(labeling_dir, "qwen_dashscope"))
    source = json.loads(source_path(labeling_dir, "qwen_dashscope").read_text())
    assert result.episodes == [0]
    assert sorted(labels) == [0]
    assert labels[0]["detections_target"][0]["label"].startswith("fresh:")
    assert reviewed == {}
    assert source["run_mode"] == "full"


def test_run_labeling_full_mode_clears_old_labels_when_no_reviewable_episode(tmp_path: Path):
    class Meta:
        repo_id = "local/test"
        features = {"observation.images.front": {"dtype": "image"}}
        episodes = {0: {"tasks": ["Place the yellow duck on the table"], "length": 1}}

    static_dir = tmp_path / "static"
    labeling_dir = static_dir / "labeling"
    labeling_dir.mkdir(parents=True)
    labels_path(labeling_dir, "qwen_dashscope").write_text(
        json.dumps({"episode_index": 0, "task": "Pick up the yellow duck", "parsed": None}) + "\n"
    )

    result = run_labeling(
        tmp_path / "dataset",
        Meta(),
        [0],
        static_dir,
        backend="qwen_dashscope",
        qwen_model="qwen3.6-plus",
        output_variant="qwen_dashscope",
        run_mode="full",
        workers=1,
        show_progress=False,
    )

    assert result.episodes == []
    assert labels_path(labeling_dir, "qwen_dashscope").read_text() == ""

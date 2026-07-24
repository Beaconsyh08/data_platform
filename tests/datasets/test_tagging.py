import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import lerobot.data_platform.precompute.tagging.vlm_backend as tag_vlm
from lerobot.data_platform.precompute.tagging.schema import DEFAULT_SELECTED_TAG_NAMES, selected_tag_defs
from lerobot.data_platform.precompute.tagging.review import (
    available_tag_variants,
    current_tags,
    load_episode_record,
    load_tags_jsonl,
    merge_tag_record,
    resolved_reviewed_path,
    resolved_tags_path,
    save_reviewed_tag,
    source_path,
    tags_path,
)
from lerobot.data_platform.precompute.tagging.runner import (
    _prompt_action_allowed_objects,
    _sync_prompt_action_mismatch_flags,
    run_tagging,
)
from lerobot.data_platform.precompute.tagging.vlm_backend import (
    DashScopeVLMTagger,
    VLMTagger,
    _build_prompt_action_match_prompt,
    _normalize_prompt_action_payload,
    _normalize_values,
)


def test_background_tag_schema_matches_supported_classes():
    tags = {
        tag["name"]: tag
        for tag in selected_tag_defs(["background", "background_color", "object_count", "prompt_action_match"])
    }
    assert tags["background"]["options"] == ["round_table", "square_table", "tv_cabinet", "sofa"]
    assert tags["background_color"]["dtype"] == "str"
    assert tags["object_count"]["dtype"] == "int"
    assert tags["prompt_action_match"]["options"] == ["match", "mismatch", "unclear"]


def test_default_selected_tags_include_prompt_action_match():
    assert "prompt_action_match" in DEFAULT_SELECTED_TAG_NAMES


def test_dashscope_tagging_capabilities_include_qwen37_plus():
    caps = tag_vlm.get_capabilities(backend="qwen_dashscope")
    assert "qwen3.7-plus" in caps["backends"]["qwen_dashscope"]["models"]


def test_remote_qwen_tagging_value_normalization():
    tag_defs = selected_tag_defs(["background", "background_color", "object_count"])
    values = _normalize_values(
        {"background_type": "rectangular table", "surface_color": "Light Brown", "num_objects": "three objects"},
        tag_defs,
    )
    assert values == {"background": "square_table", "background_color": "light brown", "object_count": 3}

    values = _normalize_values({"background": "方桌", "color": "black", "count": 1}, tag_defs)
    assert values == {"background": "square_table", "background_color": "black", "object_count": 1}

    values = _normalize_values({"background": "long table", "color": "black", "count": 1}, tag_defs)
    assert values == {"background": "square_table", "background_color": "black", "object_count": 1}

    values = _normalize_values({"background": "电视柜", "color": "white", "count": 4}, tag_defs)
    assert values == {"background": "tv_cabinet", "background_color": "white", "object_count": 4}

    values = _normalize_values({"background": "lab bench", "color": "white", "count": 1}, tag_defs)
    assert values == {"background": "lab_bench", "background_color": "white", "object_count": 1}

    match_tag = selected_tag_defs(["prompt_action_match"])
    assert _normalize_values({"status": "incorrect object"}, match_tag) == {"prompt_action_match": "mismatch"}
    assert _normalize_values({"match": "yes"}, match_tag) == {"prompt_action_match": "match"}
    assert _normalize_prompt_action_payload(
        {"prompt_action_match": "mismatch", "observed_object": "beige bear"}
    )["observed_object"] == "brown dog"
    assert _normalize_prompt_action_payload(
        {"prompt_action_match": "mismatch", "observed_object": "beige bear"},
        allowed_objects=["yellow duck", "brown dog"],
    ) == {"value": "mismatch", "observed_object": "brown dog", "reason": None}
    assert _normalize_prompt_action_payload(
        {"prompt_action_match": "mismatch", "observed_object": "purple elephant"},
        allowed_objects=["yellow duck", "brown dog"],
    ) == {"value": "unclear", "observed_object": None, "reason": None}


def test_prompt_action_allowed_objects_are_dataset_bounded():
    class Meta:
        tasks = {
            0: "Pick up the yellow duck",
            1: "Pick up the red fox on the left of the beige bear",
        }
        episodes = {
            2: {"tasks": ["Give the green dinosaur to me"]},
            3: {"tasks": ["Place the blue cube"]},
        }

    assert _prompt_action_allowed_objects(Meta()) == ["yellow duck", "red fox", "brown dog", "green dinosaur"]

    prompt = _build_prompt_action_match_prompt(
        "Pick up the yellow duck",
        image_count=2,
        allowed_objects=["yellow duck", "brown dog"],
    )
    assert "The only valid task object types in this dataset are: yellow duck, brown dog." in prompt
    assert "Do not invent new object types" in prompt
    assert '"observed_object":"yellow duck|brown dog|null"' in prompt


def test_remote_qwen_tagger_client_mock(monkeypatch):
    tag_defs = selected_tag_defs(["background", "background_color", "object_count"])
    captured = {}

    class DummyClient:
        def predict(self, **kwargs):
            captured.update(kwargs)
            return ("preview", '```json\n{"background":"sofa","background_color":"gray","object_count":2}\n```')

    class DummyImage:
        def convert(self, mode):
            assert mode == "RGB"
            return self

        def save(self, fp, format=None, quality=None):
            fp.write(b"jpeg")

    monkeypatch.setattr(tag_vlm, "handle_file", lambda path: Path(path).name)
    tagger = VLMTagger(DummyClient(), "https://example.invalid/", "qwen-test", 1024, 9800)
    values = tagger.predict_many(DummyImage(), tag_defs)
    assert values == {"background": "sofa", "background_color": "gray", "object_count": 2}
    assert captured["api_name"] == "/run_detection_streaming"
    assert "Return ONLY one JSON object" in captured["user_prompt"]
    assert "following JSON format" in captured["user_prompt"]
    assert "object_count" in captured["user_prompt"]


def test_dashscope_qwen_tagger_client_mock():
    tag_defs = selected_tag_defs(["background", "background_color", "object_count"])
    captured = {}

    class DummyTagger(DashScopeVLMTagger):
        def _post_chat_completion(self, payload):
            captured.update(payload)
            return {
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"background":"sofa","background_color":"gray","object_count":2}\n```'
                        }
                    }
                ]
            }

    class DummyImage:
        def convert(self, mode):
            assert mode == "RGB"
            return self

        def save(self, fp, format=None, quality=None):
            fp.write(b"jpeg")

    tagger = DummyTagger(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model_id="qwen3-vl-plus",
    )
    values = tagger.predict_many(DummyImage(), tag_defs)
    assert values == {"background": "sofa", "background_color": "gray", "object_count": 2}
    assert captured["model"] == "qwen3-vl-plus"
    assert captured["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_prompt_action_mismatch_flags_sync(tmp_path: Path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "annotation_issues.json").write_text(
        '[{"episode": 2, "type": "error", "reason": "existing_stage_issue"}]\n'
    )
    (static_dir / "flagged_episodes.json").write_text('{"flagged_episodes":[2,3]}\n')

    summary = _sync_prompt_action_mismatch_flags(
        static_dir,
        {
            0: {
                "episode_index": 0,
                "task": "Pick up the yellow duck",
                "tags": {"prompt_action_match": "mismatch"},
                "tag_details": {
                    "prompt_action_match": {
                        "observed_object": "dog",
                        "reason": "final frame shows the gripper holding the dog",
                    }
                },
            },
            1: {
                "episode_index": 1,
                "task": "Pick up the yellow duck",
                "tags": {"prompt_action_match": "match"},
            },
        },
        {0, 1},
        output_variant=None,
    )

    issues = json.loads((static_dir / "annotation_issues.json").read_text())
    assert summary["mismatch_episode_count"] == 1
    assert any(issue.get("episode") == 0 and issue.get("reason") == "prompt_action_mismatch" for issue in issues)
    assert {"episode": 2, "type": "error", "reason": "existing_stage_issue"} in issues
    assert json.loads((static_dir / "flagged_episodes.json").read_text())["flagged_episodes"] == [0, 2, 3]


def test_tagging_variant_paths_and_review_round_trip(tmp_path: Path):
    tagging_dir = tmp_path / "tagging"
    tagging_dir.mkdir()
    tags_path(tagging_dir, "trial").write_text(
        '{"episode_index": 2, "task": "Pick up the cube", "tags": {"background": "long_table"}}\n'
    )
    source_path(tagging_dir, "trial").write_text('{"output_variant": "trial"}\n')

    variants = available_tag_variants(tagging_dir)
    assert variants[0]["id"] == "trial"
    assert variants[0]["tags_count"] == 1
    assert resolved_tags_path(tagging_dir, None) == tags_path(tagging_dir)
    assert resolved_tags_path(tagging_dir, variants[0]["id"]) == tags_path(tagging_dir, "trial")
    assert resolved_reviewed_path(tagging_dir, variants[0]["id"]).name == "tags_reviewed_trial.jsonl"

    save_reviewed_tag(tagging_dir, 2, {"background": "round_table"}, variant="trial")
    record = load_episode_record(tagging_dir, 2, variant="trial")
    assert record["original"]["tags"]["background"] == "square_table"
    assert record["current"]["tags"]["background"] == "round_table"


def test_tag_record_merge_keeps_unselected_existing_tags():
    base = {
        "episode_index": 3,
        "task": "Pick up the duck",
        "tags": {
            "background": "sofa",
            "background_color": "gray",
            "object_count": 2,
            "arm": "left",
        },
    }
    update = {
        "episode_index": 3,
        "task": "Pick up the duck",
        "tags": {
            "grasp_xy": [0.1, 0.2],
            "prompt_action_match": "match",
        },
        "tag_details": {"prompt_action_match": {"reason": "final frame matches"}},
    }

    merged = merge_tag_record(base, update, ["grasp_xy", "prompt_action_match"])

    assert merged["tags"] == {
        "background": "sofa",
        "background_color": "gray",
        "object_count": 2,
        "arm": "left",
        "grasp_xy": [0.1, 0.2],
        "prompt_action_match": "match",
    }
    assert merged["tag_details"]["prompt_action_match"]["reason"] == "final frame matches"


def test_current_tags_merges_reviewed_fields_instead_of_replacing_record(tmp_path: Path):
    tagging_dir = tmp_path / "tagging"
    tagging_dir.mkdir()
    tags_path(tagging_dir).write_text(
        json.dumps(
            {
                "episode_index": 0,
                "task": "Pick up the cube",
                "tags": {"background": "sofa", "object_count": 2, "arm": "left"},
            }
        )
        + "\n"
    )
    resolved_reviewed_path(tagging_dir).write_text(
        json.dumps(
            {
                "episode_index": 0,
                "task": "Pick up the cube",
                "tags": {"arm": "right"},
                "reviewed": True,
            }
        )
        + "\n"
    )

    record = current_tags(tagging_dir)[0]

    assert record["tags"] == {"background": "sofa", "object_count": 2, "arm": "right"}
    assert record["reviewed"] is True


def test_run_tagging_skips_existing_tags_unless_overwrite(tmp_path: Path):
    root = tmp_path / "dataset"
    data_dir = root / "data" / "chunk-000"
    data_dir.mkdir(parents=True)

    def write_episode(index: int, rows: list[list[float]]) -> None:
        pq.write_table(
            pa.table({"action": pa.array(rows, type=pa.list_(pa.float32()))}),
            data_dir / f"episode_{index:06d}.parquet",
        )

    write_episode(0, [[0, 0, 0, 0], [1, 0, 0, 0]])
    write_episode(1, [[0, 0, 0, 0], [0, 0, 1, 0]])

    class Meta:
        repo_id = "local/test"
        episodes = {
            0: {"tasks": ["Pick up the yellow duck"], "length": 2},
            1: {"tasks": ["Pick up the yellow duck"], "length": 2},
        }
        features = {"action": {"dtype": "float32", "shape": [4]}}

        @staticmethod
        def get_data_file_path(episode_index: int) -> str:
            return f"data/chunk-000/episode_{episode_index:06d}.parquet"

    static_dir = tmp_path / "static"
    tagging_dir = static_dir / "tagging"
    tagging_dir.mkdir(parents=True)
    tags_path(tagging_dir).write_text(
        json.dumps({"episode_index": 0, "task": "Pick up the yellow duck", "tags": {"arm": "left"}}) + "\n"
    )

    result = run_tagging(root, Meta(), [0, 1], static_dir, selected_tags=["arm"], workers=1)
    records = load_tags_jsonl(tags_path(tagging_dir))
    source = json.loads(source_path(tagging_dir).read_text())

    assert result.episodes == [1]
    assert records[0]["tags"]["arm"] == "left"
    assert records[1]["tags"]["arm"] == "right"
    assert source["skipped_existing"] == 1

    result = run_tagging(root, Meta(), [0, 1], static_dir, selected_tags=["arm"], workers=1, overwrite=True)
    source = json.loads(source_path(tagging_dir).read_text())

    assert result.episodes == [0, 1]
    assert source["overwrite"] is True
    assert source["skipped_existing"] == 0


def test_tagging_variants_sort_by_latest_started_output(tmp_path: Path):
    tagging_dir = tmp_path / "tagging"
    tagging_dir.mkdir()
    tags_path(tagging_dir, "trial").write_text('{"episode_index": 2, "tags": {}}\n')
    tags_path(tagging_dir).write_text("")
    os.utime(tags_path(tagging_dir, "trial"), (100, 100))
    os.utime(tags_path(tagging_dir), (200, 200))

    variants = available_tag_variants(tagging_dir)
    assert variants[0]["id"] == "latest"
    assert variants[0]["is_latest"] is True
    assert variants[0]["tags_path"] == str(tags_path(tagging_dir))

    os.utime(tags_path(tagging_dir, "trial"), (300, 300))
    variants = available_tag_variants(tagging_dir)
    assert variants[0]["id"] == "trial"
    assert variants[0]["is_latest"] is True

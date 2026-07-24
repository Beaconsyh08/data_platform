import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.data_platform.precompute.construction import (
    ConstructionPlan,
    build_vocab,
    classify_task,
    make_prompt,
    select_sources,
    summarize_candidates,
    write_synthetic_dataset,
)


class DummyMeta:
    def __init__(self, root: Path):
        self.root = root


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _make_source_dataset(root: Path) -> None:
    (root / "meta").mkdir(parents=True)
    info = {
        "codebase_version": "v2.1",
        "robot_type": "dummy",
        "total_episodes": 1,
        "total_frames": 3,
        "total_tasks": 1,
        "total_videos": 1,
        "total_chunks": 1,
        "chunks_size": 1000,
        "fps": 10,
        "splits": {"train": "0:1"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            "action": {"dtype": "float32", "shape": [2], "names": None},
            "observation.video": {"dtype": "video", "shape": [3, 4, 4], "names": ["c", "h", "w"]},
        },
    }
    (root / "meta" / "info.json").write_text(json.dumps(info))
    _write_jsonl(root / "meta" / "tasks.jsonl", [{"task_index": 0, "task": "Pick up the yellow duck"}])
    _write_jsonl(root / "meta" / "episodes.jsonl", [{"episode_index": 0, "tasks": ["Pick up the yellow duck"], "length": 3}])
    _write_jsonl(root / "meta" / "episodes_stats.jsonl", [{"episode_index": 0, "stats": {}}])

    table = pa.table(
        {
            "timestamp": pa.array([0.0, 0.1, 0.2], type=pa.float32()),
            "frame_index": pa.array([0, 1, 2], type=pa.int64()),
            "episode_index": pa.array([0, 0, 0], type=pa.int64()),
            "index": pa.array([0, 1, 2], type=pa.int64()),
            "task_index": pa.array([0, 0, 0], type=pa.int64()),
            "action": pa.array([[0.0, 0.0], [0.1, 0.2], [0.3, 0.4]], type=pa.list_(pa.float32())),
        }
    )
    data_path = root / "data" / "chunk-000" / "episode_000000.parquet"
    data_path.parent.mkdir(parents=True)
    pq.write_table(table, data_path)

    video_path = root / "videos" / "chunk-000" / "observation.video" / "episode_000000.mp4"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"fake-video")


def _record(idx: int, task: str, selected=True):
    return {
        "episode_index": idx,
        "task": task,
        "parsed": {"target": task.removeprefix("Pick up the ").lower(), "direction": None, "reference": None},
        "selected": {"bbox": {"left": 0, "top": 0, "right": 10, "bottom": 10}, "confidence": 0.8} if selected else None,
        "relation_satisfied": True,
        "detections_target": [{"bbox": {"left": 0, "top": 0, "right": 10, "bottom": 10}, "confidence": 0.8}],
        "detections_ref": [],
    }


def test_build_vocab_from_tasks_jsonl(tmp_path: Path):
    root = tmp_path / "dataset"
    (root / "meta").mkdir(parents=True)
    _write_jsonl(
        root / "meta" / "tasks.jsonl",
        [
            {"task_index": 0, "task": "Pick up the yellow duck"},
            {"task_index": 1, "task": "Pick up the brown dog to the left of the green dinosaur"},
            {"task_index": 2, "task": "Give me the orange lion"},
        ],
    )

    assert build_vocab(DummyMeta(root)) == {"yellow duck", "brown dog", "green dinosaur", "orange lion"}


def test_classify_task_and_prompt_templates():
    assert classify_task("Pick up the yellow duck").name == "single_pick"
    assert classify_task("Pick up the yellow duck on the left").name == "directional_pick"
    assert classify_task("Pick up the yellow duck to the right of the brown dog").name == "relative_pick"
    assert classify_task("Give me the yellow duck").name == "give"
    assert make_prompt("single_pick", "brown dog", {}, []) == "Pick up the brown dog"
    assert make_prompt("directional_pick", "brown dog", {"direction": "right"}, []) == "Pick up the brown dog on the right"
    assert make_prompt(
        "relative_pick",
        "orange lion",
        {"direction": "left", "reference": "yellow duck"},
        ["yellow duck"],
    ) == "Pick up the orange lion to the left of the yellow duck"
    assert make_prompt("give", "green dinosaur", {}, []) == "Give me the green dinosaur"


def test_select_sources_balances_missing_objects():
    vocab = {"yellow duck", "brown dog", "orange lion", "green dinosaur"}
    labels = {
        idx: _record(idx, task)
        for idx, task in enumerate(
            [
                "Pick up the yellow duck",
                "Pick up the yellow duck",
                "Pick up the brown dog",
                "Pick up the orange lion",
                "Pick up the green dinosaur",
            ]
        )
    }

    plans = select_sources(labels, vocab, 50, {"single_pick": 5})
    counts = {}
    for plan in plans:
        counts[plan.missing_object] = counts.get(plan.missing_object, 0) + 1
    assert max(counts.values()) - min(counts.values()) <= 1


def test_select_sources_balances_source_visual_objects_and_oversamples():
    vocab = {"yellow duck", "brown dog", "orange lion", "green dinosaur"}
    labels = {
        idx: _record(idx, task)
        for idx, task in enumerate(
            [
                "Pick up the yellow duck",
                "Pick up the yellow duck",
                "Pick up the brown dog",
                "Pick up the brown dog",
            ]
        )
    }

    plans = select_sources(labels, vocab, 50, {"single_pick": 2}, oversample_factor=2)
    assert len(plans) == 4

    source_counts = {}
    missing_counts = {}
    for plan in plans:
        source_counts[plan.source_visual_object] = source_counts.get(plan.source_visual_object, 0) + 1
        missing_counts[plan.missing_object] = missing_counts.get(plan.missing_object, 0) + 1

    assert source_counts == {"yellow duck": 2, "brown dog": 2}
    assert max(missing_counts.values()) - min(missing_counts.values()) <= 1

    summary = summarize_candidates(labels, vocab, 50)["single_pick"]
    assert summary["source_visual_distribution"] == {"brown dog": 2, "yellow duck": 2}


def test_select_sources_balances_object_count_buckets_when_tags_exist():
    vocab = {"yellow duck", "brown dog"}
    labels = {
        idx: _record(idx, "Pick up the yellow duck")
        for idx in range(4)
    }
    tags = {
        0: {"episode_index": 0, "tags": {"object_count": 1}},
        1: {"episode_index": 1, "tags": {"object_count": 1}},
        2: {"episode_index": 2, "tags": {"object_count": 2}},
        3: {"episode_index": 3, "tags": {"object_count": 3}},
    }

    plans = select_sources(labels, vocab, 50, {"single_pick": 3}, tags_by_episode=tags)

    assert [plan.src_episode_index for plan in plans] == [0, 2, 3]
    assert [plan.object_count_bucket for plan in plans] == ["1", "2", "3"]
    summary = summarize_candidates(labels, vocab, 50, tags_by_episode=tags)["single_pick"]
    assert summary["object_count_distribution"] == {"1": 2, "2": 1, "3": 1}


def test_select_sources_balances_backgrounds_when_tags_exist():
    vocab = {"yellow duck", "brown dog"}
    labels = {idx: _record(idx, "Pick up the yellow duck") for idx in range(4)}
    tags = {
        0: {"episode_index": 0, "tags": {"background": "round_table"}},
        1: {"episode_index": 1, "tags": {"background": "round_table"}},
        2: {"episode_index": 2, "tags": {"background": "sofa"}},
        3: {"episode_index": 3, "tags": {"background": "tv_cabinet"}},
    }

    plans = select_sources(labels, vocab, 50, {"single_pick": 3}, tags_by_episode=tags)

    assert [plan.src_episode_index for plan in plans] == [0, 2, 3]
    assert [plan.background for plan in plans] == ["round_table", "sofa", "tv_cabinet"]
    summary = summarize_candidates(labels, vocab, 50, tags_by_episode=tags)["single_pick"]
    assert summary["background_distribution"] == {"round_table": 2, "sofa": 1, "tv_cabinet": 1}


def test_select_sources_can_convert_single_pick_source_to_give():
    vocab = {"yellow duck", "brown dog"}
    labels = {0: _record(0, "Pick up the yellow duck")}

    assert select_sources(labels, vocab, 50, {"give": 1}) == []

    plans = select_sources(labels, vocab, 50, {"give": 1}, allow_pick_to_give=True)

    assert len(plans) == 1
    assert plans[0].scenario == "give"
    assert plans[0].source_scenario == "single_pick"
    assert plans[0].src_task == "Pick up the yellow duck"
    assert plans[0].new_task == "Give me the brown dog"
    summary = summarize_candidates(labels, vocab, 50, allow_pick_to_give=True)
    assert summary["give"]["candidate_count"] == 1
    assert summary["give"]["source_scenario_distribution"] == {"single_pick": 1}


def test_write_synthetic_dataset_schema_tasks_and_video_links(tmp_path: Path):
    src = tmp_path / "src"
    out = tmp_path / "src_synthetic"
    _make_source_dataset(src)
    plans = [
        ConstructionPlan(
            src_episode_index=0,
            new_episode_index=0,
            scenario="single_pick",
            src_task="Pick up the yellow duck",
            new_task="Pick up the brown dog",
            missing_object="brown dog",
            src_uncertainty=10,
            detected_existing=["yellow duck"],
            detected_missing=["brown dog"],
        )
    ]

    result = write_synthetic_dataset(src, plans, out, include_positives=False, source_repo_id="local/src")
    assert result["negative_episodes"] == 1
    assert result["positive_episodes"] == 0

    info = json.loads((out / "meta" / "info.json").read_text())
    assert info["features"]["exist_label"] == {"dtype": "int32", "shape": [1], "names": None}
    assert info["total_episodes"] == 1

    negative = pq.read_table(out / "data" / "chunk-000" / "episode_000000.parquet")
    assert negative["exist_label"].to_pylist() == [0, 0, 0]
    assert negative["task_index"].to_pylist() == [0, 0, 0]

    tasks = [json.loads(line) for line in (out / "meta" / "tasks.jsonl").read_text().splitlines()]
    assert tasks[-1] == {"task_index": 0, "task": "Pick up the brown dog"}
    plan = json.loads((out / "meta" / "construction_plan.json").read_text())
    assert plan["include_positives"] is False
    assert plan["records"][0]["new_episode_index"] == 0

    src_video = src / "videos" / "chunk-000" / "observation.video" / "episode_000000.mp4"
    out_video = out / "videos" / "chunk-000" / "observation.video" / "episode_000000.mp4"
    assert out_video.is_file()
    assert os.stat(src_video).st_ino == os.stat(out_video).st_ino

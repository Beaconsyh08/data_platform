"""MEM export isolation across real shared Parquet/video shards and immutable source snapshots."""

import io
import json
from fractions import Fraction

import av
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

from lerobot.data_platform import mem_export as mem


def fixture(tmp_path, version="v3.0", video=False):
    root = tmp_path / "source"
    (root / "meta").mkdir(parents=True)
    (root / "data").mkdir()
    info = {
        "codebase_version": version,
        "fps": 2,
        "chunks_size": 1000,
        "data_path": "data/shared.parquet" if version == "v3.0" else "data/{episode_index:06d}.parquet",
        "video_path": "videos/{video_key}/shared.mp4"
        if version == "v3.0"
        else "videos/{video_key}/{episode_index:06d}.mp4",
        "features": {"head": {"dtype": "video" if video else "image"}, "wrist": {"dtype": "image"}},
    }
    mem.write_json(root / "meta/info.json", info)
    episodes = [
        {
            "episode_index": ep,
            "tasks": ["Put objects in basket"],
            "length": 2,
            "data/chunk_index": 0,
            "data/file_index": 0,
            "videos/head/chunk_index": 0,
            "videos/head/file_index": 0,
            "videos/head/from_timestamp": float(ep),
            "videos/head/to_timestamp": float(ep + 1),
        }
        for ep in (0, 1)
    ]
    if version == "v3.0":
        (root / "meta/episodes").mkdir()
        pq.write_table(pa.Table.from_pylist(episodes), root / "meta/episodes/file.parquet")
        pq.write_table(
            pa.Table.from_pylist([{"task_index": 0, "task": "Put objects in basket"}]),
            root / "meta/tasks.parquet",
        )
    else:
        (root / "meta/episodes.jsonl").write_text("".join(json.dumps(e) + "\n" for e in episodes))
    rows = []
    for ep in (0, 1):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "red" if ep == 0 else "blue").save(buffer, format="PNG")
        image = {"bytes": buffer.getvalue(), "path": None}
        for frame in (0, 1):
            row = {"episode_index": ep, "frame_index": frame, "timestamp": frame / 2, "wrist": image}
            if not video:
                row["head"] = image
            rows.append(row)
    if version == "v3.0":
        pq.write_table(pa.Table.from_pylist(rows), root / "data/shared.parquet")
    else:
        for ep in (0, 1):
            pq.write_table(
                pa.Table.from_pylist([r for r in rows if r["episode_index"] == ep]),
                root / f"data/{ep:06d}.parquet",
            )
    if video:
        (root / "videos/head").mkdir(parents=True)
        groups = (
            [(root / "videos/head/shared.mp4", [0, 0, 1, 1])]
            if version == "v3.0"
            else [(root / f"videos/head/{ep:06d}.mp4", [ep, ep]) for ep in (0, 1)]
        )
        for path, colors in groups:
            with av.open(str(path), "w") as container:
                stream = container.add_stream("mpeg4", rate=2)
                stream.width = stream.height = 16
                stream.pix_fmt = "yuv420p"
                for i, ep in enumerate(colors):
                    frame = av.VideoFrame.from_image(Image.new("RGB", (16, 16), "red" if ep == 0 else "blue"))
                    frame.pts, frame.time_base = i, Fraction(1, 2)
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
    artifact = tmp_path / "caption.json"
    mem.write_json(
        artifact,
        {
            "dataset_key": "fixture",
            "episode_index": 1,
            "frame_count": 2,
            "fps": 2,
            "variant": "fusion_reviewed",
            "review": {"human_reviewed": False},
            "result": {
                "summary": "Blue object",
                "limitations": [],
                "segments": [
                    {
                        "start_frame": 0,
                        "end_frame": 2,
                        "caption": "蓝色",
                        "caption_en": "Blue object",
                        "evidence": "Visible blue object",
                        "confidence": "high",
                        "uncertainty": "",
                    }
                ],
            },
        },
    )
    selection = {
        "root": str(root),
        "dataset_key": "fixture",
        "episode_index": 1,
        "cameras": ["head", "wrist"],
        "annotation": str(artifact),
        "task": "Put objects in basket",
        "lineage_group": "original-episode-1",
    }
    manifest = tmp_path / "selection.json"
    mem.write_json(manifest, {"schema": "mem_export_selection/v1", "episodes": [selection]})
    return root, manifest, selection


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
@pytest.mark.parametrize("video", [False, True])
def test_roundtrip_isolates_selected_episode_and_raw_views(tmp_path, version, video):
    root, manifest, _ = fixture(tmp_path, version, video)
    before = {str(p): mem.digest(p) for p in root.rglob("*") if p.is_file()}
    output = tmp_path / "export"
    report = mem.export(manifest, output, dry_run=True)
    assert not output.exists() and report["frames"] == 2 and report["images"] == 4
    mem.export(manifest, output)
    package = json.loads((output / "manifest.json").read_text())
    episode = json.loads((output / "episodes.jsonl").read_text())
    frames = [json.loads(line) for line in (output / episode["frames_file"]).read_text().splitlines()]
    assert episode["episode_index"] == 1 and episode["cameras"] == ["head", "wrist"]
    for name, sha in package["files"].items():
        assert mem.digest(output / name) == sha
    for frame in frames:
        for item in frame["images"]:
            with Image.open(output / item["path"]) as image:
                r, g, b = image.getpixel((8, 8))
                assert b > 240 and r < 15 and g < 15  # No red frames from episode zero or labels.
        if video:
            assert frame["images"][0]["source_timestamp"] == frame["timestamp"] + (
                1 if version == "v3.0" else 0
            )
    normalized = json.loads((output / episode["annotation"]).read_text())
    assert normalized["human_review"] is None and normalized["model_review"]["human_reviewed"] is False
    assert before == {str(p): mem.digest(p) for p in root.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        mem.export(manifest, output)


@pytest.mark.parametrize(
    "change",
    [
        {"task": ""},
        {"cameras": ["missing"]},
        {"annotation_sha256": "stale"},
        {"source_files_sha256": {"bad": "stale"}},
    ],
)
def test_reject_invalid_selection_before_publication(tmp_path, change):
    _, manifest, selection = fixture(tmp_path)
    selection.update(change)
    mem.write_json(manifest, {"schema": "mem_export_selection/v1", "episodes": [selection]})
    with pytest.raises(ValueError):
        mem.export(manifest, tmp_path / "export")
    assert not (tmp_path / "export").exists()


def test_reject_source_output_and_escaping_symlink(tmp_path):
    root, manifest, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        mem.export(manifest, root / "export")
    (tmp_path / "external.json").write_text("{}")
    (root / "meta/external.json").symlink_to(tmp_path / "external.json")
    with pytest.raises(ValueError, match="escaping"):
        mem.export(manifest, tmp_path / "export")


def test_cleanup_on_decode_failure(tmp_path, monkeypatch):
    _, manifest, _ = fixture(tmp_path, video=True)

    def broken(*args):
        raise ValueError("bad timestamps")

    monkeypatch.setattr(mem, "_video_images", broken)
    with pytest.raises(ValueError, match="timestamps"):
        mem.export(manifest, tmp_path / "export")
    assert not (tmp_path / "export").exists() and not list(tmp_path.glob(".export-*"))


def test_detect_changed_annotation_during_export(tmp_path, monkeypatch):
    _, manifest, selection = fixture(tmp_path)
    original = mem.shutil.copyfile

    def copy_and_change(source, destination):
        result = original(source, destination)
        if str(source) == selection["annotation"]:
            source.write_text(source.read_text() + "\n")
        return result

    monkeypatch.setattr(mem.shutil, "copyfile", copy_and_change)
    with pytest.raises(ValueError, match="changed during export"):
        mem.export(manifest, tmp_path / "export")
    assert not (tmp_path / "export").exists()

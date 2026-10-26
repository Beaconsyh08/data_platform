"""Real v2/v3 artifact equivalence and publication failure checks."""

import hashlib
import json

import av
import pandas as pd
import pytest

from lerobot.data_platform.cli import run_precompute
from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3
from tests.datasets.test_preprocess_ops import _add_v21_stat_counts, _make_dataset, _png_bytes


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def image_dataset(root):
    _make_dataset(root)
    _add_v21_stat_counts(root)
    info_path = root / "meta/info.json"
    info = json.loads(info_path.read_text())
    for key in ("front", "left", "right"):
        info["features"][key] = {
            "dtype": "image",
            "shape": [16, 16, 3],
            "names": ["height", "width", "channels"],
        }
    info_path.write_text(json.dumps(info))
    for path in (root / "data").rglob("*.parquet"):
        data = pd.read_parquet(path)
        for key in ("front", "left", "right"):
            data[key] = [{"bytes": _png_bytes(i * 20), "path": None} for i in range(len(data))]
        data.to_parquet(path, index=False)


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
@pytest.mark.parametrize("videos,csv", [(True, True), (True, False), (False, True)])
def test_parallel_matches_serial_and_keeps_source(tmp_path, monkeypatch, version, videos, csv):
    monkeypatch.setenv("DATA_PLATFORM_VIDEO_THREADS", "1")
    root = tmp_path / "src"
    image_dataset(root)
    if version == "v3.0":
        run_convert_v3(root, tmp_path / "v3", workers=2)
        root = tmp_path / "v3"
    before = snapshot(root)
    for workers in (1, 4):
        output = tmp_path / f"output-{workers}"
        run_precompute(
            root=root,
            repo_id="local/test",
            output_dir=output,
            episodes=[0],
            prepare_videos=videos,
            prepare_csv=csv,
            prepare_workers=workers,
            visualize_only=True,
            show_progress=False,
        )
        assert (output / "static/viewer_manifest.json").is_file()
    one, many = (tmp_path / f"output-{w}/static" for w in (1, 4))
    for path in one.rglob("*.csv"):
        pd.testing.assert_frame_equal(pd.read_csv(path), pd.read_csv(many / path.relative_to(one)))
    for path in one.rglob("*.mp4"):

        def frames(p):
            with av.open(str(p)) as container:
                return [(frame.width, frame.height, float(frame.time)) for frame in container.decode(video=0)]

        assert frames(path) == frames(many / path.relative_to(one))
    # A second invocation reuses the same files.
    cache = snapshot(many)
    run_precompute(
        root=root,
        repo_id="local/test",
        output_dir=many.parent,
        episodes=[0],
        prepare_videos=videos,
        prepare_csv=csv,
        prepare_workers=4,
        visualize_only=True,
        show_progress=False,
    )
    assert {k: v for k, v in snapshot(many).items() if k.endswith((".csv", ".mp4"))} == {
        k: v for k, v in cache.items() if k.endswith((".csv", ".mp4"))
    }
    assert snapshot(root) == before


def test_failed_camera_does_not_publish_manifest(tmp_path, monkeypatch):
    from lerobot.data_platform import cli

    root = tmp_path / "src"
    image_dataset(root)

    def fail(*args, **kwargs):
        raise RuntimeError("camera error")

    monkeypatch.setattr(cli, "encode_episode_video", fail)
    output = tmp_path / "output"
    with pytest.raises(RuntimeError, match="camera error"):
        run_precompute(
            root=root, output_dir=output, prepare_workers=4, visualize_only=True, show_progress=False
        )
    assert not (output / "static/viewer_manifest.json").exists()

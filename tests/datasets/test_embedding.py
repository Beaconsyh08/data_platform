import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.data_platform.precompute.embedding.reducer import fit_reducer, transform
from lerobot.data_platform.precompute.embedding.policy_backend import DEFAULT_OPENPI_CONFIG, resolve_openpi_config
from lerobot.data_platform.precompute.embedding.runner import project_existing_embeddings, run_embedding


class DummyMeta:
    repo_id = "local/dummy"
    features = {"action": {"dtype": "float32"}}
    episodes = {0: {"tasks": ["task0"], "length": 2}, 1: {"tasks": ["task1"], "length": 2}}

    def get_data_file_path(self, episode_index: int) -> Path:
        return Path(f"data/chunk-000/episode_{episode_index:06d}.parquet")


def _make_dataset(root: Path) -> None:
    (root / "data" / "chunk-000").mkdir(parents=True)
    for episode_index in [0, 1]:
        table = pa.table(
            {
                "action": pa.array([[episode_index, 0.0], [episode_index + 1.0, 0.2]], type=pa.list_(pa.float32())),
                "state": pa.array([[0.0, episode_index], [0.1, episode_index + 1.0]], type=pa.list_(pa.float32())),
            }
        )
        pq.write_table(table, root / DummyMeta().get_data_file_path(episode_index))


def test_reducer_transform_shape():
    x = np.asarray([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    reducer = fit_reducer(x)
    coords = transform(reducer, x)
    assert coords.shape == (3, 2)


def test_run_embedding_writes_artifacts(tmp_path: Path):
    root = tmp_path / "dataset"
    static = tmp_path / "static"
    ckpt = tmp_path / "ckpt.bin"
    tmp_path.mkdir(parents=True, exist_ok=True)
    ckpt.write_bytes(b"dummy")
    _make_dataset(root)

    result = run_embedding(root, DummyMeta(), None, static, ckpt, layer_hook="episode_stats_fallback")

    assert result.points == 2
    assert (static / "embedding" / "embeddings.npz").is_file()
    assert (static / "embedding" / "coords_2d.npz").is_file()
    source = json.loads((static / "embedding" / "source.json").read_text())
    assert source["ckpt_hash"]
    assert source["adapter"] == "episode_stats_fallback"


def test_project_existing_embeddings_without_rerun(tmp_path: Path):
    static = tmp_path / "static"
    emb_dir = static / "embedding"
    emb_dir.mkdir(parents=True)
    np.savez_compressed(
        emb_dir / "embeddings.npz",
        episode_index=np.asarray([0, 1, 2], dtype=np.int64),
        embeddings=np.asarray([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    )
    (emb_dir / "source.json").write_text(json.dumps({"adapter": "test"}))

    projection = project_existing_embeddings(static, method="pca", seed=7, n_neighbors=2, min_dist=0.05, metric="cosine")

    coords = np.load(emb_dir / "coords_2d.npz")
    source = json.loads((emb_dir / "source.json").read_text())
    assert coords["coords"].shape == (3, 2)
    assert projection["actual_method"] == "pca"
    assert source["projection"]["seed"] == 7
    assert source["projection"]["points"] == 3


def test_resolve_openpi_config_from_checkpoint_metadata(tmp_path: Path):
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    (ckpt / "train_config_full.json").write_text(json.dumps({"name": "pi05_h10w_dual_full_finetune_0417_ALL"}))

    assert resolve_openpi_config(ckpt, None) == "pi05_h10w_dual_full_finetune_0417_ALL"
    assert resolve_openpi_config(ckpt, "manual_config") == "manual_config"
    assert resolve_openpi_config(tmp_path / "missing", None) == DEFAULT_OPENPI_CONFIG

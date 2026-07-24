from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    import umap

    _UMAP_AVAILABLE = True
except Exception:
    umap = None
    _UMAP_AVAILABLE = False


@dataclass
class PCAReducer:
    mean: np.ndarray
    components: np.ndarray

    def transform(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) @ self.components.T


def capabilities() -> dict:
    return {
        "umap_available": bool(_UMAP_AVAILABLE),
        "fallback": "pca",
        "projection_methods": ["auto", "umap", "pca"],
        "projection_metrics": ["cosine", "euclidean", "manhattan"],
    }


def _fit_pca(x: np.ndarray):
    mean = x.mean(axis=0)
    centered = x - mean
    if x.shape[0] == 1:
        components = np.zeros((2, x.shape[1]), dtype=np.float32)
        components[0, 0] = 1.0
    else:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
        components = np.zeros((2, x.shape[1]), dtype=np.float32)
        components[: min(2, vt.shape[0]), :] = vt[:2]
    return PCAReducer(mean.astype(np.float32), components.astype(np.float32))


def fit_reducer(
    x: np.ndarray,
    seed: int = 42,
    method: str = "auto",
    n_neighbors: int = 15,
    min_dist: float = 0.1,
    metric: str = "euclidean",
):
    x = np.asarray(x, dtype=np.float32)
    if x.shape[0] == 0:
        return PCAReducer(np.zeros((x.shape[1],), dtype=np.float32), np.zeros((2, x.shape[1]), dtype=np.float32))
    method = (method or "auto").strip().lower()
    use_umap = method == "umap" or (method == "auto" and _UMAP_AVAILABLE)
    if use_umap and _UMAP_AVAILABLE and x.shape[0] >= 3:
        reducer = umap.UMAP(
            n_components=2,
            random_state=seed,
            n_neighbors=max(2, min(int(n_neighbors), x.shape[0] - 1)),
            min_dist=float(min_dist),
            metric=metric or "euclidean",
        )
        reducer.fit(x)
        return reducer
    return _fit_pca(x)


def reducer_name(reducer) -> str:
    if isinstance(reducer, PCAReducer):
        return "pca"
    return reducer.__class__.__name__.lower()


def transform(reducer, x: np.ndarray) -> np.ndarray:
    coords = reducer.transform(np.asarray(x, dtype=np.float32))
    coords = np.asarray(coords, dtype=np.float32)
    if coords.ndim == 1:
        coords = coords.reshape(1, -1)
    if coords.shape[1] < 2:
        coords = np.pad(coords, ((0, 0), (0, 2 - coords.shape[1])))
    return coords[:, :2]


def save_reducer(path: Path, reducer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("wb") as f:
        pickle.dump(reducer, f)


def load_reducer(path: Path):
    with Path(path).open("rb") as f:
        return pickle.load(f)

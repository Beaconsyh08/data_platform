"""Metadata-only datasets shared by HTTP composition and computation workers."""

from pathlib import Path

from lerobot.common.datasets.lerobot_dataset import LeRobotDatasetMetadata
from lerobot.data_platform.precompute.dataset_io import V3DatasetMetadata, is_v3_dataset


class MetaOnlyDataset:
    def __init__(
        self,
        repo_id: str,
        root: str | Path | None = None,
        revision: str | None = None,
        force_cache_sync: bool = False,
    ):
        self.repo_id = repo_id
        root_path = Path(root) if root is not None else None
        if root_path is not None and is_v3_dataset(root_path):
            self.meta = V3DatasetMetadata(repo_id=repo_id, root=root_path)
        else:
            self.meta = LeRobotDatasetMetadata(
                repo_id=repo_id, root=root, revision=revision, force_cache_sync=force_cache_sync
            )
        self.root = self.meta.root
        self.features = self.meta.features
        self.fps = self.meta.fps
        self.codebase_version = self.meta.info.get("codebase_version", "unknown")
        self.total_frames = self.meta.total_frames
        self.total_episodes = self.meta.total_episodes

"""Versioned GenDAS import configuration shared with the isolated v3 worker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

PROTOCOL = 1
OPERATIONS = {"import.mcap.inspect", "import.mcap.convert"}


def default_output_root(source: str, writable_roots: list[str]) -> str | None:
    """Mirror a source tree into its writable sibling, using Agent paths only."""
    path = PurePosixPath(source)
    roots = [PurePosixPath(root) for root in writable_roots]
    if not path.is_absolute() or not path.name or ".." in path.parts:
        return None
    roots = [root for root in roots if root.is_absolute() and ".." not in root.parts]
    ancestors = [path, *[parent for parent in path.parents if parent.name]]
    # Prefer an explicitly configured paired root over a broad writable ancestor.
    for ancestor in ancestors:
        paired = ancestor.with_name(f"{ancestor.name}_lerobot")
        if paired in roots:
            return str(paired / path.relative_to(ancestor))
    sibling = path.with_name(f"{path.name}_lerobot") if path.name else None
    if sibling and any(sibling.is_relative_to(root) for root in roots):
        return str(sibling)
    # No suitable destination on this source's storage; require an explicit choice.
    return None


@dataclass(frozen=True)
class McapImportConfig:
    adapter: str = "genrobot_das"
    target_version: str = "v3.0"
    short_side: int = 256
    fps: int = 30
    max_files: int = 3
    image_tolerance_ms: int = 20
    interpolation_gap_ms: int = 100
    min_frames: int = 30
    invalid_policy: str = "valid_segments"

    @classmethod
    def from_dict(cls, value: dict) -> McapImportConfig:
        if not isinstance(value, dict):
            raise ValueError("config must be an object")
        if set(value) - set(cls.__dataclass_fields__):
            raise ValueError("Unsupported import configuration fields")
        config = cls(**value)
        if (config.adapter, config.target_version, config.fps, config.invalid_policy) != (
            "genrobot_das",
            "v3.0",
            30,
            "valid_segments",
        ):
            raise ValueError("Only GenDAS to v3.0 at 30 FPS with valid_segments is supported")
        for key, low, high in (
            ("short_side", 64, 1600),
            ("max_files", 0, 100000),
            ("image_tolerance_ms", 1, 33),
            ("interpolation_gap_ms", 1, 1000),
            ("min_frames", 2, 900),
        ):
            val = getattr(config, key)
            if isinstance(val, bool) or not isinstance(val, int) or not low <= val <= high:
                raise ValueError(f"{key} must be an integer in [{low}, {high}]")
        if config.short_side % 2:
            raise ValueError("short_side must be even")
        return config

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

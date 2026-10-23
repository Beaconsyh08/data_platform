"""Reuse content hashes for unchanged caption inputs without caching dataset reads."""

from __future__ import annotations

import fcntl
import hashlib
import json
import time
from pathlib import Path

from lerobot.data_platform.lifecycle import _canonical_json, _file_sha256, validate_dataset_root
from lerobot.data_platform.temporal_caption import write_json


def _inventory(root: Path) -> dict[str, list]:
    result = {}
    for name in ("meta", "data", "videos"):
        for path in sorted((root / name).rglob("*")):
            if path.is_symlink() and not path.resolve(strict=True).is_relative_to(root):
                raise ValueError("Caption source asset escapes dataset root")
            if not path.is_file():
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("Caption source asset escapes dataset root")
            stat, link = path.stat(), path.lstat()
            result[path.relative_to(root).as_posix()] = [
                str(resolved),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                link.st_ino,
                link.st_mtime_ns,
                link.st_ctime_ns,
            ]
    return result


def caption_source_fingerprint(root: Path, cache: Path, progress=None) -> str:
    """Match the lifecycle content digest; ctime also invalidates restored mtimes.

    The service-owned cache is outside source data. Every call inventories all assets,
    hashes new/changed files, and rechecks the inventory before publishing a digest.
    A per-dataset process lock shares a cold scan across concurrent episode jobs.
    """
    root = validate_dataset_root(root).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(root).encode()).hexdigest()
    path = cache / f"{key}.json"
    started = time.monotonic()
    progress = progress or (lambda message: None)
    with (cache / f"{key}.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                progress("Caption source check waiting for a shared scan")
                time.sleep(5)
        before = _inventory(root)
        try:
            previous = json.loads(path.read_text())
            if previous.get("schema_version") != 1 or previous.get("root") != str(root):
                previous = {}
            previous = previous.get("files", {})
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError, AttributeError):
            previous = {}
        records, files = {}, []
        hashed = 0
        for name, signature in before.items():
            old = previous.get(name, {})
            digest = old.get("sha256") if isinstance(old, dict) else None
            if (
                not isinstance(old, dict)
                or old.get("signature") != signature
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                digest = _file_sha256(root / name)
                hashed += 1
            records[name] = {"signature": signature, "sha256": digest}
            files.append({"path": name, "size": signature[3], "sha256": digest})
        if _inventory(root) != before:
            raise ValueError("Caption source changed during content verification")
        fingerprint = hashlib.sha256(
            _canonical_json({"schema_version": 1, "files": files}).encode()
        ).hexdigest()
        write_json(path, {"schema_version": 1, "root": str(root), "files": records})
    progress(
        f"Caption source check · {time.monotonic() - started:.1f}s · {hashed} hashed, "
        f"{len(files) - hashed} reused"
    )
    return fingerprint

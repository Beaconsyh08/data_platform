"""Lossless storage for Viewer CSV caches; public CSV names remain unchanged."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import os
import shutil
import uuid
from pathlib import Path


def logical_csv_path(path: Path) -> Path:
    return path.with_suffix("") if path.name.endswith(".csv.gz") else path


def resolve_cached_csv(path: Path) -> Path:
    logical = logical_csv_path(path)
    compressed = logical.with_name(logical.name + ".gz")
    return compressed if compressed.is_file() else logical


def cached_csv_files(directory: Path, pattern: str = "episode_*_ds*.csv") -> list[Path]:
    files = {path.name: path for path in directory.glob(pattern)}
    files.update({logical_csv_path(path).name: path for path in directory.glob(pattern + ".gz")})
    return [files[name] for name in sorted(files)]


@contextlib.contextmanager
def open_cached_csv(path: Path, mode: str = "r", *, newline: str | None = None):
    if mode == "r" or path.suffix != ".gz":
        path = resolve_cached_csv(path)
    if mode == "r":
        with contextlib.ExitStack() as stack:
            try:
                opener = gzip.open if path.suffix == ".gz" else open
                handle = stack.enter_context(opener(path, "rt", encoding="utf-8", newline=newline))
            except FileNotFoundError:
                # Migration may publish gzip between the lookup and opening plaintext.
                path = resolve_cached_csv(path)
                handle = stack.enter_context(gzip.open(path, "rt", encoding="utf-8", newline=newline))
            yield handle
        return
    if mode != "w":
        raise ValueError("Viewer CSV mode must be r or w")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        os.close(descriptor)
        with (
            gzip.open(temporary, "wt", encoding="utf-8", newline=newline, compresslevel=6)
            if path.suffix == ".gz"
            else temporary.open("w", encoding="utf-8", newline=newline)
        ) as handle:
            yield handle
        _inherit_permissions(path, temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_cache_bytes(path: Path, *, max_bytes: int | None = None) -> bytes:
    path = resolve_cached_csv(path) if logical_csv_path(path).suffix == ".csv" else path
    opener = gzip.open if path.name.endswith(".csv.gz") else Path.open
    with opener(path, "rb") as stream:
        data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError("Dataset result sidecars exceed the synchronization size limit")
    return data


def _inherit_permissions(source: Path, target: Path) -> None:
    if source.is_file():
        stat = source.stat()
        if hasattr(os, "listxattr"):
            for name in os.listxattr(source):
                if name == "system.posix_acl_access":
                    os.setxattr(target, name, os.getxattr(source, name))
        os.chmod(target, stat.st_mode & 0o777)
        if os.geteuid() == 0:
            os.chown(target, stat.st_uid, stat.st_gid)


def compress_file(path: Path, *, remove_original: bool = True) -> Path:
    """Publish verified gzip before removing plaintext. Caller holds the owning cache/log lock."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Only regular cache files can be compressed")
    target = path.with_name(path.name + ".gz")
    if target.is_symlink():
        raise ValueError("Compressed cache file must not be a symbolic link")
    if target.is_file():
        # Resume a crash after gzip publication but before removing plaintext.
        with path.open("rb") as source, gzip.open(target, "rb") as zipped:
            while chunk := source.read(1024 * 1024):
                if chunk != zipped.read(len(chunk)):
                    raise FileExistsError("Compressed cache differs from plaintext")
            if zipped.read(1):
                raise FileExistsError("Compressed cache differs from plaintext")
        if remove_original:
            path.unlink()
        return target
    before = path.stat()
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as raw, path.open("rb") as source:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as zipped:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    zipped.write(chunk)
            raw.flush()
            os.fsync(raw.fileno())
        verified = hashlib.sha256()
        with gzip.open(temporary, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                verified.update(chunk)
        after = path.stat()
        if digest.digest() != verified.digest() or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Cache file changed during compression")
        shutil.copystat(path, temporary)
        _inherit_permissions(path, temporary)
        os.replace(temporary, target)
        if remove_original:
            path.unlink()
        return target
    finally:
        temporary.unlink(missing_ok=True)


def compress_viewer_csvs(static: Path) -> None:
    """Compact only the CSV directory; media and annotation sidecars are untouched."""
    directory = static / "csv"
    if directory.is_symlink():
        raise ValueError("Viewer CSV directory must not be a symbolic link")
    for path in directory.glob("episode_*_ds*.csv"):
        compress_file(path)

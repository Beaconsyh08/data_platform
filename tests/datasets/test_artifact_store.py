"""Artifacts must survive interrupted transfer without filling disk or deleting live references."""

import json
import shutil
from dataclasses import replace

import pytest

from lerobot.data_platform.artifact_store import (
    ArchiveCapacityError,
    ArchiveConfig,
    ArtifactStore,
    ArtifactUnavailableError,
)


class MemoryTransport:
    def __init__(self):
        self.objects = {}
        self.offline = False
        self.reads = 0

    def exists(self, key):
        if self.offline:
            raise ArtifactUnavailableError("offline")
        return key in self.objects

    def put(self, key, path):
        self.objects[key] = path.read_bytes()

    def download(self, key, path, expected_size):
        self.reads += 1
        if self.offline:
            raise ArtifactUnavailableError("offline")
        if key not in self.objects:
            raise ArtifactUnavailableError("missing")
        path.write_bytes(self.objects[key])

    def delete(self, key):
        del self.objects[key]


@pytest.fixture
def archive(tmp_path):
    config = ArchiveConfig(
        "test-bucket",
        "cn-beijing",
        "dev",
        tmp_path / "archive",
        enabled=True,
        unlimited=True,
        min_free_bytes=0,
        min_free_fraction=0,
    )
    return ArtifactStore(config, MemoryTransport())


def source(tmp_path, value):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(value))
    return path


def test_offline_retry_verified_content_and_cache(archive, tmp_path):
    value = {"data": list(range(400))}
    ref = archive.enqueue(source(tmp_path, value), "reports")
    archive.transport.offline = True
    with pytest.raises(ArtifactUnavailableError):
        archive.flush()
    assert list((archive.root / "pending").glob("*.json"))
    assert not archive.verified(ref)
    archive.transport.offline = False
    assert archive.flush() == [ref]
    assert archive.verified(ref)
    assert not list((archive.root / "pending").iterdir())
    assert archive.read_json(ref) == value
    reads = archive.transport.reads
    archive.transport.offline = True
    assert archive.read_json(ref) == value
    assert archive.transport.reads == reads


def test_corrupt_remote_keeps_spool_and_cannot_publish(archive, tmp_path):
    ref = archive.enqueue(source(tmp_path, {"a": 1}), "reports")
    archive.transport.objects[ref.key] = b"corrupt"
    with pytest.raises(ArtifactUnavailableError):
        archive.flush()
    assert not archive.verified(ref)
    assert list((archive.root / "pending").glob("*.gz"))


def test_spool_limit_and_disk_headroom(archive, tmp_path, monkeypatch):
    archive.config = replace(archive.config, spool_bytes=32)
    with pytest.raises(ArchiveCapacityError):
        archive.enqueue(source(tmp_path, {"a": 1}), "reports")
    assert not list((archive.root / "pending").iterdir())
    monkeypatch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 99, 1))
    archive.config = replace(archive.config, min_free_bytes=2)
    with pytest.raises(ArchiveCapacityError):
        archive.check_space()


def test_cross_environment_and_checksum_rejected(archive, tmp_path):
    ref = archive.enqueue(source(tmp_path, {"a": 1}), "reports")
    with pytest.raises(ValueError):
        archive.read_json(replace(ref, key=ref.key.replace("/dev/", "/prod/")))
    archive.flush()
    with pytest.raises(ArtifactUnavailableError):
        archive.read_json(
            replace(ref, sha256="0" * 64, key=archive.config.prefix + "reports/" + "0" * 64 + ".gz")
        )


def test_cleanup_never_deletes_reports_or_protected_evidence(archive, tmp_path):
    report = archive.enqueue(source(tmp_path, {"a": 1}), "reports")
    evidence = archive.enqueue(source(tmp_path, {"a": 2}), "acceptance-success")
    archive.flush()
    assert archive.cleanup(protected_keys={evidence.key}, now=10**12) == 0
    assert archive.cleanup(protected_keys=(), now=10**12) == 1
    assert report.key in archive.transport.objects


def test_backup_requires_completion_and_keeps_three(archive, tmp_path):
    with pytest.raises(ValueError):
        archive.enqueue(source(tmp_path, {"a": 1}), "backups")
    for n in range(5):
        archive.enqueue(source(tmp_path, {"a": n}), "backups", completed_backup=True)
    archive.flush()
    assert archive.cleanup(protected_keys=(), now=10**12) == 2
    assert len(archive.transport.objects) == 3


def test_interrupted_scratch_files_are_reclaimed_under_lock(archive, tmp_path):  # noqa: F811
    stale = archive.root / "pending/enqueue-dead.partial"
    stale.write_bytes(b"partial compressed bytes")
    ref = archive.enqueue(source(tmp_path, {"ok": True}), "reports")
    assert not stale.exists()
    stale = archive.root / "verify-dead.partial"
    stale.write_bytes(b"partial remote bytes")
    archive.flush()
    assert not stale.exists()
    stale = archive.root / "cache/dead.partial"
    stale.write_bytes(b"partial cached bytes")
    assert archive.read_json(ref) == {"ok": True}
    assert not stale.exists()


def test_finite_cloud_quota_cannot_silently_enable_unenforced_budget(archive):  # noqa: F811
    with pytest.raises(ValueError, match="billing gate"):
        replace(archive.config, unlimited=False, remote_capacity_bytes=1024, monthly_budget=1)


def test_live_sqlite_file_is_never_an_archive_attachment(archive, tmp_path):  # noqa: F811
    path = tmp_path / "database"
    path.write_bytes(b"SQLite format 3\x00" + b"content")
    with pytest.raises(ValueError, match="completed backup"):
        archive.enqueue(path, "reports")
    assert not list((archive.root / "pending").iterdir())

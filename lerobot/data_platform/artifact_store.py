"""Optional OSS artifacts with verified immutable objects and bounded local storage.

This module never receives source dataset paths from HTTP requests. Callers must authorize
an artifact through its owning job/release before reading. No credentials are serialized.
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

GIB = 1024**3
CATEGORIES = {"reports", "acceptance-success", "acceptance-failure", "backups"}


class ArtifactUnavailableError(RuntimeError):
    pass


class ArchiveCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveConfig:
    bucket: str
    region: str
    environment: str
    root: Path
    enabled: bool = False
    migration_enabled: bool = False
    cache_bytes: int = 2 * GIB
    spool_bytes: int = 5 * GIB
    min_free_bytes: int = 10 * GIB
    min_free_fraction: float = 0.1
    # None is an explicit unlimited setting; it is never inferred from a missing value.
    remote_capacity_bytes: int | None = None
    monthly_budget: float | None = None
    unlimited: bool = False

    def __post_init__(self):
        if not self.root.is_absolute() or self.root == Path("/"):
            raise ValueError("Archive root must be a dedicated absolute directory")
        if self.environment not in {"dev", "prod"} or not re.fullmatch(r"[a-z0-9-]{3,63}", self.bucket):
            raise ValueError("Invalid archive environment or bucket")
        if not re.fullmatch(r"[a-z0-9-]+", self.region):
            raise ValueError("Invalid OSS region")
        if self.enabled and not self.unlimited:
            raise ValueError(
                "Finite cloud quotas require a billing gate; set explicit unlimited=true only with authorization"
            )
        if (
            min(self.cache_bytes, self.spool_bytes) <= 0
            or self.min_free_bytes < 0
            or not 0 <= self.min_free_fraction < 1
        ):
            raise ValueError("Invalid local storage limits")

    @property
    def prefix(self):
        return f"data-platform/{self.environment}/"

    @classmethod
    def from_file(cls, path: Path):
        values = json.loads(path.read_text())
        values["root"] = Path(values["root"])
        return cls(**values)


@dataclass(frozen=True)
class ArtifactRef:
    bucket: str
    key: str
    sha256: str
    size: int
    stored_sha256: str
    stored_size: int
    compression: str = "gzip"
    schema_version: int = 1

    @classmethod
    def parse(cls, value):
        ref = cls(**value)
        if ref.schema_version != 1 or ref.compression != "gzip" or min(ref.size, ref.stored_size) < 0:
            raise ValueError("Unsupported artifact reference")
        if not all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in (ref.sha256, ref.stored_sha256)):
            raise ValueError("Invalid artifact checksum")
        return ref


def digest_file(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.chmod(0o600)
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextlib.contextmanager
def staging_directory(store, kind):
    """One bounded staging operation per kind; reclaim files left by a killed process."""
    if kind not in {"backup", "migration"}:
        raise ValueError("Unknown staging kind")
    with (store.root / f".{kind}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for old in store.root.glob(f"{kind}-*"):
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old)
        with tempfile.TemporaryDirectory(prefix=f"{kind}-", dir=store.root) as directory:
            yield Path(directory)


def service_status(error):
    for _ in range(5):
        if error is None:
            return None
        if getattr(error, "status_code", None):
            return error.status_code
        error = error.__cause__ or error.__context__ or getattr(error, "_error", None)
    return None


class OssTransport:
    """Official SDK with service-side credentials and no credential-bearing error output."""

    def __init__(self, config):
        import alibabacloud_oss_v2 as oss

        self.oss = oss
        cfg = oss.config.load_default()
        cfg.region = config.region
        credentials_path = os.environ.get("DATA_PLATFORM_OSS_CREDENTIALS_FILE")
        if credentials_path:
            import configparser

            path = Path(credentials_path)
            if path.is_symlink() or path.stat().st_mode & 0o027:
                raise ValueError("OSS credentials file must be private and not writable by its group")
            values = configparser.ConfigParser()
            values.read(path)
            section = values["default"] if values.has_section("default") else values["Credentials"]
            cfg.credentials_provider = oss.credentials.StaticCredentialsProvider(
                section["accesskeyid"], section["accesskeysecret"], section.get("securitytoken")
            )
        else:
            cfg.credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        cfg.connect_timeout = 5
        cfg.readwrite_timeout = 30
        cfg.retry_max_attempts = 3
        self.client = oss.Client(cfg)
        self.bucket = config.bucket
        self.prefix = config.prefix

    def exists(self, key):
        try:
            self.client.head_object(self.oss.HeadObjectRequest(bucket=self.bucket, key=key))
            return True
        except Exception as exc:
            if service_status(exc) == 404:
                return False
            raise ArtifactUnavailableError("OSS metadata request failed") from None

    def put(self, key, path):
        try:
            with Path(path).open("rb") as stream:
                self.client.put_object(
                    self.oss.PutObjectRequest(
                        bucket=self.bucket,
                        key=key,
                        body=stream,
                        content_length=Path(path).stat().st_size,
                        content_type="application/gzip",
                        forbid_overwrite=True,
                        acl="private",
                        server_side_encryption="AES256",
                    )
                )
        except Exception as exc:
            # Another writer may have published the same content. The caller verifies bytes next.
            if service_status(exc) != 409:
                raise ArtifactUnavailableError("OSS upload failed") from None

    def download(self, key, path, expected_size):
        try:
            result = self.client.get_object(self.oss.GetObjectRequest(bucket=self.bucket, key=key))
            with contextlib.closing(result.body), Path(path).open("wb") as stream:
                count = 0
                for block in result.body.iter_bytes():
                    count += len(block)
                    if count > expected_size:
                        raise ValueError("Object exceeds declared size")
                    stream.write(block)
                if count != expected_size:
                    raise ValueError("Object length mismatch")
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise ArtifactUnavailableError("OSS download or length verification failed") from None

    def delete(self, key):
        """Delete expired owned evidence including versions; callers must check protected references."""
        if not key.startswith(self.prefix) or "/reports/" in key:
            raise ValueError("Deletion is restricted to owned expendable evidence")
        try:
            marker, version_marker = None, None
            for _ in range(20):
                result = self.client.list_object_versions(
                    self.oss.ListObjectVersionsRequest(
                        bucket=self.bucket,
                        prefix=key,
                        key_marker=marker,
                        version_id_marker=version_marker,
                        max_keys=1000,
                    )
                )
                for version in [*(result.version or []), *(result.delete_marker or [])]:
                    if version.key == key:
                        self.client.delete_object(
                            self.oss.DeleteObjectRequest(
                                bucket=self.bucket, key=key, version_id=version.version_id
                            )
                        )
                if not result.is_truncated:
                    return
                marker, version_marker = result.next_key_marker, result.next_version_id_marker
            raise ArtifactUnavailableError("Version cleanup batch exhausted; retry cleanup")
        except Exception:
            raise ArtifactUnavailableError("OSS version deletion failed; retain receipt for retry") from None

    def abort_expired_uploads(self, *, now=None):
        now = time.time() if now is None else now
        count = 0
        try:
            result = self.client.list_multipart_uploads(
                self.oss.ListMultipartUploadsRequest(bucket=self.bucket, prefix=self.prefix, max_uploads=1000)
            )
            for upload in result.uploads or []:
                if upload.key.startswith(self.prefix) and now - upload.initiated.timestamp() > 7 * 86400:
                    self.client.abort_multipart_upload(
                        self.oss.AbortMultipartUploadRequest(
                            bucket=self.bucket, key=upload.key, upload_id=upload.upload_id
                        )
                    )
                    count += 1
            return count
        except Exception:
            raise ArtifactUnavailableError("OSS multipart cleanup failed") from None


class ArtifactStore:
    def __init__(self, config: ArchiveConfig, transport=None):
        self.config = config
        self.transport = transport
        self.root = config.root
        for name in ("pending", "cache", "receipts", "migrations"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)

    def remote(self):
        if not self.config.enabled:
            raise ArtifactUnavailableError("OSS archive is disabled")
        if self.transport is None:
            self.transport = OssTransport(self.config)
        return self.transport

    @contextlib.contextmanager
    def locked(self):
        with (self.root / ".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def check_space(self, additional=0):
        usage = shutil.disk_usage(self.root)
        if usage.free - additional < max(
            self.config.min_free_bytes, usage.total * self.config.min_free_fraction
        ):
            raise ArchiveCapacityError("Insufficient disk headroom; archiving paused")

    def validate_ref(self, ref):
        ref = ArtifactRef.parse(asdict(ref))
        expected = self.config.prefix
        if ref.bucket != self.config.bucket or not ref.key.startswith(expected) or ".." in ref.key.split("/"):
            raise ValueError("Artifact is outside this environment")
        suffix = ref.key[len(expected) :].split("/")
        if len(suffix) != 2 or suffix[0] not in CATEGORIES or suffix[1] != ref.sha256 + ".gz":
            raise ValueError("Invalid owned artifact key")

    def enqueue(self, source: Path, category: str, *, completed_backup=False):
        if not self.config.enabled:
            raise ArtifactUnavailableError("OSS archive is disabled")
        if category not in CATEGORIES or (category == "backups" and not completed_backup):
            raise ValueError("Only completed backup exports may be archived")
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError("Archive input must be a regular file")
        with source.open("rb") as stream:
            if stream.read(16) == b"SQLite format 3\x00" or source.suffix.lower() in {".ibd", ".ibdata"}:
                raise ValueError("Archive a completed backup export, not a live database file")
        initial = source.stat()
        with self.locked():
            self.check_space()
            pending = self.root / "pending"
            for stale in pending.glob("enqueue-*.partial"):
                stale.unlink()
            for stale in pending.glob("*.gz"):
                if not stale.with_suffix(".json").exists():
                    stale.unlink()
            used = sum(p.stat().st_size for p in pending.iterdir() if p.is_file())
            remaining = self.config.spool_bytes - used
            original = hashlib.sha256()
            size = 0
            with tempfile.NamedTemporaryFile(
                dir=pending, prefix="enqueue-", suffix=".partial", delete=False
            ) as output:
                temporary = Path(output.name)
                try:
                    with (
                        source.open("rb") as stream,
                        gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed,
                    ):
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            original.update(block)
                            size += len(block)
                            compressed.write(block)
                            compressed.flush()
                            self.check_space()
                            if output.tell() > remaining - 8192:
                                raise ArchiveCapacityError("Pending upload capacity reached")
                    output.flush()
                    os.fsync(output.fileno())
                    current = source.stat()
                    if (initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
                        current.st_ino,
                        current.st_size,
                        current.st_mtime_ns,
                    ):
                        raise ValueError("Archive source changed while being read")
                    ref = ArtifactRef(
                        self.config.bucket,
                        f"{self.config.prefix}{category}/{original.hexdigest()}.gz",
                        original.hexdigest(),
                        size,
                        digest_file(temporary),
                        temporary.stat().st_size,
                    )
                    identifier = hashlib.sha256(ref.key.encode()).hexdigest()
                    destination = pending / (identifier + ".gz")
                    temporary.replace(destination)
                    atomic_json(
                        pending / (identifier + ".json"), {"ref": asdict(ref), "created_at": time.time()}
                    )
                    return ref
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise

    def _verify(self, path, ref):
        if path.stat().st_size != ref.stored_size or digest_file(path) != ref.stored_sha256:
            raise ArtifactUnavailableError("Archived object checksum mismatch")
        sha, size = hashlib.sha256(), 0
        try:
            with gzip.open(path, "rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(block)
                    if size > ref.size:
                        raise ValueError()
                    sha.update(block)
        except (OSError, EOFError, ValueError):
            raise ArtifactUnavailableError("Invalid compressed artifact") from None
        if size != ref.size or sha.hexdigest() != ref.sha256:
            raise ArtifactUnavailableError("Artifact content checksum mismatch")

    def flush(self, *, limit=20):
        completed = []
        with self.locked():
            for stale in self.root.glob("verify-*.partial"):
                stale.unlink()
            for manifest in sorted((self.root / "pending").glob("*.json"))[:limit]:
                self.check_space()
                record = json.loads(manifest.read_text())
                ref = ArtifactRef.parse(record["ref"])
                self.validate_ref(ref)
                path = manifest.with_suffix(".gz")
                self._verify(path, ref)
                remote = self.remote()
                if not remote.exists(ref.key):
                    remote.put(ref.key, path)
                self.check_space(ref.stored_size)
                with tempfile.NamedTemporaryFile(
                    dir=self.root, prefix="verify-", suffix=".partial", delete=False
                ) as stream:
                    check = Path(stream.name)
                try:
                    remote.download(ref.key, check, ref.stored_size)
                    self._verify(check, ref)
                finally:
                    check.unlink(missing_ok=True)
                atomic_json(self.root / "receipts" / manifest.name, {**record, "verified_at": time.time()})
                path.unlink()
                manifest.unlink()
                completed.append(ref)
        return completed

    def verified(self, ref):
        self.validate_ref(ref)
        identifier = hashlib.sha256(ref.key.encode()).hexdigest()
        receipt = self.root / "receipts" / (identifier + ".json")
        return receipt.is_file() and json.loads(receipt.read_text()).get("ref") == asdict(ref)

    def read_json(self, ref, *, max_bytes=64 * 1024**2):
        self.validate_ref(ref)
        if ref.size > max_bytes or ref.stored_size > self.config.cache_bytes:
            raise ArtifactUnavailableError("Report exceeds interactive read limit")
        with self.locked():
            for stale in (self.root / "cache").glob("*.partial"):
                stale.unlink()
            path = self.root / "cache" / (hashlib.sha256(ref.key.encode()).hexdigest() + ".gz")
            if path.exists():
                try:
                    self._verify(path, ref)
                except ArtifactUnavailableError:
                    path.unlink()
            if not path.exists():
                files = sorted(
                    (p for p in path.parent.glob("*.gz") if p.is_file()), key=lambda p: p.stat().st_mtime
                )
                used = sum(p.stat().st_size for p in files)
                for old in files:
                    if used + ref.stored_size <= self.config.cache_bytes:
                        break
                    used -= old.stat().st_size
                    old.unlink()
                self.check_space(ref.stored_size)
                temporary = path.with_suffix(".partial")
                try:
                    self.remote().download(ref.key, temporary, ref.stored_size)
                    self._verify(temporary, ref)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            path.touch()
            try:
                with gzip.open(path, "rt") as stream:
                    return json.load(stream)
            except (ValueError, UnicodeError):
                raise ArtifactUnavailableError("Archived report is not valid JSON") from None

    def cleanup(self, *, protected_keys, now=None):
        """Only owned expendable objects; reports are never deleted automatically."""
        now = time.time() if now is None else now
        with self.locked():
            records = [(p, json.loads(p.read_text())) for p in (self.root / "receipts").glob("*.json")]
            backups = sorted(
                (r for _, r in records if "/backups/" in r["ref"]["key"]),
                key=lambda r: r["created_at"],
                reverse=True,
            )
            protected = set(protected_keys) | {r["ref"]["key"] for r in backups[:3]}
            deleted = 0
            for path, record in records:
                ref = ArtifactRef.parse(record["ref"])
                self.validate_ref(ref)
                category = ref.key.split("/")[-2]
                days = {"acceptance-success": 30, "acceptance-failure": 90, "backups": 30}.get(category)
                if days is None or ref.key in protected or now - record["created_at"] < days * 86400:
                    continue
                self.remote().delete(ref.key)
                path.unlink()
                deleted += 1
            return deleted


def configured_store():
    path = os.environ.get("DATA_PLATFORM_ARCHIVE_CONFIG")
    if not path:
        raise ArtifactUnavailableError("OSS archive is not configured")
    return ArtifactStore(ArchiveConfig.from_file(Path(path)))

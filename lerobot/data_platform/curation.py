"""Location-independent Curation targets, snapshots, and immutable result bundles."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from lerobot.data_platform.execution import atomic_json
from lerobot.data_platform.lifecycle import DatasetReplica, DatasetVersion, LifecycleStore
from lerobot.data_platform.precompute.dataset_io import load_episode_records, load_task_records
from lerobot.data_platform.precompute.preprocess.common import load_json
from lerobot.data_platform.task_catalog import TaskConfigSnapshot, content_digest

CURATION_PROTOCOL = 1
OPERATIONS = frozenset(
    {
        "curation.snapshot",
        "curation.validate_source",
        "curation.quality",
        "curation.stage",
        "curation.labeling",
        "curation.tagging",
        "curation.embedding",
        "curation.project",
        "curation.compare_summary",
        "curation.construction",
        "curation.construction_preview",
        "curation.materialize",
    }
)
DATASET_OPERATIONS = {"curation.construction", "curation.materialize"}


@dataclass(frozen=True)
class CurationTarget:
    dataset_key: str
    location_id: str | None = None
    dataset_version_id: str | None = None

    @classmethod
    def from_dict(cls, payload: dict) -> CurationTarget:
        if not isinstance(payload, dict):
            raise ValueError("target must be an object")
        key = str(payload.get("dataset_key") or "").strip()
        location = str(payload.get("location_id") or "").strip() or None
        version = str(payload.get("dataset_version_id") or "").strip() or None
        if not key and not location:
            raise ValueError("dataset_key or location_id is required")
        return cls(key, location, version)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CurationSnapshot:
    version: dict
    identity: dict
    content: dict
    info: dict
    episodes: list[dict]
    tasks: list[dict]
    task_config: dict

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> CurationSnapshot:
        value = cls(**{field: payload[field] for field in cls.__dataclass_fields__})
        version = DatasetVersion.from_dict(value.version)
        artifact = dict(value.identity)
        digest = artifact.pop("artifact_digest", None)
        if content_digest(artifact) != digest or digest != version.identity_artifact_digest:
            raise ValueError("Invalid snapshot identity digest")
        if (
            value.identity["dataset_version_id"] != version.version_id
            or value.identity["dataset_id"] != version.dataset_id
            or value.identity["dataset_fingerprint"] != version.fingerprint
            or value.content["dataset_fingerprint"] != version.fingerprint
            or value.info.get("features", {}) != version.schema
        ):
            raise ValueError("Snapshot identity does not match dataset version")
        content = dict(value.content)
        fingerprint = content.pop("dataset_fingerprint", None)
        if content_digest(content) != fingerprint:
            raise ValueError("Snapshot content manifest checksum mismatch")
        refs = version.uid_by_index()
        indices = [int(row["episode_index"]) for row in value.episodes]
        identity_refs = {int(row["episode_index"]): row for row in value.identity["episodes"]}
        if (
            len(indices) != len(set(indices))
            or set(indices) != set(refs)
            or len(set(refs.values())) != len(refs)
            or set(identity_refs) != set(refs)
            or int(value.info["total_episodes"]) != len(refs)
        ):
            raise ValueError("Snapshot episodes do not match dataset version")
        for ref in version.episode_refs:
            identity = identity_refs[int(ref["episode_index"])]
            if (
                identity["episode_uid"] != ref["episode_uid"]
                or identity["content_fingerprint"] != ref["fingerprint"]
            ):
                raise ValueError("Snapshot episode identity mismatch")
        TaskConfigSnapshot.from_dict(value.task_config)
        return value


def snapshot_from_version(store: LifecycleStore, version: DatasetVersion, root: Path, task_config=None):
    from lerobot.data_platform.precompute.data_profile import resolve_data_profile

    info = load_json(root / "meta" / "info.json")
    info["data_profile"] = resolve_data_profile(root).to_dict()
    return CurationSnapshot(
        version.to_dict(),
        store._load_identity_artifact(Path(version.identity_artifact_uri)).to_dict(),
        load_json(store._content_manifest_path(version.fingerprint)),
        info,
        load_episode_records(root),
        load_task_records(root),
        task_config or store.tasks.snapshot(version.dataset_key).to_dict(),
    )


def scan_snapshot(root: Path, dataset_key: str, dataset_id: str, *, previous=None, task_config=None):
    """Use the existing full validator in a disposable ledger, never a second source of truth."""
    from lerobot.data_platform.lifecycle import dataset_snapshot

    fingerprint, _ = dataset_snapshot(root)
    episodes = load_episode_records(root)
    identity = (
        previous["identity"] if previous and previous["version"]["fingerprint"] == fingerprint else None
    )
    uids = {
        int(row["episode_index"]): uuid.uuid5(
            uuid.NAMESPACE_URL, f"{dataset_id}:{fingerprint}:{int(row['episode_index'])}"
        ).hex
        for row in episodes
    }
    with tempfile.TemporaryDirectory(prefix="curation-scan-") as temporary:
        store = LifecycleStore(Path(temporary))
        version = store.ingest(
            root, dataset_key, dataset_id=dataset_id, episode_uid_by_index=uids, identity_artifact=identity
        )
        return snapshot_from_version(store, version, root, task_config)


def register_snapshot(store: LifecycleStore, snapshot: CurationSnapshot, location: dict, *, connection=None):
    """Register authenticated Agent metadata without resolving its paths on Server A."""
    value = CurationSnapshot.from_dict(snapshot.to_dict())
    if connection is None:
        with store.repository.transaction() as active:
            return register_snapshot(store, value, location, connection=active)
    artifact = store._persist_identity_artifact(value.identity, connection=connection)
    payload = {
        **value.version,
        "root": location["root"],
        "identity_artifact_uri": str(store._identity_path(artifact.artifact_digest)),
    }
    version = DatasetVersion.from_dict(payload)
    existing = store._get_record("versions", version.version_id, connection=connection)
    if existing:
        if existing["fingerprint"] != version.fingerprint or existing["episode_refs"] != version.episode_refs:
            raise ValueError("Version identity collision")
        version = DatasetVersion.from_dict(existing)
    else:
        store._put_record(
            "versions", version.version_id, version.to_dict(), immutable=True, connection=connection
        )
        parents = [
            DatasetVersion.from_dict(store._get_record("versions", parent, connection=connection))
            for parent in version.parent_version_ids
        ]
        store._record_automatic_reconciliation(
            parents, version, operation=version.operation, connection=connection
        )
    snapshot_id = version.version_id
    previous = store._get_record("curation_snapshots", snapshot_id, connection=connection)
    if previous is None:
        store._put_record(
            "curation_snapshots", snapshot_id, value.to_dict(), immutable=True, connection=connection
        )
    atomic_json(store._content_manifest_path(version.fingerprint), value.content)
    if location["location_id"].startswith("pending-"):
        # The completion outbox retains this version until Control Plane allocates
        # the real location. A placeholder must not become an advertised replica.
        return version
    replica_id = (
        "rep_" + content_digest([version.version_id, location["node_id"], location["location_id"]])[:24]
    )
    if store._get_record("replicas", replica_id, connection=connection) is None:
        replica = DatasetReplica(
            replica_id,
            version.version_id,
            location["root"],
            version.fingerprint,
            "available",
            version.created_at,
            dataset_key=location["dataset_key"],
            node_id=location["node_id"],
            location_id=location["location_id"],
        )
        store._put_record("replicas", replica_id, replica.to_dict(), immutable=True, connection=connection)
    store._put_record(
        "curation_locations",
        location["location_id"],
        {"location_id": location["location_id"], "dataset_version_id": version.version_id},
        connection=connection,
    )
    return version


def artifact_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError("Invalid artifact path")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact escapes result directory")
    return path


def bundle_manifest(root: Path, *, operation: str, target: dict) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Curation artifacts cannot contain symlinks")
        if path.is_file() and path.name != "curation-manifest.json":
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            files[path.relative_to(root).as_posix()] = {
                "sha256": digest.hexdigest(),
                "size": path.stat().st_size,
            }
    result = {"protocol": CURATION_PROTOCOL, "operation": operation, "target": target, "files": files}
    atomic_json(root / "curation-manifest.json", result)
    return result


def validate_bundle(root: Path, operation: str, target: dict) -> dict:
    manifest = load_json(root / "curation-manifest.json")
    if (
        manifest.get("protocol") != CURATION_PROTOCOL
        or manifest.get("operation") != operation
        or manifest.get("target") != target
    ):
        raise ValueError("Result bundle belongs to a different request")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "curation-manifest.json"
    }
    if actual != set(manifest["files"]):
        raise ValueError("Incomplete Curation result bundle")
    for name, expected in manifest["files"].items():
        path = artifact_path(root, name)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected["size"]:
            raise ValueError("Invalid Curation artifact")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected["sha256"]:
            raise ValueError("Curation artifact checksum mismatch")
    return manifest


def publish_bundle(staged: Path, destination: Path, operation: str, target: dict) -> dict:
    manifest = validate_bundle(staged, operation, target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if validate_bundle(destination, operation, target) != manifest:
            raise ValueError("Conflicting Curation result publication")
        return manifest
    temporary = Path(tempfile.mkdtemp(prefix=".publishing-", dir=destination.parent))
    try:
        shutil.copytree(staged, temporary, dirs_exist_ok=True)
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest

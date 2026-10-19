"""Explicit, bounded archive maintenance. No changes to running task publication."""

from __future__ import annotations

import argparse
import json
import tarfile
import tempfile
from dataclasses import asdict
from pathlib import Path

from lerobot.data_platform.artifact_store import ArchiveConfig, ArtifactStore, digest_file


def enqueue_backup(store, directory):
    directory = Path(directory).resolve()
    receipt = json.loads((directory / "backup.json").read_text())
    if (
        not receipt.get("complete")
        or not receipt.get("quiescent")
        or receipt.get("environment") != store.config.environment
    ):
        raise ValueError("A completed quiescent backup of this environment is required")
    # Deliberately exclude server.env and management credentials from an offsite database backup.
    files = {
        name: sha
        for name, sha in receipt["files"].items()
        if name in {"control.sql", "logs.sql"} or (name.startswith("state/") and name.endswith(".db"))
    }
    if not {"control.sql", "logs.sql"} <= files.keys():
        raise ValueError("Backup database exports are missing")
    size = 0
    for name, sha in files.items():
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory) or digest_file(path) != sha:
            raise ValueError("Backup inventory or checksum mismatch")
        size += path.stat().st_size
    store.check_space(size)
    if size > store.config.spool_bytes:
        raise ValueError("Backup exceeds the configured staging capacity")
    with tempfile.TemporaryDirectory(dir=store.root) as temporary:
        path = Path(temporary) / "databases.tar"
        with tarfile.open(path, "w") as archive:
            for name in files:
                archive.add(directory / name, arcname=name, recursive=False)
        return store.enqueue(path, "backups", completed_backup=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("file", type=Path)
    enqueue.add_argument(
        "--category", choices=["reports", "acceptance-success", "acceptance-failure"], required=True
    )
    backup = sub.add_parser("backup")
    backup.add_argument("directory", type=Path)
    sub.add_parser("flush")
    sub.add_parser("probe")
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument(
        "--protected-keys",
        type=Path,
        required=True,
        help="JSON list from the current and rollback release evidence inventories",
    )
    args = parser.parse_args()
    store = ArtifactStore(ArchiveConfig.from_file(args.config))
    if args.command == "enqueue":
        print(json.dumps(asdict(store.enqueue(args.file, args.category))))
    elif args.command == "backup":
        print(json.dumps(asdict(enqueue_backup(store, args.directory))))
    elif args.command == "flush":
        print(json.dumps({"uploaded": len(store.flush())}))
    elif args.command == "cleanup":
        keys = json.loads(args.protected_keys.read_text())
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise ValueError("Expected a JSON array of protected object keys")
        print(json.dumps({"deleted": store.cleanup(protected_keys=keys)}))
    else:
        transport = store.remote()
        oss = transport.oss
        acl = transport.client.get_bucket_acl(oss.GetBucketAclRequest(bucket=store.config.bucket)).acl
        version = transport.client.get_bucket_versioning(
            oss.GetBucketVersioningRequest(bucket=store.config.bucket)
        ).version_status
        rules = transport.client.get_bucket_lifecycle(
            oss.GetBucketLifecycleRequest(bucket=store.config.bucket)
        ).lifecycle_configuration.rules
        print(
            json.dumps(
                {
                    "bucket": store.config.bucket,
                    "region": store.config.region,
                    "acl": acl,
                    "versioning": version,
                    "lifecycle_rules": [{"id": r.id, "prefix": r.prefix, "status": r.status} for r in rules],
                }
            )
        )


if __name__ == "__main__":
    main()

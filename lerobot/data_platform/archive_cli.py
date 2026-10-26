"""Explicit, bounded archive maintenance. No changes to running task publication."""

from __future__ import annotations

import argparse
import json
import tarfile
from dataclasses import asdict
from pathlib import Path

from lerobot.data_platform.artifact_store import (
    ArchiveConfig,
    ArtifactStore,
    ArtifactUnavailableError,
    OssTransport,
    digest_file,
    staging_directory,
)


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
    with staging_directory(store, "backup") as temporary:
        path = Path(temporary) / "databases.tar"
        with tarfile.open(path, "w") as archive:
            for name in files:
                archive.add(directory / name, arcname=name, recursive=False)
        return store.enqueue(path, "backups", completed_backup=True)


def protected_release_keys(store):
    """Protect evidence from every retained release, including current and rollback packages."""
    from lerobot.data_platform.releases import RELEASE_ROOT

    if not RELEASE_ROOT.is_dir():
        raise ValueError("Release inventory is unavailable; cleanup cannot establish reference protection")
    keys = set()

    def visit(value):
        if isinstance(value, dict):
            if (
                value.get("bucket") == store.config.bucket
                and isinstance(value.get("key"), str)
                and value["key"].startswith(store.config.prefix)
            ):
                keys.add(value["key"])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for release in RELEASE_ROOT.iterdir():
        if not release.is_dir() or release.is_symlink():
            continue
        for name in ("approval.json", "acceptance.json", "operator-matrix.json"):
            path = release / name
            if path.exists():
                if path.is_symlink() or path.stat().st_size > 2 * 1024**2:
                    raise ValueError("Release evidence inventory requires inspection before cleanup")
                visit(json.loads(path.read_text()))
    return keys


def probe(store):
    try:
        transport = OssTransport(store.config)
        oss = transport.oss
        acl = transport.client.get_bucket_acl(oss.GetBucketAclRequest(bucket=store.config.bucket)).acl
        version = transport.client.get_bucket_versioning(
            oss.GetBucketVersioningRequest(bucket=store.config.bucket)
        ).version_status
        rules = transport.client.get_bucket_lifecycle(
            oss.GetBucketLifecycleRequest(bucket=store.config.bucket)
        ).lifecycle_configuration.rules
        versions = transport.client.list_object_versions(
            oss.ListObjectVersionsRequest(
                bucket=store.config.bucket, prefix=store.config.prefix, max_keys=1000
            )
        )
        return {
            "bucket": store.config.bucket,
            "region": store.config.region,
            "acl": acl,
            "versioning": version,
            "lifecycle_rules": [{"id": r.id, "prefix": r.prefix, "status": r.status} for r in rules],
            "owned_version_bytes": sum(item.size or 0 for item in versions.version or []),
            "owned_versions_truncated": bool(versions.is_truncated),
            "local_bytes": {
                name: sum(p.stat().st_size for p in (store.root / name).glob("*") if p.is_file())
                for name in ("cache", "pending", "receipts", "migrations")
            },
        }
    except Exception:
        raise ArtifactUnavailableError(
            "OSS preflight failed; inspect region and server-side permissions"
        ) from None


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
    sub.add_parser("abort-expired-uploads")
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument(
        "--protected-keys",
        type=Path,
        required=True,
        help="JSON list from the current and rollback release evidence inventories",
    )
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--cursor", default="")
    migrate.add_argument("--records", type=int, default=20)
    migrate.add_argument("--bytes", type=int, default=64 * 1024**2)
    restore = sub.add_parser("restore")
    restore.add_argument("job_id")
    args = parser.parse_args()
    store = ArtifactStore(ArchiveConfig.from_file(args.config))
    if args.command in {"migrate", "restore"}:
        import os

        from lerobot.data_platform.control_plane import ControlPlaneStore
        from lerobot.data_platform.report_migration import ReportMigrator

        migration = ReportMigrator(ControlPlaneStore(os.environ["DATA_PLATFORM_DATABASE_URL"]), store)
        if args.command == "migrate":
            print(
                json.dumps(
                    asdict(migration.batch(cursor=args.cursor, records=args.records, byte_limit=args.bytes))
                )
            )
        else:
            print(json.dumps({"restored": migration.restore(args.job_id)}))
    elif args.command == "enqueue":
        print(json.dumps(asdict(store.enqueue(args.file, args.category))))
    elif args.command == "backup":
        print(json.dumps(asdict(enqueue_backup(store, args.directory))))
    elif args.command == "flush":
        print(json.dumps({"uploaded": len(store.flush())}))
    elif args.command == "abort-expired-uploads":
        print(json.dumps({"aborted": store.remote().abort_expired_uploads()}))
    elif args.command == "cleanup":
        keys = json.loads(args.protected_keys.read_text())
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise ValueError("Expected a JSON array of protected object keys")
        print(
            json.dumps({"deleted": store.cleanup(protected_keys=set(keys) | protected_release_keys(store))})
        )
    else:
        print(json.dumps(probe(store)))


if __name__ == "__main__":
    main()

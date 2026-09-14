"""Private deployment backups; database credentials never appear in subprocess arguments."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from lerobot.data_platform.releases import atomic_json, digest, run


def mysql_command(url: str, executable: str, *, output=None, source=None):
    parsed = make_url(url)
    if parsed.get_backend_name() != "mysql":
        raise RuntimeError("Deployment database backups require MySQL")

    def quote(value):
        return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    with tempfile.TemporaryDirectory(prefix="data-platform-db-") as temporary:
        config = Path(temporary) / "client.cnf"
        config.write_text(
            "[client]\n"
            + "\n".join(
                f"{key}={quote(value)}"
                for key, value in {
                    "user": parsed.username,
                    "password": parsed.password,
                    "host": parsed.host or "localhost",
                    "port": parsed.port or 3306,
                }.items()
            )
            + "\n"
        )
        config.chmod(0o600)
        args = [executable, f"--defaults-extra-file={config}"]
        if executable == "mysqldump":
            args += ["--single-transaction", "--skip-lock-tables", "--no-tablespaces", "--hex-blob"]
        args.append(parsed.database)
        run(args, stdout=output, stdin=source)


def backup(deployment, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    for key, name in (
        ("DATA_PLATFORM_DATABASE_URL", "control.sql"),
        ("DATA_PLATFORM_LOG_DATABASE_URL", "logs.sql"),
    ):
        with (target / name).open("wb") as stream:
            mysql_command(os.environ[key], "mysqldump", output=stream)
    shutil.copy2(deployment.config, target / "server.env")
    output = Path(os.environ["DATA_PLATFORM_OUTPUT_DIR"])
    remote = Path(os.environ["DATA_PLATFORM_REMOTE_CACHE_ROOT"])
    paths = [
        output / "static/lifecycle",
        output / "static/datasets_registry.json",
        output / "management",
        remote.parent / "management",
    ]
    inventory = {}
    for number, path in enumerate(dict.fromkeys(paths)):
        if not path.exists():
            continue
        destination = target / "state" / str(number)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(
                path,
                destination,
                symlinks=True,
                ignore=shutil.ignore_patterns("*.db", "*.db-wal", "*.db-shm"),
            )
            for database in path.rglob("*.db"):
                saved = destination / database.relative_to(path)
                saved.parent.mkdir(parents=True, exist_ok=True)
                with (
                    sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source,
                    sqlite3.connect(saved) as sink,
                ):
                    source.backup(sink)
        else:
            shutil.copy2(path, destination)
        inventory[str(number)] = str(path)
    atomic_json(
        target / "backup.json",
        {
            "environment": deployment.environment,
            "instance_id": os.environ["DATA_PLATFORM_INSTANCE_ID"],
            "state_paths": inventory,
            "files": {
                str(path.relative_to(target)): digest(path) for path in target.rglob("*") if path.is_file()
            },
            "complete": True,
        },
    )


def validate_backup(deployment, target: Path) -> dict:
    value = json.loads((target / "backup.json").read_text())
    if not value.get("complete") or (value["environment"], value["instance_id"]) != (
        deployment.environment,
        os.environ["DATA_PLATFORM_INSTANCE_ID"],
    ):
        raise RuntimeError("Backup belongs to a different environment or is incomplete")
    if not {"control.sql", "logs.sql", "server.env"} <= value.get("files", {}).keys():
        raise RuntimeError("Backup is missing its checksums")
    for name, expected in value["files"].items():
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or (target / path).is_symlink()
            or digest(target / path) != expected
        ):
            raise RuntimeError("Backup checksum mismatch; no restore was started")
    return value


def restore_backup(deployment, target: Path):
    """Explicit destructive recovery, only into the same stopped environment and its known state paths."""
    from sqlalchemy import create_engine, inspect

    value = validate_backup(deployment, target)
    output = Path(os.environ["DATA_PLATFORM_OUTPUT_DIR"])
    remote = Path(os.environ["DATA_PLATFORM_REMOTE_CACHE_ROOT"])
    allowed = {
        str(path)
        for path in (
            output / "static/lifecycle",
            output / "static/datasets_registry.json",
            output / "management",
            remote.parent / "management",
        )
    }
    if not set(value["state_paths"].values()) <= allowed:
        raise RuntimeError("Backup contains state paths outside the configured environment")
    for key, name in (
        ("DATA_PLATFORM_DATABASE_URL", "control.sql"),
        ("DATA_PLATFORM_LOG_DATABASE_URL", "logs.sql"),
    ):
        engine = create_engine(os.environ[key])
        try:
            tables = inspect(engine).get_table_names()
            if any(not name.startswith("dp_") for name in tables):
                raise RuntimeError("Refuse to restore a database containing unrelated tables")
            with engine.connect() as connection:
                connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
                try:
                    for table in tables:
                        connection.exec_driver_sql(
                            "DROP TABLE " + engine.dialect.identifier_preparer.quote(table)
                        )
                finally:
                    connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
                    connection.commit()
        finally:
            engine.dispose()
        with (target / name).open("rb") as stream:
            mysql_command(os.environ[key], "mysql", source=stream)
    import pwd

    account = pwd.getpwnam(deployment.user)
    for number, path in value["state_paths"].items():
        destination = Path(path)
        source = target / "state" / number
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink(missing_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
            copied = [destination, *destination.rglob("*")]
        else:
            shutil.copy2(source, destination)
            copied = [destination]
        for entry in copied:
            os.chown(entry, account.pw_uid, account.pw_gid, follow_symlinks=False)

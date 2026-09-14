"""Explicit deployment identity and pre-migration isolation checks."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, String, Table, inspect, select

_metadata = MetaData()
_identity = Table(
    "dp_environment_identity",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("environment", String(8), nullable=False),
    Column("instance_id", String(36), nullable=False),
    Column("kind", String(16), nullable=False),
)


@dataclass(frozen=True)
class EnvironmentIdentity:
    name: str
    instance_id: str

    @classmethod
    def from_env(cls) -> EnvironmentIdentity | None:
        name = os.environ.get("DATA_PLATFORM_ENV", "").strip()
        switch = os.environ.get("DATA_PLATFORM_ENABLE_DEV_ROLE_SWITCH", "0") == "1"
        if switch and name != "dev":
            raise RuntimeError("Role switching is only available in the dev environment")
        if not name:
            return None  # Existing single-user/legacy deployments remain compatible.
        if name not in {"dev", "prod"}:
            raise RuntimeError("DATA_PLATFORM_ENV must be dev or prod")
        try:
            instance = str(uuid.UUID(os.environ.get("DATA_PLATFORM_INSTANCE_ID", "")))
        except ValueError:
            raise RuntimeError("DATA_PLATFORM_INSTANCE_ID must be a UUID") from None
        return cls(name, instance)

    def record(self, kind: str) -> dict:
        return {"environment": self.name, "instance_id": self.instance_id, "kind": kind}


def session_cookie_name() -> str:
    identity = EnvironmentIdentity.from_env()
    return f"data_platform_session_{identity.name}" if identity else "data_platform_session"


def verify_database_environment(engine, kind: str, *, initialize: bool = False) -> None:
    """Never silently adopt a database before ORM constructors perform schema writes."""
    identity = EnvironmentIdentity.from_env()
    if identity is None:
        # A legacy process must not open a database already assigned to an environment.
        if inspect(engine).has_table(_identity.name):
            raise RuntimeError("Database requires explicit environment configuration")
        return
    exists = inspect(engine).has_table(_identity.name)
    if not exists and not initialize:
        raise RuntimeError("Database environment is not initialized; run environment init explicitly")
    if not exists:
        if identity.name == "dev" and inspect(engine).get_table_names():
            raise RuntimeError("Initialize development using empty databases; do not clone production state")
        _metadata.create_all(engine)
    with engine.begin() as connection:
        row = connection.execute(select(_identity)).mappings().first()
        expected = identity.record(kind)
        if row is None and initialize:
            connection.execute(_identity.insert().values(id=1, **expected))
        elif row is None or any(row[key] != value for key, value in expected.items()):
            raise RuntimeError("Database environment identity mismatch")


def verify_directory(path: Path, kind: str, *, initialize: bool = False) -> None:
    identity = EnvironmentIdentity.from_env()
    if identity is None:
        return
    path = Path(path).expanduser()
    if not path.is_absolute() or path.resolve() != path:
        raise RuntimeError("Environment directories must be absolute, without symlink components")
    marker = path / ".data-platform-environment.json"
    expected = identity.record(kind)
    if marker.exists():
        if marker.is_symlink() or json.loads(marker.read_text()) != expected:
            raise RuntimeError("Directory environment identity mismatch")
    elif initialize:
        for parent in path.parents:
            ancestor = parent / ".data-platform-environment.json"
            if ancestor.exists():
                value = json.loads(ancestor.read_text())
                if (value.get("environment"), value.get("instance_id")) != (
                    identity.name,
                    identity.instance_id,
                ):
                    raise RuntimeError("Directory is nested inside another environment")
        path.mkdir(parents=True, exist_ok=True, mode=0o750)
        with marker.open("x") as stream:
            json.dump(expected, stream, sort_keys=True)
        marker.chmod(0o640)
    else:
        raise RuntimeError("Directory environment is not initialized")


def check_server_environment(*, initialize: bool = False) -> None:
    identity = EnvironmentIdentity.from_env()
    if identity is None:
        return
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    urls = [
        os.environ.get(key, "") for key in ("DATA_PLATFORM_DATABASE_URL", "DATA_PLATFORM_LOG_DATABASE_URL")
    ]
    if not all(urls):
        raise RuntimeError("Named environments require both control and log database URLs")
    parsed = [make_url(value) for value in urls]
    if all(value.get_backend_name() == "mysql" for value in parsed):
        if parsed[0].username == parsed[1].username:
            raise RuntimeError("Control and log databases require separate service accounts")
        if identity.name == "dev" and any(not (value.database or "").endswith("_dev") for value in parsed):
            raise RuntimeError("Development MySQL database names must end with _dev")
    if parsed[0].set(username=None, password=None) == parsed[1].set(username=None, password=None):
        raise RuntimeError("Control and log databases must be separate")
    # Compare actual endpoints as URL.set(None) does not clear an existing field.
    if (parsed[0].host, parsed[0].port, parsed[0].database) == (
        parsed[1].host,
        parsed[1].port,
        parsed[1].database,
    ):
        raise RuntimeError("Control and log databases must be separate")
    root = Path(os.environ.get("DATA_PLATFORM_STATE_ROOT", ""))
    if not root.is_absolute() or root.resolve() != root or root == Path("/"):
        raise RuntimeError("DATA_PLATFORM_STATE_ROOT must be an absolute dedicated directory")
    directories = []
    for key in ("DATA_PLATFORM_OUTPUT_DIR", "DATA_PLATFORM_REMOTE_CACHE_ROOT"):
        path = Path(os.environ.get(key, ""))
        if not path.is_absolute() or root not in path.parents:
            raise RuntimeError(f"{key} must be below DATA_PLATFORM_STATE_ROOT")
        directories.append(path)
    if directories[0] == directories[1] or any(
        left in right.parents for left, right in (directories, directories[::-1])
    ):
        raise RuntimeError("Console and remote cache directories must not overlap")
    # Development is only allowed to register and process samples below its own root.
    data_root = Path(os.environ.get("DATA_PLATFORM_ROOT", ""))
    if identity.name == "dev" and (not data_root.is_absolute() or root not in data_root.resolve().parents):
        raise RuntimeError("Development datasets must be below the development state root")
    for value, kind in zip(urls, ("control", "logs"), strict=True):
        engine = create_engine(value)
        try:
            verify_database_environment(engine, kind, initialize=initialize)
        finally:
            engine.dispose()
    verify_directory(root, "server", initialize=initialize)
    for path, kind in zip(directories, ("console", "remote-cache"), strict=True):
        verify_directory(path, kind, initialize=initialize)


def validate_dev_path(path: Path) -> None:
    identity = EnvironmentIdentity.from_env()
    if identity and identity.name == "dev":
        root = Path(os.environ["DATA_PLATFORM_STATE_ROOT"]).resolve()
        resolved = Path(path).expanduser().resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("Development paths must stay below the development state root")

"""Explicit initialization, maintenance and development test-user commands."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import time
from pathlib import Path

from lerobot.data_platform.deployment import Deployment, load_environment
from lerobot.data_platform.environment import check_server_environment


def initialize(deployment):
    from lerobot.data_platform.environment import verify_directory
    from lerobot.data_platform.releases import run

    if deployment.environment == "prod":
        import subprocess

        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "data-platform-web.service"], check=False
        )
        if active.returncode == 0:
            raise RuntimeError(
                "Use adopt-legacy to drain the existing production service before initialization"
            )

    try:
        account = pwd.getpwnam(deployment.user)
    except KeyError:
        run(
            [
                "useradd",
                "--system",
                "--home-dir",
                f"/var/lib/{deployment.user}",
                "--shell",
                "/usr/sbin/nologin",
                deployment.user,
            ]
        )
        account = pwd.getpwnam(deployment.user)
    home_dir = Path(account.pw_dir)
    if not home_dir.exists():
        home_dir.mkdir(parents=True, mode=0o750)
        os.chown(home_dir, account.pw_uid, account.pw_gid)
    check_server_environment(initialize=True)
    for key in ("DATA_PLATFORM_STATE_ROOT", "DATA_PLATFORM_OUTPUT_DIR", "DATA_PLATFORM_REMOTE_CACHE_ROOT"):
        path = Path(os.environ[key])
        os.chown(path, account.pw_uid, account.pw_gid)
        marker = path / ".data-platform-environment.json"
        os.chown(marker, 0, account.pw_gid)
    state = Path(os.environ["DATA_PLATFORM_OUTPUT_DIR"]) / "management"
    state.mkdir(mode=0o700, exist_ok=True)
    os.chown(state, account.pw_uid, account.pw_gid)
    verify_directory(state, "agent", initialize=True)
    os.chown(state / ".data-platform-environment.json", 0, account.pw_gid)
    local_state = state / "local-agent.json"
    if local_state.exists():
        from lerobot.data_platform.releases import atomic_json

        value = json.loads(local_state.read_text())
        if value.get("environment") and (value["environment"], value.get("instance_id")) != (
            deployment.environment,
            os.environ["DATA_PLATFORM_INSTANCE_ID"],
        ):
            raise RuntimeError("Local executor state belongs to another environment")
        value.update(environment=deployment.environment, instance_id=os.environ["DATA_PLATFORM_INSTANCE_ID"])
        atomic_json(local_state, value)
        os.chown(local_state, account.pw_uid, account.pw_gid)
    os.chown(deployment.config, 0, account.pw_gid)
    deployment.config.chmod(0o640)


def adopt_legacy(deployment):
    from sqlalchemy import create_engine, text

    from lerobot.data_platform.deployment import read_environment_file
    from lerobot.data_platform.deployment_backup import backup
    from lerobot.data_platform.deployment_network import configure_network
    from lerobot.data_platform.releases import run

    if deployment.environment != "prod":
        raise RuntimeError("Only prod can adopt the legacy installation")
    if (deployment.root / "legacy-adopted.json").exists():
        raise RuntimeError("Production has already been adopted; use the normal release commands")
    old_config = Path("/etc/data-platform/server.env")
    previous = read_environment_file(old_config)
    for key in (
        "DATA_PLATFORM_DATABASE_URL",
        "DATA_PLATFORM_LOG_DATABASE_URL",
        "DATA_PLATFORM_OUTPUT_DIR",
        "DATA_PLATFORM_REMOTE_CACHE_ROOT",
    ):
        if previous.get(key) != os.environ.get(key):
            raise RuntimeError("Legacy adoption must preserve database URLs and state paths")
    work = Path("/var/lib/data-platform-deployment")
    work.mkdir(mode=0o755, exist_ok=True)
    (work / "prod-maintenance").touch()
    original = work / "legacy-server.env"
    if not original.exists():
        shutil.copy2(old_config, original)
        original.chmod(0o600)
    configure_network(deployment, adopt_legacy=True)
    # Preserve the old config verbatim except for its supported claim-pause setting.
    lines = [
        line
        for line in old_config.read_text().splitlines()
        if not line.startswith("DATA_PLATFORM_PAUSE_CLAIMS=")
    ]
    old_config.write_text("\n".join([*lines, "DATA_PLATFORM_PAUSE_CLAIMS=1", ""]))
    run(["systemctl", "restart", "data-platform-web.service"])
    engine = create_engine(previous["DATA_PLATFORM_DATABASE_URL"])
    try:
        with engine.connect() as connection:
            count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM dp_jobs WHERE status IN ('running','cancel_requested','interrupted')"
                )
            ).scalar_one()
        if count:
            raise RuntimeError("Legacy maintenance enabled; wait/reconcile active jobs, then rerun adoption")
    finally:
        engine.dispose()
    run(["systemctl", "disable", "--now", "data-platform-web.service", "data-platform-local-worker.service"])
    backup(deployment, work / f"legacy-backup-{time.time_ns()}")
    initialize(deployment)


def copy_sample(source: Path, destination: Path, deployment):
    from lerobot.data_platform.environment import validate_dev_path

    if deployment.environment != "dev":
        raise RuntimeError("Sample copies are only created in dev")
    validate_dev_path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Sample output already exists")
    if not (source / "meta/info.json").is_file():
        raise ValueError("Source must be a LeRobot dataset root")
    # shutil copies file contents even when the source is a symlink; it never makes hard links.
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, destination, symlinks=False)
        account = pwd.getpwnam(deployment.user)
        for path in [destination, *destination.rglob("*")]:
            os.chown(path, account.pw_uid, account.pw_gid)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "init",
            "adopt-legacy",
            "network",
            "copy-sample",
            "check",
            "seed-dev-users",
            "maintenance-on",
            "maintenance-off",
            "status",
        ),
    )
    parser.add_argument("--env", required=True, choices=("dev", "prod"))
    parser.add_argument("--release", default="")
    parser.add_argument("--adopt-legacy", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    deployment = Deployment(args.env)
    load_environment(deployment)
    if args.command == "network":
        from lerobot.data_platform.deployment_network import configure_network

        configure_network(deployment, adopt_legacy=args.adopt_legacy)
        return
    if args.command in {"init", "adopt-legacy"}:
        if args.command == "adopt-legacy":
            adopt_legacy(deployment)
        else:
            initialize(deployment)
    else:
        check_server_environment()
    if args.command == "copy-sample":
        if not args.source or not args.destination:
            parser.error("--source and --destination are required")
        copy_sample(args.source, args.destination, deployment)
        return
    from lerobot.data_platform.control_plane import ControlPlaneStore
    from lerobot.data_platform.maintenance import active_jobs, is_maintenance, set_maintenance
    from lerobot.data_platform.management_storage import UsageLogStore

    store = ControlPlaneStore(
        os.environ["DATA_PLATFORM_DATABASE_URL"], initialize_schema=args.command in {"init", "adopt-legacy"}
    )
    if args.command in {"init", "adopt-legacy"}:
        UsageLogStore(os.environ["DATA_PLATFORM_LOG_DATABASE_URL"]).ensure_schema(initialize=True)
        if args.command == "adopt-legacy":
            set_maintenance(store, True)
            from lerobot.data_platform.releases import atomic_json

            atomic_json(
                deployment.root / "legacy-adopted.json",
                {
                    "environment": "prod",
                    "instance_id": os.environ["DATA_PLATFORM_INSTANCE_ID"],
                    "adopted_at": time.time(),
                },
            )
    elif args.command == "seed-dev-users":
        from lerobot.data_platform.dev_roles import seed_test_users

        seed_test_users(store)
    elif args.command.startswith("maintenance-"):
        set_maintenance(store, args.command == "maintenance-on", release=args.release)
        if args.command == "maintenance-off":
            deployment.maintenance_file.unlink(missing_ok=True)
    with store.sessions() as session:
        maintenance = is_maintenance(session)
    print(
        json.dumps({"environment": args.env, "maintenance": maintenance, "active_jobs": active_jobs(store)})
    )


if __name__ == "__main__":
    from lerobot.data_platform.deployment import safe_cli

    safe_cli(main)

"""Operator commands for immutable development-to-production releases."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import time
from pathlib import Path

from lerobot.data_platform import releases
from lerobot.data_platform.deployment import Deployment, agent_targets, load_environment


def _store():
    from lerobot.data_platform.control_plane import ControlPlaneStore

    return ControlPlaneStore(os.environ["DATA_PLATFORM_DATABASE_URL"])


def ssh_prefix():
    caller = os.environ.get("SUDO_USER")
    return ["runuser", "-u", caller, "--"] if caller and caller != "root" else []


def agent_connection(target):
    host = target.host
    identity = target.identity_file
    if not re_full_host(host) or not Path(identity).is_file():
        raise RuntimeError("Configure a valid environment-specific Agent SSH host and identity file")
    return host, [
        "-i",
        identity,
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
    ]


def re_full_host(host):
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", host))


def agent_preflight(deployment):
    for target in agent_targets():
        host, options = agent_connection(target)
        config = f"/etc/data-platform/{deployment.environment}/agent.env"
        program = """import json, shlex, sys
values = {}
for line in open(sys.argv[1]):
    key, sep, value = line.strip().partition('=')
    if sep and key in {'DATA_PLATFORM_ENV', 'DATA_PLATFORM_INSTANCE_ID', 'DATA_PLATFORM_AGENT_NAME'}:
        values[key] = shlex.split(value)[0]
print(json.dumps(values))
"""
        command = f'if [ "$(id -u)" = 0 ]; then set --; else set -- sudo -n; fi; "$@" python3 -c {shlex.quote(program)} {config}'
        result = releases.run([*ssh_prefix(), "ssh", *options, host, command], capture_output=True, text=True)
        identity = json.loads(result.stdout)
        if (identity.get("DATA_PLATFORM_ENV"), identity.get("DATA_PLATFORM_INSTANCE_ID")) != (
            deployment.environment,
            os.environ["DATA_PLATFORM_INSTANCE_ID"],
        ) or identity.get("DATA_PLATFORM_AGENT_NAME") not in target.node_names:
            raise RuntimeError("Remote Agent configuration does not match the selected environment/node")


def install_agent(deployment, version, *, hard=False):
    return [_install_agent_target(deployment, version, target, hard=hard) for target in agent_targets()]


def _install_agent_target(deployment, version, target, *, hard=False):
    root, manifest = releases.verify_release(
        version, production=deployment.environment == "prod" and not hard
    )
    host, options = agent_connection(target)
    archive = manifest["agent_archive"]
    remote = f"data-platform-updates/{deployment.environment}/{version}"
    releases.run([*ssh_prefix(), "ssh", *options, host, f"umask 077; mkdir -p {remote}"])
    # The receipt and archives are root-owned. Copy readable artifacts to a bounded upload directory.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="data-platform-upload-") as directory:
        import shutil

        upload = Path(directory)
        upload.chmod(0o755)
        for name in (archive, archive + ".sha256"):
            if name.endswith(".sha256"):
                (upload / name).write_text(f"{manifest['files'][archive]}  {archive}\n")
            else:
                shutil.copy2(root / name, upload / name)
            (upload / name).chmod(0o644)
        releases.run(
            [
                *ssh_prefix(),
                "scp",
                *options,
                upload / archive,
                upload / (archive + ".sha256"),
                f"{host}:{remote}/",
            ]
        )
    env = deployment.environment
    command = f"""set -eu
cd {remote}
sha256sum --check {archive}.sha256
tar -xzf {archive}
cd {archive.removesuffix(".tar.gz")}
./install.sh --verify-only
if [ "$(id -u)" = 0 ]; then set --; else set -- sudo -n; fi
"$@" ./install.sh --env {env}
test "$(readlink -f /opt/data-platform-agent/{env}/current)" = /opt/data-platform-agent/{env}/releases/{version}
"$@" systemctl is-active --quiet data-platform-agent@{env}.service
"""
    releases.run([*ssh_prefix(), "ssh", *options, host, command])
    # Cleanup is deferred until central heartbeat validation succeeds.
    return (host, options, remote)


def deploy(
    deployment, version, *, with_agent=True, index_url=None, rollback=False, restore_from=None, hard=False
):
    from sqlalchemy import func, select

    from lerobot.data_platform.deployment_backup import backup, restore_backup, validate_backup
    from lerobot.data_platform.environment import check_server_environment
    from lerobot.data_platform.maintenance import set_maintenance, wait_until_idle
    from lerobot.data_platform.management_storage import SchemaMigration

    load_environment(deployment)
    check_server_environment()
    root, manifest = releases.verify_release(
        version, production=deployment.environment == "prod" and not hard
    )
    deployment.root.mkdir(parents=True, exist_ok=True)
    with (deployment.root / ".deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = _store()
        with store.sessions() as session:
            schema = session.scalar(select(func.max(SchemaMigration.version)))
        if restore_from is not None:
            if not rollback:
                raise ValueError("--restore-backup is only valid with rollback")
            validate_backup(deployment, restore_from)
        if (
            rollback
            and restore_from is None
            and not manifest["schema_min"] <= schema <= manifest["schema_max"]
        ):
            raise RuntimeError(
                "Database schema is incompatible; remain in maintenance and restore the backup explicitly"
            )
        previous = (deployment.root / "current").resolve() if (deployment.root / "current").exists() else None
        state = {
            "environment": deployment.environment,
            "release": version,
            "previous": str(previous) if previous else None,
            "started_at": time.time(),
            "status": "running",
            "steps": [],
            "mode": "hard" if hard else "standard",
            "approval_bypassed": hard and deployment.environment == "prod",
            "requested_by": os.environ.get("SUDO_USER", str(os.getuid())),
        }
        state_path = deployment.root / "deployment.json"

        def progress(phase):
            state["phase"] = phase
            state["updated_at"] = time.time()
            state["steps"].append({"phase": phase, "at": state["updated_at"]})
            releases.atomic_json(state_path, state)

        try:
            progress("preflight")
            if with_agent:
                agent_preflight(deployment)
            # Dependency errors never modify the running installation or pause user work.
            progress("dependencies")
            target = releases.prepare_server(deployment, version, index_url=index_url, hard=hard)
            progress("draining")
            set_maintenance(store, True, release=version)
            wait_until_idle(store)
            progress("stopping")
            # Only the selected environment is stopped. Existing jobs have already drained.
            if previous is not None:
                releases.run(["systemctl", "stop", *deployment.units])
            saved = deployment.root / "backups" / f"{version}-{time.time_ns()}"
            progress("backup")
            backup(deployment, saved)
            state["backup"] = str(saved)
            releases.atomic_json(state_path, state)
            if restore_from is not None:
                progress("restore")
                restore_backup(deployment, restore_from)
            progress("migration")
            migration_env = dict(os.environ, DATA_PLATFORM_RELEASE=version)
            releases.run(
                [
                    target / ".venv/bin/python",
                    "-m",
                    "lerobot.data_platform.environment_cli",
                    "init",
                    "--env",
                    deployment.environment,
                ],
                cwd=target,
                env=migration_env,
            )
            set_maintenance(store, True, release=version)
            progress("restart")
            releases.switch_current(deployment, target)
            releases.install_server_units(deployment)
            releases.run(["systemctl", "restart", *deployment.units])
            progress("health")
            releases.wait_healthy(deployment, version)
            if with_agent:
                progress("agents")
            remote = install_agent(deployment, version, hard=hard) if with_agent else None
            if with_agent:
                progress("heartbeats")
                for attempt in range(30):
                    try:
                        releases.check_nodes(
                            store,
                            version,
                            required_names=[name for target in agent_targets() for name in target.node_names],
                        )
                        break
                    except RuntimeError:
                        if attempt == 29:
                            raise
                        time.sleep(2)
            if remote:
                for host, options, directory in remote:
                    releases.run(
                        [*ssh_prefix(), "ssh", *options, host, f"rm -rf -- {shlex.quote(directory)}"]
                    )
            # A server-only update deliberately leaves maintenance enabled until matching Agents are checked.
            if (
                deployment.environment == "dev"
                and Path("/etc/systemd/system/data-platform-promotion.service").is_file()
            ):
                releases.run(["systemctl", "try-restart", "data-platform-promotion.service"])
            if with_agent:
                progress("reopening")
                set_maintenance(store, False, release=version)
                deployment.maintenance_file.unlink(missing_ok=True)
            state["status"] = "installed" if with_agent else "server-installed"
            state["finished_at"] = time.time()
            progress("complete" if with_agent else "server-only")
            if with_agent:
                history = deployment.root / "update-history.jsonl"
                with history.open("a") as stream:
                    stream.write(json.dumps(state) + "\n")
            print(json.dumps(state))
        except BaseException:
            state["status"] = "failed"
            state["finished_at"] = time.time()
            state["updated_at"] = state["finished_at"]
            releases.atomic_json(state_path, state)
            # No automatic DB reversal and no automatic reopening after a partial upgrade.
            raise


def deploy_both(version, *, index_url=None):
    """Explicit direct upgrade; no acceptance receipt is created or modified."""
    from lerobot.data_platform.environment import check_server_environment

    releases.verify_release(version)
    deployments = [Deployment("dev"), Deployment("prod")]
    # Catch missing/wrong configuration in either environment before changing services.
    for deployment in deployments:
        load_environment(deployment)
        check_server_environment()
        agent_preflight(deployment)
    for deployment in deployments:
        print(f"Hard upgrade: {deployment.environment} -> {version}", flush=True)
        deploy(deployment, version, index_url=index_url, hard=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "approve", "deploy", "rollback", "status", "restart"))
    parser.add_argument("--env", choices=("dev", "prod", "both"), required=True)
    parser.add_argument(
        "--hard",
        action="store_true",
        help="With deploy --env both, upgrade dev then prod without manual development approval",
    )
    parser.add_argument("--release")
    parser.add_argument("--version")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--index-url")
    parser.add_argument("--server-only", action="store_true")
    parser.add_argument(
        "--restore-backup",
        type=Path,
        help="Explicitly replace this stopped environment's databases/state from a matching backup",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run via sudo; the original SSH owner is retained through SUDO_USER")
    version = args.release or args.version
    if args.command not in {"status", "restart"} and not version:
        parser.error("--release or --version is required")
    if args.env == "both" or args.hard:
        if args.command != "deploy" or args.env != "both" or not args.hard:
            parser.error("Hard upgrade requires deploy --env both --hard")
        if args.server_only or args.restore_backup:
            parser.error(
                "Hard upgrade includes Agents and cannot be combined with --server-only/--restore-backup"
            )
        if args.release and args.version:
            parser.error("Choose --version to build once or --release to reuse existing artifacts")
        if args.version:
            releases.build_release(args.source, version, index_url=args.index_url)
        deploy_both(version, index_url=args.index_url)
        return
    deployment = Deployment(args.env)
    if args.command == "build":
        if args.env != "dev":
            parser.error("Build candidates in dev; production reuses approved artifacts")
        print(releases.build_release(args.source, version, index_url=args.index_url))
    elif args.command == "approve":
        if not args.evidence:
            parser.error("--evidence is required for manual acceptance")
        releases.approve_release(deployment, version, args.evidence)
    elif args.command in {"deploy", "rollback"}:
        if args.version and not args.release:
            if args.env != "dev":
                parser.error("Production requires --release for an approved artifact")
            releases.build_release(args.source, version, index_url=args.index_url)
        deploy(
            deployment,
            version,
            with_agent=not args.server_only,
            index_url=args.index_url,
            rollback=args.command == "rollback",
            restore_from=args.restore_backup,
        )
    else:
        load_environment(deployment)
        if args.command == "restart":
            releases.run(["systemctl", "restart", *deployment.units])
        state = deployment.root / "deployment.json"
        print(
            state.read_text()
            if state.exists()
            else json.dumps({"environment": args.env, "status": "not-deployed"})
        )


if __name__ == "__main__":
    from lerobot.data_platform.deployment import safe_cli

    safe_cli(main)

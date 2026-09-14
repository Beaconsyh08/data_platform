"""Release integrity, ordering, failure containment and environment-specific deployment tests."""

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from lerobot.data_platform import releases
from lerobot.data_platform.deployment import Deployment, read_environment_file, server_unit
from lerobot.data_platform.deployment_network import nginx_config


@pytest.mark.parametrize("lock_in_archive", [False, True])
def test_build_checks_archived_lock_before_tests_and_preserves_it(tmp_path, monkeypatch, lock_in_archive):
    source = tmp_path / "source"
    source.mkdir()
    # An ignored local lock file must not hide a missing lock in the Git archive.
    lock = b"version = 1\nrevision = 3\n"
    (source / "uv.lock").write_bytes(lock)
    monkeypatch.setattr(releases, "RELEASE_ROOT", tmp_path / "releases")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="")
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="commit\n")
        if "archive" in argv:
            with tarfile.open(fileobj=kwargs["stdout"], mode="w:gz") as stream:
                if lock_in_archive:
                    entry = tarfile.TarInfo("uv.lock")
                    entry.size = len(lock)
                    stream.addfile(entry, io.BytesIO(lock))
            return
        if "pytest" in argv:
            assert (kwargs["cwd"] / "uv.lock").read_bytes() == lock
            kwargs["stdout"].write("passed")
            return
        if "--output-dir" in argv:
            output = Path(argv[argv.index("--output-dir") + 1])
            output.mkdir()
            agent = output / "agent.tar.gz"
            with tarfile.open(agent, "w:gz") as stream:
                entry = tarfile.TarInfo("agent/manifest.sha256")
                entry.size = 0
                stream.addfile(entry, io.BytesIO())
            Path(str(agent) + ".sha256").write_text("test checksum")
            return
        pytest.fail("Unexpected subprocess")

    monkeypatch.setattr(releases, "run", run)
    if not lock_in_archive:
        with pytest.raises(RuntimeError, match="committed source archive is missing uv.lock"):
            releases.build_release(source, "candidate")
        assert not any("pytest" in call or "--output-dir" in call for call in calls)
        assert not (releases.RELEASE_ROOT / "candidate").exists()
    else:
        root = releases.build_release(source, "candidate")
        releases.verify_release("candidate")
        with tarfile.open(root / "server.tar.gz") as stream:
            assert stream.extractfile("uv.lock").read() == lock


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "RELEASE_ROOT", tmp_path / "artifacts")
    root = releases.RELEASE_ROOT / "candidate"
    root.mkdir(parents=True)
    (root / "server.tar.gz").write_bytes(b"server")
    (root / "agent.tar.gz").write_bytes(b"agent")
    (root / "validation.txt").write_text("Passed")
    releases.atomic_json(
        root / "release.json",
        {
            "format": 1,
            "release": "candidate",
            "agent_archive": "agent.tar.gz",
            "schema_min": 2,
            "schema_max": 2,
            "files": {
                name: releases.digest(root / name)
                for name in ("server.tar.gz", "agent.tar.gz", "validation.txt")
            },
        },
    )
    return root


def test_production_requires_approval_bound_to_exact_manifest(release):
    releases.verify_release("candidate")
    with pytest.raises(FileNotFoundError):
        releases.verify_release("candidate", production=True)
    releases.atomic_json(
        release / "approval.json",
        {"environment": "dev", "manifest_sha256": releases.digest(release / "release.json")},
    )
    releases.verify_release("candidate", production=True)
    (release / "server.tar.gz").write_bytes(b"changed after acceptance")
    with pytest.raises(ValueError, match="checksum"):
        releases.verify_release("candidate", production=True)


def test_changed_manifest_invalidates_acceptance(release):
    releases.atomic_json(
        release / "approval.json",
        {"environment": "dev", "manifest_sha256": releases.digest(release / "release.json")},
    )
    manifest = json.loads((release / "release.json").read_text())
    manifest["schema_max"] = 3
    releases.atomic_json(release / "release.json", manifest)
    with pytest.raises(ValueError, match="exact artifacts"):
        releases.verify_release("candidate", production=True)


@pytest.mark.parametrize(
    "name,kind", [("../outside", "file"), ("/outside", "file"), ("link", "symlink"), ("link", "hardlink")]
)
def test_release_extraction_rejects_traversal_and_links(tmp_path, name, kind):
    archive = tmp_path / "archive.tar"
    with tarfile.open(archive, "w") as stream:
        entry = tarfile.TarInfo(name)
        entry.type = {"file": tarfile.REGTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE}[kind]
        entry.linkname = "outside"
        stream.addfile(entry, io.BytesIO())
    with pytest.raises(ValueError, match="Unsafe"):
        releases.extract_archive(archive, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("offline_hit,online_failure", [(True, False), (False, False), (False, True)])
def test_dependency_failure_never_switches_current(
    release, tmp_path, monkeypatch, offline_hit, online_failure
):
    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / "installed"))
    deployment = Deployment("dev")
    old = deployment.root / "releases/old"
    old.mkdir(parents=True)
    (deployment.root / "current").symlink_to(old)
    manifest = json.loads((release / "release.json").read_text())
    manifest["agent_manifest_sha256"] = "a" * 64
    releases.atomic_json(release / "release.json", manifest)
    monkeypatch.setattr(releases, "extract_archive", lambda archive, target: None)
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if "sync" in argv and (
            ("--offline" in argv and not offline_hit) or ("--offline" not in argv and online_failure)
        ):
            raise subprocess.CalledProcessError(9, argv)

    monkeypatch.setattr(releases, "run", command)
    if online_failure:
        with pytest.raises(subprocess.CalledProcessError):
            releases.prepare_server(deployment, "candidate", index_url="https://mirror.test/simple")
        assert not (deployment.root / "releases/candidate").exists()
    else:
        releases.prepare_server(deployment, "candidate", index_url="https://mirror.test/simple")
    assert (deployment.root / "current").resolve() == old
    sync = [argv for argv in calls if "sync" in argv]
    assert len(sync) == (1 if offline_hit else 2)
    assert all("--frozen" in argv and "https://mirror.test/simple" in argv for argv in sync)
    assert "--offline" in sync[0]


def test_corrupt_backup_is_rejected_before_database_restore(tmp_path, monkeypatch):
    from lerobot.data_platform import deployment_backup

    monkeypatch.setenv("DATA_PLATFORM_INSTANCE_ID", "instance")
    for name in ("control.sql", "logs.sql", "server.env"):
        (tmp_path / name).write_text("original")
    releases.atomic_json(
        tmp_path / "backup.json",
        {
            "environment": "dev",
            "instance_id": "instance",
            "complete": True,
            "state_paths": {},
            "files": {
                name: releases.digest(tmp_path / name) for name in ("control.sql", "logs.sql", "server.env")
            },
        },
    )
    (tmp_path / "control.sql").write_text("corrupt")
    monkeypatch.setattr(
        deployment_backup, "mysql_command", lambda *args, **kwargs: pytest.fail("No database changes allowed")
    )
    with pytest.raises(RuntimeError, match="checksum"):
        deployment_backup.restore_backup(Deployment("dev"), tmp_path)


def test_environment_file_does_not_execute_shell_and_rejects_duplicates(tmp_path):
    config = tmp_path / "server.env"
    config.write_text("DATA_PLATFORM_ENV=dev\nDATA_PLATFORM_VALUE='$(touch SHOULD_NOT_EXIST)'\n")
    assert read_environment_file(config)["DATA_PLATFORM_VALUE"] == "$(touch SHOULD_NOT_EXIST)"
    config.write_text("DATA_PLATFORM_ENV=dev\nDATA_PLATFORM_ENV=prod\n")
    with pytest.raises(ValueError, match="duplicate"):
        read_environment_file(config)


def test_ports_cookies_units_and_state_are_separate():
    dev, prod = Deployment("dev"), Deployment("prod")
    for deployment in (dev, prod):
        config = nginx_config(deployment)
        assert f"listen {deployment.public_port} ssl" in config
        assert f"127.0.0.1:{deployment.port}" in config
        assert "Host $http_host" in config  # Preserve port for browser Origin validation.
        assert f"proxy_set_header Cookie $dp_{deployment.environment}_cookie" in config
        assert "proxy_set_header Cookie $http_cookie" not in config
        unit = server_unit(deployment, worker=True)
        assert str(deployment.config) in unit and str(deployment.root / "current") in unit
        assert "Delegate=yes" in unit
    assert set(dev.units).isdisjoint(prod.units)
    assert dev.user != prod.user
    assert "Slice=data-platform-dev.slice" in server_unit(dev, worker=False)


@pytest.mark.parametrize(
    "script", ["update-all.sh", "update-server.sh", "restart-server.sh", "configure-dynamic-ip.sh"]
)
@pytest.mark.parametrize("arguments", [[], ["--env", "both"]])
def test_commands_require_environment_before_sudo_or_mutations(script, tmp_path, arguments):
    root = Path(__file__).parents[2] / "deploy/data-platform"
    result = subprocess.run(
        ["bash", str(root / script), *arguments], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert ("Combined upgrade requires" if arguments else "Explicit --env") in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", [None, "prepare", "health", "agent"])
@pytest.mark.parametrize("environment_name,hard", [("dev", False), ("prod", True)])
def test_deploy_order_and_failure_preserve_maintenance(
    release, tmp_path, monkeypatch, failure, environment_name, hard
):
    from lerobot.data_platform import deployment_backup, environment, release_cli
    from lerobot.data_platform.control_plane import ControlPlaneStore
    from lerobot.data_platform.maintenance import is_maintenance

    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / "installed" / self.environment))
    monkeypatch.setattr(release_cli, "load_environment", lambda deployment: {})
    monkeypatch.setattr(environment, "check_server_environment", lambda: None)
    store = ControlPlaneStore(f"sqlite:///{tmp_path}/control.db")
    monkeypatch.setattr(release_cli, "_store", lambda: store)
    from lerobot.data_platform.deployment import AgentTarget

    monkeypatch.setattr(
        release_cli, "agent_targets", lambda: [AgentTarget("test-host", "/unused", ("test-node",))]
    )
    monkeypatch.setattr(Deployment, "maintenance_file", property(lambda self: tmp_path / "maintenance"))
    events = []
    monkeypatch.setattr(release_cli, "agent_preflight", lambda deployment: events.append("ssh"))

    def prepare(deployment, version, **kwargs):
        events.append("prepare")
        if failure == "prepare":
            raise RuntimeError("dependency failed")
        target = deployment.root / "releases" / version
        target.mkdir(parents=True)
        return target

    monkeypatch.setattr(releases, "prepare_server", prepare)
    monkeypatch.setattr(releases, "run", lambda argv, **kwargs: events.append(list(map(str, argv))))
    monkeypatch.setattr(releases, "install_server_units", lambda deployment: events.append("units"))
    monkeypatch.setattr(deployment_backup, "backup", lambda deployment, target: events.append("backup"))

    def healthy(*args):
        events.append("health")
        if failure == "health":
            raise RuntimeError("health failed")

    def agent(*args, **kwargs):
        assert kwargs["hard"] is hard
        events.append("agent")
        if failure == "agent":
            raise RuntimeError("agent failed")
        return None

    monkeypatch.setattr(releases, "wait_healthy", healthy)
    monkeypatch.setattr(release_cli, "install_agent", agent)
    monkeypatch.setattr(releases, "check_nodes", lambda *args, **kwargs: events.append("heartbeat"))
    if failure:
        with pytest.raises(RuntimeError):
            release_cli.deploy(Deployment(environment_name), "candidate", hard=hard)
    else:
        release_cli.deploy(Deployment(environment_name), "candidate", hard=hard)
    with store.sessions() as session:
        assert is_maintenance(session) == (failure in {"health", "agent"})
    assert events[:2] == ["ssh", "prepare"]
    if failure == "prepare":
        assert events == ["ssh", "prepare"]
    else:
        assert events.index("backup") < events.index("units") < events.index("health")
        if failure != "health":
            assert events.index("health") < events.index("agent")
        other_environment = "prod" if environment_name == "dev" else "dev"
        assert not any(
            f"@{other_environment}" in str(event) or "restart', 'mysql" in str(event) for event in events
        )
        state = json.loads((Deployment(environment_name).root / "deployment.json").read_text())
        assert state["mode"] == ("hard" if hard else "standard")
        assert state["approval_bypassed"] is hard
    assert not (release / "approval.json").exists()


@pytest.mark.parametrize("build", [False, True])
@pytest.mark.parametrize("failure", [None, "preflight-prod", "deploy-dev", "deploy-prod"])
def test_hard_upgrade_builds_once_and_stops_on_failure(release, monkeypatch, build, failure):
    import sys

    from lerobot.data_platform import environment, release_cli

    events = []
    monkeypatch.setattr(release_cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(releases, "build_release", lambda *args: events.append("build"))
    monkeypatch.setattr(
        release_cli, "load_environment", lambda target: events.append(f"load-{target.environment}")
    )
    monkeypatch.setattr(environment, "check_server_environment", lambda: events.append("identity"))

    def preflight(target):
        name = f"preflight-{target.environment}"
        events.append(name)
        if name == failure:
            raise RuntimeError("preflight failed")

    def deploy(target, version, **kwargs):
        assert version == "candidate"
        assert kwargs == {"index_url": "https://mirror.test/simple", "hard": True}
        name = f"deploy-{target.environment}"
        events.append(name)
        if name == failure:
            raise RuntimeError("deploy failed")

    monkeypatch.setattr(release_cli, "agent_preflight", preflight)
    monkeypatch.setattr(release_cli, "deploy", deploy)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release",
            "deploy",
            "--env",
            "both",
            "--hard",
            "--version" if build else "--release",
            "candidate",
            "--index-url",
            "https://mirror.test/simple",
        ],
    )
    if failure:
        with pytest.raises(RuntimeError):
            release_cli.main()
    else:
        release_cli.main()
    expected = (["build"] if build else []) + [
        "load-dev",
        "identity",
        "preflight-dev",
        "load-prod",
        "identity",
        "preflight-prod",
        "deploy-dev",
        "deploy-prod",
    ]
    if failure:
        expected = expected[: expected.index(failure) + 1]
    assert events == expected
    assert not (release / "approval.json").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["deploy", "--env", "both"],
        ["deploy", "--env", "prod", "--hard"],
        ["rollback", "--env", "both", "--hard"],
        ["deploy", "--env", "both", "--hard", "--server-only"],
        ["deploy", "--env", "both", "--hard", "--restore-backup", "/backup"],
    ],
)
def test_invalid_hard_upgrade_arguments_never_build_or_deploy(monkeypatch, arguments):
    import sys

    from lerobot.data_platform import release_cli

    monkeypatch.setattr(release_cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["release", *arguments, "--version", "candidate"])
    monkeypatch.setattr(releases, "build_release", lambda *args: pytest.fail("Must validate arguments first"))
    monkeypatch.setattr(release_cli, "deploy", lambda *args, **kwargs: pytest.fail("Must not deploy"))
    with pytest.raises(SystemExit) as error:
        release_cli.main()
    assert error.value.code == 2


def test_hard_agent_install_skips_approval_but_still_checks_hashes(release, monkeypatch):
    from lerobot.data_platform import release_cli
    from lerobot.data_platform.deployment import AgentTarget

    monkeypatch.setattr(release_cli, "ssh_prefix", lambda: [])
    monkeypatch.setattr(release_cli, "agent_connection", lambda target: (target.host, []))
    calls = []
    monkeypatch.setattr(releases, "run", lambda argv, **kwargs: calls.append(argv))
    target = AgentTarget("host", "/unused", ("prod-node",))
    with pytest.raises(FileNotFoundError):
        release_cli._install_agent_target(Deployment("prod"), "candidate", target)
    assert calls == []
    release_cli._install_agent_target(Deployment("prod"), "candidate", target, hard=True)
    assert any("./install.sh --env prod" in str(call) for call in calls)
    assert not (release / "approval.json").exists()
    calls.clear()
    (release / "agent.tar.gz").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        release_cli._install_agent_target(Deployment("prod"), "candidate", target, hard=True)
    assert calls == []

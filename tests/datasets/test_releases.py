"""Release integrity, ordering, failure containment and environment-specific deployment tests."""

import io
import json
import os
import runpy
import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest

from lerobot.data_platform import releases
from lerobot.data_platform.deployment import Deployment, read_environment_file, server_unit
from lerobot.data_platform.deployment_network import nginx_config


def test_hindsight_runtime_permissions_allow_services_without_exposing_private_state(tmp_path):
    installer = Path(__file__).resolve().parents[2] / "deploy/data-platform/install-hindsight.py"
    normalize = runpy.run_path(str(installer))["readable_runtime"]
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    package = runtime / "package.py"
    package.write_text("# public package\n")
    package.chmod(0o600)
    binary = runtime / "python"
    binary.write_bytes(b"executable")
    binary.chmod(0o700)
    private = tmp_path / "private.env"
    private.write_text("PRIVATE=synthetic\n")
    private.chmod(0o600)
    (runtime / "external-link").symlink_to(private)
    normalize(runtime)
    assert runtime.stat().st_mode & 0o777 == 0o755
    assert package.stat().st_mode & 0o777 == 0o644
    assert binary.stat().st_mode & 0o777 == 0o755
    assert private.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("index_url", [None, "https://mirror.test/simple"])
@pytest.mark.parametrize("lock_in_archive", [False, True])
@pytest.mark.parametrize("validation_fails", [False, True])
def test_build_checks_archived_lock_before_tests_and_preserves_it(
    tmp_path, monkeypatch, lock_in_archive, index_url, validation_fails
):
    source = tmp_path / "source"
    source.mkdir()
    # An ignored local lock file must not hide a missing lock in the Git archive.
    lock = b"version = 1\nrevision = 3\n"
    (source / "uv.lock").write_bytes(lock)
    monkeypatch.setattr(releases, "RELEASE_ROOT", tmp_path / "releases")
    calls = []
    browser = tmp_path / "chrome"
    browser.touch()
    browser.chmod(0o700)

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
            if validation_fails:
                kwargs["stdout"].write("FAILED test_release_regression: useful failure detail\n")
                raise subprocess.CalledProcessError(1, argv)
            kwargs["stdout"].write("passed")
            for arg in argv:
                if str(arg).startswith("--junitxml="):
                    from lerobot.data_platform.browser_validation import REQUIRED_CASES

                    Path(str(arg).split("=", 1)[1]).write_text(
                        "<testsuite>"
                        + "".join(f'<testcase name="{name}"/>' for name in REQUIRED_CASES)
                        + "</testsuite>"
                    )
            return
        if "--output-dir" in argv:
            if index_url:
                assert kwargs["env"]["UV_DEFAULT_INDEX"] == index_url
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
            releases.build_release(source, "candidate", index_url=index_url, browser_executable=browser)
        assert not any("pytest" in call or "--output-dir" in call for call in calls)
        assert not (releases.RELEASE_ROOT / "candidate").exists()
    elif validation_fails:
        with pytest.raises(RuntimeError, match="Release validation failed.*validation.txt"):
            releases.build_release(source, "candidate", index_url=index_url, browser_executable=browser)
        assert not (releases.RELEASE_ROOT / "candidate").exists()
        assert not any("--output-dir" in call for call in calls)
        assert not list(releases.RELEASE_ROOT.glob(".build-*"))
        failures = list(releases.RELEASE_ROOT.glob(".failed-candidate-*"))
        assert len(failures) == 1
        assert failures[0].stat().st_mode & 0o777 == 0o700
        assert "useful failure detail" in (failures[0] / "validation.txt").read_text()
        assert json.loads((failures[0] / "failure.json").read_text()) == {
            "release": "candidate",
            "commit": "commit",
            "stage": "validation",
            "exit_code": 1,
        }
    else:
        root = releases.build_release(source, "candidate", index_url=index_url, browser_executable=browser)
        _, manifest = releases.verify_release("candidate")
        assert manifest["package_index_url"] == index_url
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
@pytest.mark.parametrize("assistant_runtime", [False, True])
def test_dependency_failure_never_switches_current(
    release, tmp_path, monkeypatch, offline_hit, online_failure, assistant_runtime
):
    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / "installed"))
    deployment = Deployment("dev")
    old = deployment.root / "releases/old"
    old.mkdir(parents=True)
    (deployment.root / "current").symlink_to(old)
    manifest = json.loads((release / "release.json").read_text())
    manifest["agent_manifest_sha256"] = "a" * 64
    releases.atomic_json(release / "release.json", manifest)

    def extract(archive, target):
        if assistant_runtime:
            module = target / "lerobot/data_platform/ai_tools/runtime.py"
            module.parent.mkdir(parents=True)
            module.touch()

    monkeypatch.setattr(releases, "extract_archive", extract)
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
    probes = [argv[-1] for argv in calls if argv[0] == "runuser"]
    if not online_failure:
        assert len(probes) == 1
        assert ("ai_tools.runtime" in probes[0]) == assistant_runtime


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
        assert "gzip on;" in config
        assert "gzip_types application/json;" in config
        assert "gzip_min_length 1024;" in config
        assert "gzip_vary on;" in config
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


@pytest.mark.parametrize("failure", [None, "prepare", "drain_timeout", "interrupted_jobs", "health", "agent"])
@pytest.mark.parametrize("index_url", [None, "https://explicit.test/simple"])
@pytest.mark.parametrize("environment_name,hard", [("dev", False), ("prod", True)])
def test_deploy_order_and_failure_preserve_maintenance(
    release, tmp_path, monkeypatch, failure, environment_name, hard, index_url
):
    from lerobot.data_platform import deployment_backup, environment, maintenance, release_cli
    from lerobot.data_platform.control_plane import ControlPlaneStore
    from lerobot.data_platform.maintenance import ActiveJobCounts, JobDrainError, is_maintenance

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
    manifest = json.loads((release / "release.json").read_text())
    manifest["package_index_url"] = "https://saved.test/simple"
    releases.atomic_json(release / "release.json", manifest)
    expected_index = index_url or manifest["package_index_url"]
    events = []
    monkeypatch.setattr(release_cli, "agent_preflight", lambda deployment: events.append("ssh"))

    def prepare(deployment, version, **kwargs):
        assert kwargs["index_url"] == expected_index
        assert kwargs["hard"] is hard
        events.append("prepare")
        if failure == "prepare":
            raise RuntimeError("dependency failed")
        target = deployment.root / "releases" / version
        target.mkdir(parents=True)
        return target

    monkeypatch.setattr(releases, "prepare_server", prepare)
    if failure in {"drain_timeout", "interrupted_jobs"}:
        counts = ActiveJobCounts(running=1) if failure == "drain_timeout" else ActiveJobCounts(interrupted=1)

        def blocked(*args):
            raise JobDrainError(failure, counts)

        monkeypatch.setattr(maintenance, "wait_until_idle", blocked)
    monkeypatch.setattr(releases, "run", lambda argv, **kwargs: events.append(list(map(str, argv))))
    monkeypatch.setattr(releases, "install_server_units", lambda deployment: events.append("units"))
    monkeypatch.setattr(deployment_backup, "backup", lambda deployment, target: events.append("backup"))

    def healthy(*args):
        events.append("health")
        if failure == "health":
            raise RuntimeError("health failed")

    def agent(*args, **kwargs):
        assert kwargs["index_url"] == expected_index
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
            release_cli.deploy(Deployment(environment_name), "candidate", hard=hard, index_url=index_url)
    else:
        release_cli.deploy(Deployment(environment_name), "candidate", hard=hard, index_url=index_url)
    with store.sessions() as session:
        assert is_maintenance(session) == (
            failure in {"drain_timeout", "interrupted_jobs", "health", "agent"}
        )
    assert events[:2] == ["ssh", "prepare"]
    if failure in {"prepare", "drain_timeout", "interrupted_jobs"}:
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
    progress = json.loads((Deployment(environment_name).root / "deployment.json").read_text())
    assert progress["status"] == ("failed" if failure else "installed")
    assert (
        progress["phase"]
        == {
            "prepare": "dependencies",
            "drain_timeout": "draining",
            "interrupted_jobs": "draining",
            "health": "health",
            "agent": "agents",
            None: "complete",
        }[failure]
    )
    assert progress["steps"][0]["phase"] == "preflight"
    assert progress["finished_at"] >= progress["started_at"]
    assert not (release / "approval.json").exists()
    if failure in {"drain_timeout", "interrupted_jobs"}:
        assert progress["failure_code"] == failure
        assert progress["blocking_jobs"] == {
            "running": int(failure == "drain_timeout"),
            "cancel_requested": 0,
            "interrupted": int(failure == "interrupted_jobs"),
        }


@pytest.mark.parametrize("build", [False, True])
@pytest.mark.parametrize("failure", [None, "preflight-prod", "deploy-dev", "deploy-prod"])
def test_hard_upgrade_builds_once_and_stops_on_failure(release, monkeypatch, build, failure):
    import sys

    from lerobot.data_platform import environment, release_cli

    events = []
    monkeypatch.setattr(release_cli.os, "geteuid", lambda: 0)

    def build_release(*args, **kwargs):
        assert kwargs["index_url"] == "https://mirror.test/simple"
        events.append("build")

    monkeypatch.setattr(releases, "build_release", build_release)
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


def test_hard_server_prepare_skips_only_approval(release, tmp_path, monkeypatch):
    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / "installed"))
    deployment = Deployment("prod")
    target = deployment.root / "releases/candidate"
    target.mkdir(parents=True)
    (target / "manifest.sha256").write_text(releases.digest(release / "release.json"))
    with pytest.raises(FileNotFoundError):
        releases.prepare_server(deployment, "candidate")
    assert releases.prepare_server(deployment, "candidate", hard=True) == target
    assert not (release / "approval.json").exists()
    (release / "server.tar.gz").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        releases.prepare_server(deployment, "candidate", hard=True)


@pytest.mark.parametrize("command", ["build", "deploy"])
def test_development_cli_forwards_index_to_build(monkeypatch, command):
    import sys

    from lerobot.data_platform import release_cli

    monkeypatch.setattr(release_cli.os, "geteuid", lambda: 0)
    calls = []
    monkeypatch.setattr(releases, "build_release", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(release_cli, "deploy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release",
            command,
            "--env",
            "dev",
            "--version",
            "candidate",
            "--index-url",
            "https://mirror.test/simple",
        ],
    )
    release_cli.main()
    assert calls == [
        {"index_url": "https://mirror.test/simple", "browser_executable": Path("/usr/bin/google-chrome")}
    ]


def test_build_rejects_non_https_index_before_creating_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "RELEASE_ROOT", tmp_path / "artifacts")
    with pytest.raises(ValueError, match="HTTPS"):
        releases.build_release(tmp_path, "candidate", index_url="http://mirror.test/simple")
    assert not releases.RELEASE_ROOT.exists()


@pytest.mark.parametrize("active,symlinked", [(False, False), (True, False), (False, True)])
def test_incomplete_installation_is_preserved_before_retry(release, tmp_path, monkeypatch, active, symlinked):
    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / "installed"))
    deployment = Deployment("dev")
    target = deployment.root / "releases/candidate"
    target.mkdir(parents=True)
    if symlinked:
        target.rmdir()
        actual = tmp_path / "external"
        actual.mkdir()
        target.symlink_to(actual, target_is_directory=True)
    (target / "partial.txt").write_text("interrupted dependency install")
    old = deployment.root / "releases/old"
    old.mkdir()
    current = deployment.root / "current"
    current.symlink_to(target if active else old)
    manifest = json.loads((release / "release.json").read_text())
    manifest["agent_manifest_sha256"] = "a" * 64
    releases.atomic_json(release / "release.json", manifest)
    calls = []
    monkeypatch.setattr(releases, "extract_archive", lambda archive, destination: None)
    monkeypatch.setattr(releases, "run", lambda argv, **kwargs: calls.append(argv))
    if active or symlinked:
        with pytest.raises(RuntimeError, match="active or symlinked"):
            releases.prepare_server(deployment, "candidate")
        assert (target / "partial.txt").is_file()
        assert not calls
        assert not list(target.parent.glob(".incomplete-*"))
    else:
        assert releases.prepare_server(deployment, "candidate") == target
        backups = list(target.parent.glob(".incomplete-candidate-*/installation/partial.txt"))
        assert len(backups) == 1
        assert backups[0].read_text() == "interrupted dependency install"
        assert not (target / "partial.txt").exists()
        assert (target / "manifest.sha256").read_text().strip() == releases.digest(release / "release.json")
        assert current.resolve() == old
        assert calls
        count = len(calls)
        assert releases.prepare_server(deployment, "candidate") == target
        assert len(calls) == count


@pytest.mark.parametrize("index_url", [None, "https://mirror.test/a'b/simple"])
def test_agent_install_forwards_mirror_after_privilege_boundary(release, monkeypatch, index_url):
    from lerobot.data_platform import release_cli
    from lerobot.data_platform.deployment import AgentTarget

    monkeypatch.setattr(release_cli, "ssh_prefix", lambda: [])
    monkeypatch.setattr(release_cli, "agent_connection", lambda target: (target.host, []))
    calls = []
    monkeypatch.setattr(releases, "run", lambda argv, **kwargs: calls.append(argv))
    target = AgentTarget("host", "/unused", ("dev-node",))
    monkeypatch.setattr(release_cli, "agent_targets", lambda: [target])
    release_cli.install_agent(Deployment("dev"), "candidate", index_url=index_url)
    command = calls[-1][-1]
    line = next(line for line in command.splitlines() if "./install.sh --env dev" in line)
    expected = ["$@"]
    if index_url:
        expected += ["env", "UV_DEFAULT_INDEX=" + index_url]
    assert shlex.split(line) == expected + ["./install.sh", "--env", "dev"]


@pytest.mark.parametrize(
    "index_url",
    [
        "http://mirror.test/simple",
        "https:///simple",
        "https://user:secret@mirror.test/simple",
        "https://mirror.test/simple?token=secret",
        "https://mirror.test/simple#secret",
    ],
)
def test_unsafe_package_index_is_rejected_before_archiving(tmp_path, index_url):
    with pytest.raises(ValueError, match="Package index"):
        releases.build_release(tmp_path, "candidate", index_url=index_url)


def test_history_retention_keeps_running_version_and_numeric_recent_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / self.environment))
    deployment = Deployment("dev")
    parent = deployment.root / "releases"
    for version in ("V0.0.6.9_Old", "V0.0.6.10_Current", "V0.0.6.11_Recent", "V0.0.6.12_Newer"):
        path = parent / version
        path.mkdir(parents=True)
        (path / "manifest.sha256").write_text("manifest")
    active = parent / "V0.0.6.10_Current"
    (deployment.root / "current").symlink_to(active)
    incomplete = parent / ".incomplete-installation"
    incomplete.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (parent / "linked").symlink_to(external, target_is_directory=True)
    backups = deployment.root / "backups"
    for index in range(5):
        path = backups / str(index)
        path.mkdir(parents=True)
        (path / "backup.json").write_text(json.dumps({"complete": True, "environment": "dev"}))
        os.utime(path, (index, index))
    unfinished = backups / "unfinished"
    unfinished.mkdir()
    (unfinished / "backup.json").write_text(json.dumps({"complete": False, "environment": "dev"}))
    removed = releases.prune_deployment_history(deployment)
    assert str(parent / "V0.0.6.9_Old") in removed
    assert (deployment.root / "current").resolve() == active
    assert active.exists()
    assert (parent / "V0.0.6.11_Recent").exists()
    assert (parent / "V0.0.6.12_Newer").exists()
    assert incomplete.exists() and external.exists()
    assert {path.name for path in backups.iterdir()} == {"2", "3", "4", "unfinished"}


def test_pipeline_coordinator_unit_follows_release_and_legacy_rollback(tmp_path, monkeypatch):
    from lerobot.data_platform.deployment import coordinator_unit

    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / self.environment))
    deployment = Deployment("dev")
    old = deployment.root / "releases/old"
    new = deployment.root / "releases/new"
    old.mkdir(parents=True)
    module = new / "lerobot/data_platform/processing/coordinator.py"
    module.parent.mkdir(parents=True)
    module.touch()
    current = deployment.root / "current"
    current.symlink_to(new)
    assert deployment.coordinator_unit_name in deployment.units
    assert "ConditionPathExists=" in coordinator_unit(deployment)
    assert "DATA_PLATFORM_COORDINATOR_CURRENT=" in coordinator_unit(deployment)
    current.unlink()
    current.symlink_to(old)
    assert deployment.coordinator_unit_name not in deployment.units


def test_assistant_unit_pins_release_and_drains_before_legacy_rollback(tmp_path, monkeypatch):
    from lerobot.data_platform.deployment import assistant_unit, experience_unit

    monkeypatch.setattr(Deployment, "root", property(lambda self: tmp_path / self.environment))
    deployment = Deployment("dev")
    old = deployment.root / "releases/old"
    new = deployment.root / "releases/new"
    old.mkdir(parents=True)
    module = new / "lerobot/data_platform/ai_tools/runner.py"
    module.parent.mkdir(parents=True)
    module.touch()
    module.with_name("experience_worker.py").touch()
    current = deployment.root / "current"
    current.symlink_to(new)
    assert deployment.assistant_unit_name in deployment.units
    unit = assistant_unit(deployment)
    assert f"ExecStart={new}/.venv/bin/python -m lerobot.data_platform.ai_tools.runner" in unit
    assert "TimeoutStopSec=100" in unit and "KillMode=mixed" in unit
    assert "ProtectSystem=strict" in unit and "InaccessiblePaths=" in unit
    background = experience_unit(deployment)
    assert deployment.experience_unit_name in deployment.units
    assert "ai_tools.experience_worker" in background and "TimeoutStopSec=260" in background
    assert f"EnvironmentFile={deployment.config}" in background
    current.unlink()
    current.symlink_to(old)
    assert deployment.assistant_unit_name not in deployment.units
    assert deployment.experience_unit_name not in deployment.units


def test_browser_gate_rejects_skips_and_empty_reports(tmp_path):
    from lerobot.data_platform.browser_validation import REQUIRED_CASES, check_browser_report

    report = tmp_path / "browser.xml"
    for contents in (
        "<testsuite/>",
        "<testsuite>"
        + "".join(f'<testcase name="{name}"><skipped/></testcase>' for name in REQUIRED_CASES)
        + "</testsuite>",
    ):
        report.write_text(contents)
        with pytest.raises(RuntimeError):
            check_browser_report(report)


def test_browser_gate_preserves_explicit_fixture_assets(tmp_path, monkeypatch):
    from lerobot.data_platform.browser_validation import REQUIRED_CASES, run_browser_validation

    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("ui-tailwind.js", "ui-alpine.js"):
        (assets / name).write_text("/* deterministic fixture */")
    executable = tmp_path / "chrome"
    executable.touch()
    executable.chmod(0o700)
    monkeypatch.setenv("DATA_PLATFORM_BROWSER_ASSETS", str(assets))

    def runner(command, **kwargs):
        assert kwargs["env"]["DATA_PLATFORM_BROWSER_ASSETS"] == str(assets)
        report = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        report.write_text(
            "<testsuite>" + "".join(f'<testcase name="{name}"/>' for name in REQUIRED_CASES) + "</testsuite>"
        )

    result = run_browser_validation("python", tmp_path, executable, tmp_path, {}, runner)
    assert result["passed"] == len(REQUIRED_CASES)
    assert set(result["assets_sha256"]) == {"ui-tailwind.js", "ui-alpine.js"}

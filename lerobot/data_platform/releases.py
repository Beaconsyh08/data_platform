"""Build once, approve in development, and install the same verified release in production."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from lerobot.data_platform.deployment import Deployment, agent_targets, load_environment, server_unit

RELEASE_ROOT = Path("/var/lib/data-platform-releases")
SCHEMA_VERSION = 3
VALIDATION_TESTS = [
    "tests/datasets/test_data_manager.py",
    "tests/datasets/test_curation.py",
    "tests/datasets/test_curation_routes.py",
    "tests/datasets/test_execution_supervisor.py",
    "tests/datasets/test_environments.py",
    "tests/datasets/test_releases.py",
    "tests/datasets/test_deployment_progress.py",
    "tests/datasets/test_control_plane.py",
    "tests/datasets/test_job_management.py",
    "tests/datasets/test_data_platform_agent.py",
    "tests/datasets/test_management_ui.py",
]


def validate_version(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value):
        raise ValueError("Release names must contain 1-100 letters, digits, dots, underscores or dashes")
    return value


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def run(argv, **kwargs):
    return subprocess.run([str(arg) for arg in argv], check=True, **kwargs)


def extract_archive(archive: Path, destination: Path):
    """Release archives contain regular files/directories only; reject links and traversal."""
    with tarfile.open(archive) as stream:
        for entry in stream.getmembers():
            path = Path(entry.name)
            if path.is_absolute() or ".." in path.parts or not (entry.isfile() or entry.isdir()):
                raise ValueError("Unsafe release archive member")
        stream.extractall(destination)


def build_release(source: Path, version: str, *, index_url: str | None = None) -> Path:
    if index_url and not index_url.startswith("https://"):
        raise ValueError("Package index must use HTTPS")
    version = validate_version(version)
    source = source.resolve()
    status = run(
        ["git", "-c", f"safe.directory={source}", "status", "--porcelain", "--untracked-files=normal"],
        cwd=source,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RuntimeError("Commit the intended changes before building a release; the worktree is preserved")
    commit = run(
        ["git", "-c", f"safe.directory={source}", "rev-parse", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
    ).stdout.strip()
    final = RELEASE_ROOT / version
    if final.exists():
        raise FileExistsError("Release already exists; choose a new version")
    RELEASE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=RELEASE_ROOT) as temporary:
        root = Path(temporary)
        source_tar = root / "server.tar.gz"
        with source_tar.open("wb") as stream:
            run(
                ["git", "-c", f"safe.directory={source}", "archive", "--format=tar.gz", commit],
                cwd=source,
                stdout=stream,
            )
        snapshot = root / "source"
        snapshot.mkdir()
        extract_archive(source_tar, snapshot)
        if not (snapshot / "uv.lock").is_file():
            raise RuntimeError(
                "The committed source archive is missing uv.lock. Track and commit uv.lock with pyproject.toml; "
                "ensure it is not excluded by .gitignore or .gitattributes, then rebuild the release."
            )
        python = source / ".venv/bin/python"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DATA_PLATFORM_", "DASHSCOPE_"))
        }
        if index_url:
            env["UV_DEFAULT_INDEX"] = index_url
        env["PYTHONPATH"] = str(snapshot)
        for key in ("HF_HOME", "HF_DATASETS_CACHE", "HF_LEROBOT_HOME", "UV_CACHE_DIR"):
            env[key] = str(root / "test-cache" / key)
        with (root / "validation.txt").open("w") as report:
            run(
                [python, "-m", "pytest", *VALIDATION_TESTS, "-q"],
                cwd=snapshot,
                env=env,
                stdout=report,
                stderr=subprocess.STDOUT,
            )
        run(
            [
                python,
                snapshot / "scripts/build_data_platform_agent_bundle.py",
                "--version",
                version,
                "--output-dir",
                root / "agent",
            ],
            cwd=snapshot,
            env=env,
        )
        agent = next((root / "agent").glob("*.tar.gz"))
        with tarfile.open(agent) as bundle:
            internal_manifest = bundle.extractfile(agent.name.removesuffix(".tar.gz") + "/manifest.sha256")
            agent_manifest_sha256 = hashlib.sha256(internal_manifest.read()).hexdigest()
        shutil.move(agent, root / agent.name)
        shutil.move(Path(str(agent) + ".sha256"), root / (agent.name + ".sha256"))
        files = {name: digest(root / name) for name in ("server.tar.gz", agent.name, "validation.txt")}
        atomic_json(
            root / "release.json",
            {
                "format": 1,
                "release": version,
                "commit": commit,
                "created_at": time.time(),
                "files": files,
                "agent_archive": agent.name,
                "agent_manifest_sha256": agent_manifest_sha256,
                "schema_min": SCHEMA_VERSION,
                "schema_max": SCHEMA_VERSION,
                "job_protocol": 2,
                "tests": VALIDATION_TESTS,
            },
        )
        shutil.rmtree(snapshot)
        shutil.rmtree(root / "agent")
        shutil.rmtree(root / "test-cache", ignore_errors=True)
        shutil.copytree(root, final)
    # Keep the existing administrator-facing Agent artifact location.
    agent_dir = source / "dist/agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".sha256"):
        target = agent_dir / (agent.name + suffix)
        if target.exists() and digest(target) != digest(final / target.name):
            raise FileExistsError("A different Agent package already exists in dist/agent")
        shutil.copy2(final / target.name, target)
    return final


def verify_release(version: str, *, production: bool = False) -> tuple[Path, dict]:
    root = RELEASE_ROOT / validate_version(version)
    manifest = json.loads((root / "release.json").read_text())
    if manifest.get("format") != 1 or manifest.get("release") != version:
        raise ValueError("Invalid release manifest")
    if not {"server.tar.gz", "validation.txt", manifest.get("agent_archive")} <= manifest["files"].keys():
        raise ValueError("Incomplete release manifest")
    for name, expected in manifest["files"].items():
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
            or (root / name).is_symlink()
            or digest(root / name) != expected
        ):
            raise ValueError("Release checksum mismatch")
    if production:
        receipt = json.loads((root / "approval.json").read_text())
        if receipt.get("environment") != "dev" or receipt.get("manifest_sha256") != digest(
            root / "release.json"
        ):
            raise ValueError("Production requires approval of these exact artifacts in development")
    return root, manifest


def health(deployment: Deployment, version: str) -> dict:
    import requests

    response = requests.get(f"http://127.0.0.1:{deployment.port}/healthz", timeout=5)
    response.raise_for_status()
    result = response.json()
    if (result.get("environment"), result.get("instance_id"), result.get("release")) != (
        deployment.environment,
        os.environ["DATA_PLATFORM_INSTANCE_ID"],
        version,
    ):
        raise RuntimeError("Running server identity/version does not match deployment")
    return result


def check_nodes(store, version: str, *, required_names: list[str]):
    from datetime import datetime, timezone

    nodes = {node["name"]: node for node in store.list_nodes()}
    if not required_names:
        raise RuntimeError("Configure DATA_PLATFORM_DEPLOY_AGENT_NAMES before release acceptance")
    for name in required_names:
        node = nodes.get(name)
        caps = node.get("capabilities", {}) if node else {}
        seen = datetime.fromisoformat(node["last_seen_at"]) if node and node.get("last_seen_at") else None
        if (
            not seen
            or (datetime.now(timezone.utc) - seen).total_seconds() > 60
            or caps.get("release") != version
            or caps.get("environment") != os.environ["DATA_PLATFORM_ENV"]
            or caps.get("instance_id") != os.environ["DATA_PLATFORM_INSTANCE_ID"]
            or caps.get("job_protocol", 0) < 2
        ):
            raise RuntimeError(
                f"Node {name} does not have a fresh heartbeat for the expected release/environment"
            )


def check_release_acceptance(deployment: Deployment, version: str):
    if deployment.environment != "dev":
        raise ValueError("Only development can approve a release")
    root, _ = verify_release(version)
    load_environment(deployment)
    from lerobot.data_platform.control_plane import ControlPlaneStore
    from lerobot.data_platform.environment import check_server_environment

    check_server_environment()
    store = ControlPlaneStore(os.environ["DATA_PLATFORM_DATABASE_URL"])
    if health(deployment, version).get("maintenance"):
        raise RuntimeError("Development is under maintenance")
    run(["systemctl", "is-active", "--quiet", *deployment.units])
    check_nodes(
        store, version, required_names=[name for target in agent_targets() for name in target.node_names]
    )
    local_state = Path(os.environ["DATA_PLATFORM_OUTPUT_DIR"]) / "management/local-agent.json"
    if not local_state.is_file():
        raise RuntimeError("Run a representative local job before approving this release")
    check_nodes(store, version, required_names=[json.loads(local_state.read_text())["name"]])
    return root


def approve_release(
    deployment: Deployment, version: str, evidence: Path | dict, *, approved_by: dict | None = None
):
    report = json.loads(evidence.read_text()) if isinstance(evidence, Path) else evidence
    required = {
        "role_permissions",
        "environment_isolation",
        "local_job",
        "remote_viewer",
        "remote_preprocess",
        "rollback_drill",
    }
    if (
        not isinstance(report, dict)
        or report.get("release") != version
        or any(report.get(key) is not True for key in required)
    ):
        raise ValueError("Manual acceptance must identify this release and pass every required scenario")
    root = check_release_acceptance(deployment, version)
    atomic_json(
        root / "approval.json",
        {
            "environment": "dev",
            "instance_id": os.environ["DATA_PLATFORM_INSTANCE_ID"],
            "manifest_sha256": digest(root / "release.json"),
            "approved_at": time.time(),
            "approved_by": approved_by or os.environ.get("SUDO_USER", str(os.getuid())),
            "evidence": report,
        },
    )


def switch_current(deployment: Deployment, release: Path):
    pointer = deployment.root / ".current-next"
    pointer.unlink(missing_ok=True)
    pointer.symlink_to(release)
    pointer.replace(deployment.root / "current")


def prepare_server(
    deployment: Deployment, version: str, *, index_url: str | None = None, hard: bool = False
) -> Path:
    root, manifest = verify_release(version, production=deployment.environment == "prod" and not hard)
    target = deployment.root / "releases" / version
    if target.exists():
        receipt = target / "manifest.sha256"
        if receipt.is_file():
            if receipt.read_text().strip() != digest(root / "release.json"):
                raise ValueError("Installed version has a different manifest")
            return target
        if target.is_symlink() or (deployment.root / "current").resolve() == target.resolve():
            raise RuntimeError("Incomplete installation is active or symlinked; inspect it before recovery")
        backup = Path(tempfile.mkdtemp(prefix=f".incomplete-{version}-", dir=target.parent))
        target.rename(backup / "installation")
        print(f"Preserved incomplete installation at {backup / 'installation'}; preparing release again")
    target.mkdir(parents=True)
    try:
        extract_archive(root / "server.tar.gz", target)
        sync = [
            "uv",
            "sync",
            "--project",
            target,
            "--extra",
            "data-platform-server",
            "--frozen",
            "--python",
            "/usr/bin/python3.10",
        ]
        if index_url:
            if not index_url.startswith("https://"):
                raise ValueError("Package index must use HTTPS")
            sync += ["--default-index", index_url]
        try:
            run(
                [*sync, "--offline"],
                env=dict(os.environ, UV_CACHE_DIR=f"/var/cache/data-platform-uv/{deployment.environment}"),
            )
        except subprocess.CalledProcessError:
            run(
                sync,
                env=dict(os.environ, UV_CACHE_DIR=f"/var/cache/data-platform-uv/{deployment.environment}"),
            )
        run(
            [
                "runuser",
                "-u",
                deployment.user,
                "--",
                target / ".venv/bin/python",
                "-c",
                "import gunicorn, pymysql; from lerobot.data_platform.wsgi import create_app",
            ],
            cwd=target,
        )
        (target / "release.env").write_text(
            f"DATA_PLATFORM_RELEASE={version}\nDATA_PLATFORM_AGENT_MANIFEST_SHA256={manifest['agent_manifest_sha256']}\n"
        )
        (target / "manifest.sha256").write_text(digest(root / "release.json") + "\n")
    except BaseException:
        shutil.rmtree(target)
        raise
    return target


def install_server_units(deployment: Deployment):
    for unit, worker in zip(deployment.units, (False, True), strict=True):
        Path("/etc/systemd/system", unit).write_text(server_unit(deployment, worker=worker))
    if deployment.environment == "dev":
        Path("/etc/systemd/system/data-platform-dev.slice").write_text(
            "[Slice]\nCPUQuota=400%\nMemoryMax=8G\nCPUWeight=10\nIOWeight=10\n"
        )
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", *deployment.units])


def wait_healthy(deployment: Deployment, version: str):
    for _ in range(30):
        try:
            result = health(deployment, version)
            run(["systemctl", "is-active", "--quiet", *deployment.units])
            return result
        except (RuntimeError, subprocess.CalledProcessError, OSError):
            time.sleep(1)
        except Exception:
            time.sleep(1)
    raise RuntimeError("Deployment did not become healthy; maintenance remains enabled")

#!/usr/bin/env python3
"""Build a versioned, source-checkout-free Data Platform agent bundle."""

from __future__ import annotations

import argparse
import hashlib
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_TEMPLATE = ROOT / "deploy" / "data-platform" / "agent-bundle" / "install.sh"
SERVICE_TEMPLATE = ROOT / "deploy" / "data-platform" / "data-platform-agent.service"


def _project_version() -> str:
    match = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
    if not match:
        raise RuntimeError("project version is missing from pyproject.toml")
    return match.group(1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, quiet: bool = False) -> None:
    stdout = subprocess.DEVNULL if quiet else None
    subprocess.run(command, cwd=ROOT, check=True, stdout=stdout)


def _normalized_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def build_bundle(output_dir: Path, *, version: str) -> tuple[Path, Path]:
    if platform.system().lower() != "linux":
        raise RuntimeError("the systemd agent bundle must be built on Linux")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to build the agent bundle")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("bundle version may only contain letters, numbers, dots, underscores, and dashes")

    bundle_platform = "linux"
    bundle_arch = _normalized_arch()
    bundle_name = f"data-platform-agent-{version}-{bundle_platform}-{bundle_arch}"
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{bundle_name}.tar.gz"
    checksum = output_dir / f"{archive.name}.sha256"
    if archive.exists() or checksum.exists():
        raise FileExistsError(f"bundle output already exists: {archive}")

    with tempfile.TemporaryDirectory(prefix="data-platform-agent-build-") as temporary:
        staging_parent = Path(temporary)
        staging = staging_parent / bundle_name
        wheels = staging / "wheels"
        (staging / "bin").mkdir(parents=True)
        (staging / "systemd").mkdir()
        wheels.mkdir()

        _run([uv, "build", "--wheel", "--out-dir", str(wheels)])
        (wheels / ".gitignore").unlink(missing_ok=True)
        wheel_files = list(wheels.glob("*.whl"))
        if len(wheel_files) != 1:
            raise RuntimeError(f"expected one wheel, found {len(wheel_files)}")
        wheel = wheel_files[0]
        with zipfile.ZipFile(wheel) as wheel_zip:
            required_modules = {
                "lerobot/data_platform/agent.py",
                "lerobot/data_platform/curation.py",
                "lerobot/data_platform/curation_execution.py",
                "lerobot/data_platform/routes/curation.py",
                "lerobot/data_platform/templates/curation.js",
                "lerobot/data_platform/templates/visualize_dataset_curation.html",
                "lerobot/data_platform/environment.py",
                "lerobot/data_platform/deployment.py",
                "lerobot/data_platform/agent_install.py",
                "lerobot/data_platform/precompute/preprocess/dataset_merge.py",
                "lerobot/data_platform/precompute/preprocess/merge_alignment.py",
                "lerobot/data_platform/precompute/preprocess/v3_native.py",
                "lerobot/data_platform/precompute/data_profile.py",
                "lerobot/data_platform/precompute/signal_columns.py",
            }
            missing = required_modules - set(wheel_zip.namelist())
            if missing:
                raise RuntimeError(f"built wheel is missing Agent modules: {sorted(missing)}")

        requirements = staging / "requirements.lock"
        _run(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--extra",
                "data-platform-agent",
                "--no-emit-project",
                "--no-editable",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            quiet=True,
        )
        shutil.copy2(Path(uv).resolve(), staging / "bin" / "uv")
        shutil.copy2(SERVICE_TEMPLATE, staging / "systemd" / SERVICE_TEMPLATE.name)

        installer = INSTALLER_TEMPLATE.read_text()
        installer = installer.replace("@BUNDLE_VERSION@", version)
        installer = installer.replace("@BUNDLE_PLATFORM@", bundle_platform)
        installer = installer.replace("@BUNDLE_ARCH@", bundle_arch)
        installer = installer.replace("@WHEEL_NAME@", wheel.name)
        install_path = staging / "install.sh"
        install_path.write_text(installer)
        install_path.chmod(0o755)

        manifest_lines = []
        for path in sorted(item for item in staging.rglob("*") if item.is_file()):
            relative = path.relative_to(staging)
            manifest_lines.append(f"{_sha256(path)}  {relative.as_posix()}")
        (staging / "manifest.sha256").write_text("\n".join(manifest_lines) + "\n")

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname=bundle_name)

    checksum.write_text(f"{_sha256(archive)}  {archive.name}\n")
    return archive, checksum


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", default=_project_version())
    args = parser.parse_args()
    archive, checksum = build_bundle(args.output_dir, version=args.version)
    print(archive)
    print(checksum)


if __name__ == "__main__":
    main()

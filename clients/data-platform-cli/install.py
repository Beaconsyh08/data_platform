#!/usr/bin/env python3
"""Install the published CLI and trusted platform CA without administrator privileges."""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
BASE_URL = "https://algorithm-vla-cli-downloads.oss-cn-beijing.aliyuncs.com/cli/0.1.0"
FILES = {
    "data_platform_cli-0.1.0-py3-none-any.whl": "0946224482a1af58c7d0aaec2a21716b94ad9139f36a76d1668e765bbb63a838",
    "data-platform-ca.crt": "03bb065cae8c77a02af2f3d2bfec97ecb4be0c8a913d8b5934261be43343b776",
}
LAUNCHER_MARKER = "# Data Platform CLI managed launcher"


def download(name: str, directory: Path) -> Path:
    """Only accept the small, exact release files over verified HTTPS."""
    request = urllib.request.Request(f"{BASE_URL}/{name}", headers={"User-Agent": "DataPlatformInstaller/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.url != request.full_url:
            raise RuntimeError("Unexpected download redirect")
        content = response.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024 or hashlib.sha256(content).hexdigest() != FILES[name]:
        raise RuntimeError(f"Release checksum mismatch: {name}")
    target = directory / name
    target.write_bytes(content)
    return target


def install(prefix: Path, bin_dir: Path) -> Path:
    prefix = prefix.expanduser().absolute()
    bin_dir = bin_dir.expanduser().absolute()
    target = prefix / VERSION
    launcher = bin_dir / "data-platform"
    if launcher.is_symlink() or (
        launcher.exists() and LAUNCHER_MARKER not in launcher.read_text(errors="replace")
    ):
        raise RuntimeError(f"Existing unrelated launcher; select another --bin-dir: {launcher}")
    prefix.mkdir(parents=True, exist_ok=True)
    lock = prefix / ".install.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise RuntimeError(f"Another installation is active; inspect {lock} before retrying") from None
    created = False
    try:
        marker = target / "data-platform-install.json"
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != FILES:
                raise RuntimeError(f"Existing unrecognized installation; select another --prefix: {target}")
            ca = target / "data-platform-ca.crt"
            if hashlib.sha256(ca.read_bytes()).hexdigest() != FILES[ca.name]:
                raise RuntimeError("Installed CA checksum mismatch")
        else:
            with tempfile.TemporaryDirectory(prefix="data-platform-download-") as temporary:
                downloads = Path(temporary)
                wheel, ca = [download(name, downloads) for name in FILES]
                target.mkdir()
                created = True
                subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)
                subprocess.run(
                    [
                        str(target / "bin/python"),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        str(wheel),
                    ],
                    check=True,
                )
                shutil.copyfile(ca, target / ca.name)
                subprocess.run(
                    [str(target / "bin/data-platform"), "--help"], check=True, stdout=subprocess.DEVNULL
                )
                marker.write_text(json.dumps(FILES, sort_keys=True) + "\n")
        bin_dir.mkdir(parents=True, exist_ok=True)
        content = (
            "#!/bin/sh\n"
            f"{LAUNCHER_MARKER}\n"
            f"exec {shlex.quote(str(target / 'bin/data-platform'))}"
            " --server https://10.8.8.40"
            f" --ca-bundle {shlex.quote(str(target / 'data-platform-ca.crt'))}"
            ' "$@"\n'
        )
        descriptor, temporary_launcher = tempfile.mkstemp(prefix=".data-platform-", dir=bin_dir)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(content)
            os.chmod(temporary_launcher, 0o755)
            os.replace(temporary_launcher, launcher)
        finally:
            Path(temporary_launcher).unlink(missing_ok=True)
        return launcher
    except BaseException:
        if created:
            shutil.rmtree(target)
        raise
    finally:
        lock.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/data-platform-cli")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--login", action="store_true", help="Open the production login after installation")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or later is required")
    if os.geteuid() == 0:
        parser.error("Run as your normal account, without sudo")
    try:
        launcher = install(args.prefix, args.bin_dir)
    except Exception as error:
        print(
            f"Installation failed ({type(error).__name__}). No login credentials were requested.",
            file=sys.stderr,
        )
        print(
            "Check Python venv support, HTTPS access, and write access to the installation directory.",
            file=sys.stderr,
        )
        return 1
    print(f"Installed Data Platform CLI {VERSION}. Login: {shlex.quote(str(launcher))}", flush=True)
    if args.login:
        os.execv(str(launcher), [str(launcher)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

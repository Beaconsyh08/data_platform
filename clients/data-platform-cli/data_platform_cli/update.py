"""Update an installer-managed client using the official HTTPS distribution entry."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from data_platform_cli import __version__

INSTALLER_URL = "https://algorithm-vla-cli-downloads.oss-cn-beijing.aliyuncs.com/cli/latest/install.py"


def update() -> int:
    target = Path(sys.prefix)
    bin_dir = os.environ.get("DATA_PLATFORM_CLI_BIN_DIR", "")
    if not (target / "data-platform-install.json").is_file() or not Path(bin_dir).is_absolute():
        print("Run the official latest installer once, then use its data-platform launcher to update.")
        return 1
    try:
        request = urllib.request.Request(INSTALLER_URL, headers={"User-Agent": "DataPlatformUpdater/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.url != INSTALLER_URL:
                raise ValueError("Unexpected installer redirect")
            content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Installer exceeds size limit")
        match = re.search(rb'^VERSION = "(\d+\.\d+\.\d+)"$', content, re.MULTILINE)
        if not match:
            raise ValueError("Invalid installer version")
        version = match[1].decode("ascii")
        if tuple(map(int, version.split("."))) <= tuple(map(int, __version__.split("."))):
            print(f"Already up to date (Data Platform CLI {__version__}).")
            return 0
        print(f"Updating Data Platform CLI {__version__} → {version}…", flush=True)
        with tempfile.TemporaryDirectory(prefix="data-platform-update-") as directory:
            installer = Path(directory) / "install.py"
            installer.write_bytes(content)
            subprocess.run(
                [sys.executable, str(installer), "--prefix", str(target.parent), "--bin-dir", bin_dir],
                check=True,
            )
        print(f"Updated to {version}. Restart data-platform to use the new version.")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("Update failed. Check HTTPS access and installation permissions, then retry.", file=sys.stderr)
        return 1

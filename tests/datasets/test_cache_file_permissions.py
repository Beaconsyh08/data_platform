import os
import shutil
import stat
import subprocess
import sys

import pytest


def create_cache(path, mask):
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sys
from pathlib import Path
from lerobot.data_platform.precompute.video import temporary_output_path
os.umask(int(sys.argv[2], 8))
target = Path(sys.argv[1])
temporary = temporary_output_path(target)
assert temporary.suffix == target.suffix
temporary.write_text('cache')
temporary.replace(target)
""",
            str(path),
            mask,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_atomic_cache_file_honors_service_umask(tmp_path):
    target = tmp_path / "episode.csv"
    create_cache(target, "027")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert target.read_text() == "cache"


def test_atomic_cache_file_keeps_inherited_acl_effective(tmp_path):
    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("POSIX ACL tools are required")
    other_uid = os.getuid() + 10000
    subprocess.run(["setfacl", "-m", f"d:u:{other_uid}:rwx", str(tmp_path)], check=True)
    target = tmp_path / "episode.csv"
    create_cache(target, "077")
    acl = subprocess.check_output(["getfacl", "-c", "-n", str(target)], text=True)
    assert f"user:{other_uid}:rwx\t#effective:rw-" in acl
    assert "mask::rw-" in acl

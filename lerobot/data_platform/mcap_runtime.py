"""Install the pinned v3 importer overlay without replacing the platform's LeRobot package."""

from __future__ import annotations

import argparse
import os
import shutil
import site
import subprocess
import sys
from pathlib import Path


def install(destination: Path, uv: str, index_url: str | None = None):
    destination = destination.resolve()
    if destination == Path(sys.prefix).resolve():
        raise ValueError("The importer environment must be separate from the platform environment")
    if not (destination / "bin/python").is_file():
        subprocess.run([uv, "venv", "--python", sys.executable, str(destination)], check=True)
    python = destination / "bin/python"
    target_site = subprocess.check_output(
        [str(python), "-c", "import site; print(site.getsitepackages()[0])"], text=True
    ).strip()
    # Reuse the immutable release's heavy Torch/CUDA stack, with overlay packages taking precedence.
    (Path(target_site) / "platform_dependencies.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    args = [uv, "pip", "install", "--python", str(python)]
    if index_url:
        args += ["--index-url", index_url]
    subprocess.run(
        args
        + [
            "--no-deps",
            "lerobot==0.4.4",
            "av==15.1.0",
            "datasets==4.1.1",
            "mcap==1.2.2",
            "mcap-protobuf-support==0.5.3",
            "protobuf==5.28.3",
            "zstandard==0.23.0",
            "lz4==4.4.4",
            "accelerate==1.10.1",
            "scipy==1.15.3",
            "numpy==2.2.6",
            "opencv-python-headless==4.11.0.86",
        ],
        check=True,
    )
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    subprocess.run(
        [str(python), "-s", str(Path(__file__).with_name("mcap_worker.py")), "--check"],
        env=env,
        cwd=destination,
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--uv", default=shutil.which("uv"))
    parser.add_argument("--index-url")
    args = parser.parse_args()
    if not args.uv:
        parser.error("uv is required")
    install(args.destination, args.uv, args.index_url)


if __name__ == "__main__":
    main()

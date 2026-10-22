"""Run acceptance against an ephemeral socket-only MySQL owned by this process."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pymysql


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-only", action="store_true", help="Skip platform integration dependencies")
    parser.add_argument("--runtime", action="store_true", help="Test the integrated API and Runner instead")
    args = parser.parse_args()
    mysqld = shutil.which("mysqld")
    if mysqld is None:
        parser.error("MySQL 8.0.19+ mysqld must be installed; this command does not install services")
    source = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="assistant-graph-") as temporary:
        root = Path(temporary)
        data, socket = root / "data", root / "mysql.sock"
        data.mkdir()
        initialized = subprocess.run(
            [mysqld, "--no-defaults", "--initialize-insecure", f"--datadir={data}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if initialized.returncode:
            print(initialized.stderr, file=sys.stderr)
            return initialized.returncode
        with (root / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    mysqld,
                    "--no-defaults",
                    f"--datadir={data}",
                    f"--socket={socket}",
                    f"--pid-file={root / 'mysql.pid'}",
                    "--skip-networking",
                    "--mysqlx=OFF",
                ],
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        with pymysql.connect(unix_socket=str(socket), user="root"):
                            break
                    except pymysql.OperationalError:
                        time.sleep(0.1)
                else:
                    log.seek(0)
                    print(log.read(), file=sys.stderr)
                    raise RuntimeError("Private MySQL did not start")
                env = dict(os.environ)
                for name in tuple(env):
                    if name.startswith(("DATA_PLATFORM_", "DASHSCOPE_", "QWEN_", "OPENAI_", "LANGSMITH_")):
                        env.pop(name)
                env.update(
                    ASSISTANT_TEST_MYSQL_SOCKET=str(socket),
                    LANGCHAIN_TRACING_V2="false",
                    LANGSMITH_TRACING="false",
                    PYTHONDONTWRITEBYTECODE="1",
                )
                tests = ["tests/datasets/test_assistant_graph_mysql.py"]
                if not args.storage_only:
                    tests.append("tests/datasets/test_assistant_graph_s3.py")
                if args.runtime:
                    tests = [
                        "tests/datasets/test_ai_assistant_runtime.py",
                        "tests/datasets/test_ai_memory_followups.py",
                    ]
                return subprocess.run(
                    [sys.executable, "-m", "pytest", *tests, "--confcutdir=tests/datasets", "-q"],
                    cwd=source,
                    env=env,
                    check=False,
                ).returncode
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())

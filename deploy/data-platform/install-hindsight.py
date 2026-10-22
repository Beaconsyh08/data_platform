"""Provision a private per-environment Hindsight sidecar; run as root before platform installation.

PostgreSQL binaries must be an explicitly supplied, verified distribution including pgvector.
PostgreSQL accepts only peer-authenticated private Unix sockets. No production data is migrated.
"""

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lerobot.data_platform.deployment import Deployment, read_environment_file  # noqa: E402


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def write(path, text, *, mode=0o644, uid=0, gid=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    temporary.chmod(mode)
    os.chown(temporary, uid, gid)
    temporary.replace(path)


def environment(values):
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Multiline environment values are not supported")
    return "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--postgres-root", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--index-url", default="https://pypi.tuna.tsinghua.edu.cn/simple")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo; secrets are read from the existing protected server.env")
    with Path("/run/data-platform-hindsight-install.lock").open("w") as install_lock:
        fcntl.flock(install_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install(args)


def install(args):
    from lerobot.data_platform.releases import validate_package_index

    validate_package_index(args.index_url)
    deployment = Deployment(args.env)
    values = read_environment_file(deployment.config)
    if values.get("DATA_PLATFORM_ENV") != args.env or not values.get("DASHSCOPE_API_KEY"):
        raise ValueError("Verify the selected environment and its existing Qwen configuration")
    user = pwd.getpwnam(deployment.user)
    native = args.postgres_root.resolve(strict=True)
    for relative in [
        "bin/postgres",
        "bin/initdb",
        "bin/createdb",
        "bin/psql",
        "lib/vector.so",
        "share/extension/vector.control",
    ]:
        if not (native / relative).is_file():
            raise ValueError("PostgreSQL distribution must include server tools and pgvector")
    if any(p.is_symlink() and not p.resolve().is_relative_to(native) for p in native.rglob("*")):
        raise ValueError("PostgreSQL distribution contains an external symlink")
    manifest = {
        str(p.relative_to(native)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(native.rglob("*"))
        if p.is_file()
    }
    native_digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    root = Path("/opt/data-platform-hindsight")
    postgres = root / ("postgres-" + native_digest[:16])
    if not postgres.exists():
        shutil.copytree(native, postgres, symlinks=True)
    for relative, expected in manifest.items():
        if hashlib.sha256((postgres / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Installed PostgreSQL distribution differs from the verified input")
    runtime = root / "api-1.1.0"
    lock = Path(__file__).with_name("hindsight-requirements.lock")
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
    marker = runtime / "requirements.sha256"
    if marker.exists() and marker.read_text().strip() != lock_hash:
        raise ValueError("Existing Hindsight runtime has a different dependency lock")
    if not marker.exists():
        runtime.mkdir(parents=True, exist_ok=True)
        managed = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(root / "python")}
        run(args.uv, "python", "install", "3.11", env=managed)
        if not (runtime / ".venv/bin/python").exists():
            run(args.uv, "venv", "--python", "3.11", runtime / ".venv", env=managed)
        run(
            args.uv,
            "pip",
            "install",
            "--python",
            runtime / ".venv/bin/python",
            "-r",
            lock,
            "--index-url",
            args.index_url,
            env=managed,
        )
        run(args.uv, "pip", "check", "--python", runtime / ".venv/bin/python", env=managed)
        marker.write_text(lock_hash + "\n")
    state = Path("/var/lib/data-platform-hindsight") / args.env
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(state, user.pw_uid, user.pw_gid)
    state.chmod(0o700)
    data = state / "postgres"
    if not data.exists():
        run(
            "runuser",
            "-u",
            deployment.user,
            "--",
            postgres / "bin/initdb",
            "-D",
            data,
            "--username",
            deployment.user,
            "--auth-local=peer",
            "--auth-host=reject",
            "--encoding=UTF8",
            "--locale=C",
        )
    socket = f"/run/data-platform-hindsight-{args.env}"
    pg_unit = f"data-platform-hindsight-db@{args.env}.service"
    api_unit = f"data-platform-hindsight@{args.env}.service"
    existing_unit = Path("/etc/systemd/system") / pg_unit
    if existing_unit.exists() and f"ExecStart={postgres}/bin/postgres " not in existing_unit.read_text():
        raise ValueError("Existing PostgreSQL uses another distribution; prepare a reviewed database upgrade")
    common = (
        f"User={deployment.user}\nGroup={deployment.user}\nUMask=0077\n"
        "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\nRestart=on-failure\n"
    )
    write(
        Path("/etc/systemd/system") / pg_unit,
        f"[Unit]\nDescription=Private Hindsight PostgreSQL ({args.env})\n\n[Service]\n{common}"
        f"RuntimeDirectory=data-platform-hindsight-{args.env}\nRuntimeDirectoryMode=0700\n"
        f"ReadWritePaths={state} {socket}\n"
        f"ExecStart={postgres}/bin/postgres -D {data} -k {socket} -c listen_addresses= -c unix_socket_permissions=0700\n"
        "TimeoutStopSec=60\nKillSignal=SIGINT\n\n[Install]\nWantedBy=multi-user.target\n",
    )
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", pg_unit)
    for _ in range(30):
        result = subprocess.run(
            [
                "runuser",
                "-u",
                deployment.user,
                "--",
                str(postgres / "bin/psql"),
                "-h",
                socket,
                "-d",
                "postgres",
                "-Atqc",
                "SELECT 1",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Private PostgreSQL did not start")
    existing = run(
        "runuser",
        "-u",
        deployment.user,
        "--",
        postgres / "bin/psql",
        "-h",
        socket,
        "-d",
        "postgres",
        "-Atqc",
        "SELECT 1 FROM pg_database WHERE datname='hindsight'",
        capture_output=True,
    )
    if not existing.stdout.strip():
        run("runuser", "-u", deployment.user, "--", postgres / "bin/createdb", "-h", socket, "hindsight")
    key = values.get("DATA_PLATFORM_HINDSIGHT_API_KEY") or secrets.token_urlsafe(32)
    port = 19911 if args.env == "dev" else 19912
    settings = {
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "LOG_LEVEL": "warning",
        "DATABASE_URL": "postgresql:///hindsight?" + urlencode({"host": socket, "user": deployment.user}),
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": values.get("DATA_PLATFORM_AI_MODEL", "qwen3.6-plus"),
        "LLM_API_KEY": values["DASHSCOPE_API_KEY"],
        "LLM_BASE_URL": values.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        "LLM_EXTRA_BODY": '{"enable_thinking":false}',
        "LLM_MAX_RETRIES": "0",
        "LLM_TIMEOUT": "45",
        "RETAIN_MAX_COMPLETION_TOKENS": "4096",
        "CONSOLIDATION_MAX_COMPLETION_TOKENS": "4096",
        "EMBEDDINGS_PROVIDER": "openai",
        "EMBEDDINGS_OPENAI_API_KEY": values["DASHSCOPE_API_KEY"],
        "EMBEDDINGS_OPENAI_BASE_URL": values.get(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "EMBEDDINGS_OPENAI_MODEL": "text-embedding-v4",
        "EMBEDDINGS_OPENAI_DIMENSIONS": "1024",
        "EMBEDDINGS_MAX_RETRIES": "0",
        "RERANKER_PROVIDER": "rrf",
        "ENABLE_AUTO_CONSOLIDATION": "false",
        "TENANT_EXTENSION": "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
        "TENANT_API_KEY": key,
    }
    api_config = deployment.config.with_name("hindsight.env")
    write(
        api_config,
        environment({"HINDSIGHT_API_" + k: v for k, v in settings.items()}),
        mode=0o640,
        gid=user.pw_gid,
    )
    write(
        Path("/etc/systemd/system") / api_unit,
        f"[Unit]\nDescription=Private Hindsight API ({args.env})\nAfter={pg_unit}\nRequires={pg_unit}\n\n"
        f"[Service]\n{common}EnvironmentFile={api_config}\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
        f"ReadWritePaths={state} {socket}\nWorkingDirectory={state}\n"
        f"ExecStart={runtime}/.venv/bin/hindsight-api\nTimeoutStopSec=90\n\n[Install]\nWantedBy=multi-user.target\n",
    )
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", api_unit)
    run("systemctl", "restart", api_unit)
    for _ in range(90):
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/default/banks", headers={"Authorization": "Bearer " + key}
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(1)
    else:
        raise RuntimeError("Authenticated Hindsight API did not become ready")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/default/banks", timeout=5):
            raise RuntimeError("Hindsight permits unauthenticated bank access")
    except urllib.error.HTTPError as exc:
        if exc.code not in {401, 403}:
            raise RuntimeError("Hindsight authentication check failed") from None
    updates = {
        "DATA_PLATFORM_HINDSIGHT_URL": f"http://127.0.0.1:{port}",
        "DATA_PLATFORM_HINDSIGHT_API_KEY": key,
        "DATA_PLATFORM_AI_EXPERIENCE_ENABLED": "1",
    }
    original = deployment.config.read_text()
    write(deployment.config.with_name(f"server.env.before-hindsight-{time.time_ns()}"), original, mode=0o600)
    kept = [line for line in original.splitlines() if line.strip().partition("=")[0] not in updates]
    info = deployment.config.stat()
    write(deployment.config, "\n".join(kept) + "\n" + environment(updates), mode=0o640, gid=info.st_gid)
    write(
        state / "installation.json",
        json.dumps(
            {
                "api_version": "1.1.0",
                "lock_sha256": lock_hash,
                "postgres_tree_sha256": native_digest,
                "environment": args.env,
            },
            indent=2,
        ),
        mode=0o600,
    )
    print(
        f"Hindsight {args.env} is provisioned. Install/validate the platform release before production promotion."
    )


if __name__ == "__main__":
    main()

"""Environment-specific deployment layout and safe configuration loading."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentTarget:
    host: str
    identity_file: str
    node_names: tuple[str, ...]


def agent_targets() -> list[AgentTarget]:
    raw = os.environ.get("DATA_PLATFORM_DEPLOY_AGENT_TARGETS")
    if raw:
        entries = json.loads(raw)
        if not isinstance(entries, list) or not entries:
            raise ValueError("Agent targets must be a nonempty JSON array")
        result = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("node_names"), list):
                raise ValueError("Each Agent target needs host, identity_file and node_names")
            result.append(AgentTarget(entry["host"], entry["identity_file"], tuple(entry["node_names"])))
    else:
        result = [
            AgentTarget(
                os.environ.get("DATA_PLATFORM_DEPLOY_AGENT_HOST", ""),
                os.environ.get("DATA_PLATFORM_DEPLOY_IDENTITY_FILE", ""),
                tuple(os.environ.get("DATA_PLATFORM_DEPLOY_AGENT_NAMES", "").split()),
            )
        ]
    names = [name for target in result for name in target.node_names]
    if (
        not names
        or any(not isinstance(name, str) or not name for name in names)
        or len(names) != len(set(names))
    ):
        raise ValueError("Configure unique Agent node names for this environment")
    return result


@dataclass(frozen=True)
class Deployment:
    environment: str

    def __post_init__(self):
        if self.environment not in {"dev", "prod"}:
            raise ValueError("Explicit --env dev|prod is required")

    @property
    def config(self) -> Path:
        return Path("/etc/data-platform") / self.environment / "server.env"

    @property
    def root(self) -> Path:
        return Path("/opt/data-platform") / self.environment

    @property
    def user(self) -> str:
        return "data-platform-dev" if self.environment == "dev" else "data-platform"

    @property
    def port(self) -> int:
        return 9092 if self.environment == "dev" else 9091

    @property
    def public_port(self) -> int:
        return 8443 if self.environment == "dev" else 443

    @property
    def units(self) -> list[str]:
        units = [f"data-platform-{kind}@{self.environment}.service" for kind in ("web", "local-worker")]
        if (self.root / "current/lerobot/data_platform/processing/coordinator.py").is_file():
            units.append(self.coordinator_unit_name)
        if (self.root / "current/lerobot/data_platform/ai_tools/runner.py").is_file():
            units.append(self.assistant_unit_name)
        return units

    @property
    def coordinator_unit_name(self) -> str:
        return f"data-platform-pipeline-coordinator@{self.environment}.service"

    @property
    def assistant_unit_name(self) -> str:
        return f"data-platform-assistant@{self.environment}.service"

    @property
    def maintenance_file(self) -> Path:
        return Path("/var/lib/data-platform-deployment") / f"{self.environment}-maintenance"


def read_environment_file(path: Path) -> dict[str, str]:
    """Read systemd-style assignments, without evaluating shell code or printing secrets."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        if (
            not separator
            or not key.startswith(("DATA_PLATFORM_", "DASHSCOPE_"))
            or not key.replace("_", "").isalnum()
        ):
            raise ValueError("Environment file contains an unsupported assignment")
        parsed = shlex.split(raw, comments=False)
        if len(parsed) > 1 or key in values:
            raise ValueError(
                "Environment values must be quoted when containing spaces; duplicate keys are invalid"
            )
        values[key] = parsed[0] if parsed else ""
    return values


def load_environment(deployment: Deployment) -> dict[str, str]:
    values = read_environment_file(deployment.config)
    if values.get("DATA_PLATFORM_ENV") != deployment.environment:
        raise ValueError("Selected deployment does not match its environment file")
    for key in list(os.environ):
        if key.startswith(("DATA_PLATFORM_", "DASHSCOPE_")):
            del os.environ[key]
    os.environ.update(values)
    expected = f"http://127.0.0.1:{deployment.port}"
    if values.get("DATA_PLATFORM_LOCAL_SERVER_URL") != expected:
        raise ValueError("Local executor URL does not match the selected environment")
    return values


def safe_cli(main):
    """Do not expose driver connection strings or SQL parameter dumps in command failures."""
    import sys

    from lerobot.data_platform.operation_log import sanitize_for_log

    try:
        main()
    except Exception as exc:
        message = (
            sanitize_for_log(str(exc))
            if isinstance(exc, (ValueError, RuntimeError, FileNotFoundError, FileExistsError))
            else type(exc).__name__
        )
        print(
            f"Deployment stopped: {message}. Check the selected environment's deployment status; no automatic recovery was attempted.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def server_unit(deployment: Deployment, *, worker: bool, release: Path | None = None) -> str:
    root = release or deployment.root / "current"
    command = (
        f"{root}/.venv/bin/python -m lerobot.data_platform.local_execution"
        if worker
        else f'{root}/.venv/bin/gunicorn --workers 1 --threads 8 --timeout 3600 --bind 127.0.0.1:{deployment.port} "lerobot.data_platform.wsgi:create_app()"'
    )
    compute = (
        "Delegate=yes\nKillMode=mixed\nTimeoutStopSec=60\nEnvironment=DATA_PLATFORM_REQUIRE_CGROUP=1\n"
        if worker
        else ""
    )
    if worker and release is not None:
        compute += (
            f"Environment=DATA_PLATFORM_EXECUTOR_RELEASE={release}\n"
            f"Environment=DATA_PLATFORM_EXECUTOR_CURRENT={deployment.root / 'current'}\n"
        )
    dev = (
        "Slice=data-platform-dev.slice\nCPUWeight=10\nIOWeight=10\nProtectSystem=strict\n"
        "ReadWritePaths=/srv/data-platform-dev\n"
        "InaccessiblePaths=-/etc/data-platform/prod -/etc/data-platform/server.env -/srv/data-platform\n"
        if deployment.environment == "dev"
        else ""
    )
    return f"""[Unit]
Description=Data Platform {deployment.environment} {"executor" if worker else "web"}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={deployment.user}
Group={deployment.user}
WorkingDirectory={root}
EnvironmentFile={deployment.config}
EnvironmentFile={root}/release.env
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=HF_HOME=/srv/{"data-platform-dev" if deployment.environment == "dev" else "data-platform"}/model-cache
ExecStart={command}
Restart={"on-failure" if worker and release is not None else "always"}
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true
{compute}{dev}
[Install]
WantedBy=multi-user.target
"""


def coordinator_unit(deployment: Deployment) -> str:
    """A separate lightweight coordinator shares the control database and release identity."""
    unit = server_unit(deployment, worker=False)
    root = deployment.root / "current"
    original = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    return (
        unit.replace(
            original,
            f"Environment=DATA_PLATFORM_COORDINATOR_CURRENT={root}\n"
            f"ExecStart={root}/.venv/bin/python -m lerobot.data_platform.processing.coordinator",
        )
        .replace(
            "Wants=network-online.target",
            "Wants=network-online.target\n"
            f"ConditionPathExists={root}/lerobot/data_platform/processing/coordinator.py",
        )
        .replace(
            f"Description=Data Platform {deployment.environment} web",
            f"Description=Data Platform {deployment.environment} pipeline coordinator",
        )
    )


def assistant_unit(deployment: Deployment) -> str:
    """Pin code/interpreter to this release; SIGTERM drains bounded generations."""
    root = (deployment.root / "current").resolve()
    unit = server_unit(deployment, worker=False, release=root)
    original = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    return (
        unit.replace(original, f"ExecStart={root}/.venv/bin/python -m lerobot.data_platform.ai_tools.runner")
        .replace("RestartSec=5", "RestartSec=5\nTimeoutStopSec=100\nKillMode=mixed")
        .replace(
            "Wants=network-online.target",
            f"Wants=network-online.target\nConditionPathExists={root}/lerobot/data_platform/ai_tools/runner.py",
        )
        .replace(
            f"Description=Data Platform {deployment.environment} web",
            f"Description=Data Platform {deployment.environment} assistant runner",
        )
    )

"""One-time adoption of an idle legacy Agent, preserving its node token and private state."""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
from pathlib import Path

from lerobot.data_platform.deployment import read_environment_file


def adopt_legacy(config: Path, state_root: Path, instance_id: str, version: str, manifest_digest: str):
    from lerobot.data_platform.agent import AgentClient
    from lerobot.data_platform.releases import atomic_json, run

    if config.exists():
        return
    old = Path("/etc/data-platform/agent.env")
    values = read_environment_file(old)
    health = AgentClient(values["DATA_PLATFORM_SERVER_URL"])._request("GET", "/healthz")
    if (health.get("release"), health.get("agent_manifest_sha256")) != (version, manifest_digest):
        raise RuntimeError("Agent bundle does not match the release installed on the production server")
    if health.get("active_jobs") != 0:
        raise RuntimeError("Wait for all production jobs to finish before adopting the Agent")
    if (health.get("environment"), health.get("instance_id"), health.get("maintenance")) != (
        "prod",
        instance_id,
        True,
    ):
        raise RuntimeError(
            "Adopt only after the production server has entered maintenance with the expected identity"
        )
    run(["systemctl", "disable", "--now", "data-platform-agent.service"])
    old_state = Path(values["DATA_PLATFORM_AGENT_STATE"])
    for path in old_state.parent.iterdir():
        if path.name in {"dev", "prod"}:
            continue
        destination = state_root / path.name
        if destination.exists():
            raise FileExistsError(
                "Adoption state already exists; inspect the incomplete adoption before retrying"
            )
        if path.is_dir():
            shutil.copytree(path, destination, symlinks=True)
        else:
            shutil.copy2(path, destination)
    import json

    node = json.loads((state_root / old_state.name).read_text())
    node.update(environment="prod", instance_id=instance_id)
    atomic_json(state_root / "agent.json", node)
    values.update(
        DATA_PLATFORM_ENV="prod",
        DATA_PLATFORM_INSTANCE_ID=instance_id,
        DATA_PLATFORM_AGENT_STATE=str(state_root / "agent.json"),
    )
    config.write_text("\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n")
    account = pwd.getpwnam("data-platform-agent-prod")
    config.chmod(0o640)
    os.chown(config, 0, account.pw_gid)
    for path in [state_root, *state_root.rglob("*")]:
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)

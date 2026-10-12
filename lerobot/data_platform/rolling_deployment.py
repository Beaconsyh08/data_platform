"""Compatible executor generations share task state while retaining immutable runtime code."""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

# Bump this contract whenever old executors cannot safely use the new server, result formats or state.
ROLLING_DEPLOY_PROTOCOL = 1


def compatible_contracts(previous: dict, candidate: dict, *, schema: int) -> bool:
    return (
        previous.get("rolling_deploy_protocol")
        == candidate.get("rolling_deploy_protocol")
        == ROLLING_DEPLOY_PROTOCOL
        and previous.get("schema_min")
        == previous.get("schema_max")
        == candidate.get("schema_min")
        == candidate.get("schema_max")
        == schema
        and previous.get("job_protocol") == candidate.get("job_protocol") == 2
    )


def execution_belongs_here(job: dict) -> bool:
    """An old generation retains computation and publication ownership through acknowledgement."""
    return not os.environ.get("DATA_PLATFORM_EXECUTOR_RELEASE") or (
        job.get("execution", {}).get("release") in {None, os.environ.get("DATA_PLATFORM_RELEASE")}
    )


def release_in_use(root: Path) -> bool:
    path = root / ".executor.lock"
    if not path.exists():
        return False
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def rolling_ready(store, deployment, candidate: dict, *, schema: int, required_names: list[str]) -> bool:
    """Missing bootstrap support, stale Agents or incompatible generations require an idle upgrade."""
    from lerobot.data_platform.maintenance import active_job_counts

    if active_job_counts(store).interrupted:
        return False
    current = deployment.root / "current"
    if not current.exists() or not required_names:
        return False
    previous = current.resolve()
    try:
        contracts = [json.loads((previous / "deployment-contract.json").read_text())]
        for root in (deployment.root / "releases").iterdir():
            if root != previous and release_in_use(root):
                contracts.append(json.loads((root / "deployment-contract.json").read_text()))
    except (OSError, ValueError):
        return False
    if not all(compatible_contracts(value, candidate, schema=schema) for value in contracts):
        return False
    nodes = {node["name"]: node for node in store.list_nodes()}
    for name in required_names:
        node = nodes.get(name) or {}
        caps = node.get("capabilities") or {}
        seen = datetime.fromisoformat(node["last_seen_at"]) if node.get("last_seen_at") else None
        if (
            not seen
            or (datetime.now(timezone.utc) - seen).total_seconds() > 60
            or caps.get("rolling_deploy_protocol") != ROLLING_DEPLOY_PROTOCOL
            or caps.get("release") != previous.name
            or caps.get("environment") != deployment.environment
            or caps.get("instance_id") != os.environ.get("DATA_PLATFORM_INSTANCE_ID")
        ):
            return False
    return release_in_use(previous)


@dataclass
class ExecutorGeneration:
    release: Path | None = None
    current: Path | None = None
    _lock: BinaryIO | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(cls) -> ExecutorGeneration:
        release = os.environ.get("DATA_PLATFORM_EXECUTOR_RELEASE")
        current = os.environ.get("DATA_PLATFORM_EXECUTOR_CURRENT")
        if bool(release) != bool(current):
            raise RuntimeError("Executor generation requires both immutable release and current paths")
        return cls(Path(release) if release else None, Path(current) if current else None)

    @property
    def draining(self) -> bool:
        return self.release is not None and self.current.resolve() != self.release.resolve()

    def __enter__(self) -> ExecutorGeneration:
        if self.release is not None:
            self._lock = (self.release / ".executor.lock").open("rb")
            fcntl.flock(self._lock, fcntl.LOCK_SH)
        return self

    def __exit__(self, *_):
        if self._lock is not None:
            self._lock.close()

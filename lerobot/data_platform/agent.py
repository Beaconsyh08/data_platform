"""Node agent for executing Data Platform jobs beside local datasets."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from lerobot.common.datasets.lerobot_dataset import LeRobotDatasetMetadata
from lerobot.data_platform.cli import get_default_output_dir, run_precompute
from lerobot.data_platform.job_concurrency import agent_slots
from lerobot.data_platform.operation_log import append_operation_event
from lerobot.data_platform.operators.registry import operation_ids
from lerobot.data_platform.precompute.data_profile import (
    DATA_PROFILE_PROTOCOL,
    dataset_semantics,
    require_dataset_operation,
    resolve_data_profile,
    resolve_processing_profile,
)
from lerobot.data_platform.precompute.dataset_io import V3DatasetMetadata, is_v3_dataset
from lerobot.data_platform.precompute.preprocess import (
    default_standardize_path,
    default_v3_path,
    delete_episodes_inplace,
    repair_v3_video_timestamps,
    run_preprocess_op,
)
from lerobot.data_platform.precompute.preprocess.common import default_preprocess_path
from lerobot.data_platform.precompute.preprocess.dataset_merge import validate_merge_sources
from lerobot.data_platform.task_catalog import TASK_CONFIG_PROTOCOL, TaskConfigSnapshot

_PREPROCESS_OPS = {
    name.removeprefix("preprocess.") for name in operation_ids(family="preprocess", remote=True)
}
_SOURCE_MUTATION_OPS = {
    name.removeprefix("mutation.") for name in operation_ids(family="mutation", remote=True)
}
_FORBIDDEN_OPTION_KEYS = {
    "in_place",
    "output_mode",
    "root",
    "roots",
    "source_root",
    "source_roots",
    "src_root",
    "src_roots",
}
_MERGE_OPTION_KEYS = {
    "source_location_ids",
    "_source_locations",
    "dimension_policy",
    "dimension_names",
    "dimension_indices",
    "padding_value",
    "exclude_episodes",
    "workers",
    "dry_run",
    "out_root",
}


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "__dict__"):
        return _json_value(vars(value))
    return str(value)


def _safe_name(value: str, default: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return normalized[:64] or default


def _resolve_roots(values: list[Path]) -> list[Path]:
    roots = []
    for value in values:
        path = Path(value).expanduser().resolve()
        if path not in roots:
            roots.append(path)
    return roots


def _is_within(path: Path, roots: list[Path]) -> bool:
    resolved = Path(path).expanduser().resolve()
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def _require_within(path: Path, roots: list[Path], label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not _is_within(resolved, roots):
        raise PermissionError(f"{label} is outside configured roots: {resolved}")
    return resolved


def _require_safe_output(path: Path, source_root: Path, writable_roots: list[Path]) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"preprocess output may not be a symbolic link: {candidate}")
    output = _require_within(candidate, writable_roots, "preprocess output directory")
    source = Path(source_root).expanduser().resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("preprocess output must be separate from the source dataset and may not contain it")
    return output


def _default_remote_output(op: str, source_root: Path, options: dict) -> Path:
    if op == "standardize":
        return default_standardize_path(source_root)
    if op == "convert_v3":
        return default_v3_path(source_root)
    if op == "convert_action":
        return default_preprocess_path(source_root, f"action{int(options.get('target_dim') or 16)}")
    if op == "drop_field":
        return default_preprocess_path(source_root, f"drop_{options.get('field_name') or 'field'}")
    if op == "smooth_action":
        return default_preprocess_path(source_root, f"smooth_action_w{int(options.get('window') or 5)}")
    return default_preprocess_path(source_root, op)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_episode_ids(value: Any) -> list[int]:
    if isinstance(value, list):
        try:
            ids = {int(item) for item in value}
        except (TypeError, ValueError) as exc:
            raise ValueError("episode ids must be integers") from exc
        if not ids or min(ids) < 0:
            raise ValueError("episode ids must contain one or more non-negative integers")
        return sorted(ids)
    text = str(value or "").strip()
    if not text:
        raise ValueError("episode ids are required")
    ids: set[int] = set()
    for token in re.sub(r"\s*-\s*", "-", text).replace(",", " ").split():
        if token.isdigit():
            ids.add(int(token))
            continue
        match = re.fullmatch(r"(\d+)-(\d+)", token)
        if match is None or int(match.group(2)) < int(match.group(1)):
            raise ValueError(f"invalid episode id or range: {token}")
        ids.update(range(int(match.group(1)), int(match.group(2)) + 1))
    if not ids:
        raise ValueError("episode ids are required")
    return sorted(ids)


def _link_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


class _PersistentDatasetBackup:
    """Keep a recoverable snapshot of the dataset directories touched by source mutations."""

    def __init__(self, source_root: Path, backup_root: Path, *, job_id: str, operation: str):
        self.source_root = Path(source_root)
        self.backup_root = Path(backup_root)
        self.entries: list[tuple[str, bool]] = []
        dataset_backup = self.backup_root / "dataset"
        try:
            dataset_backup.mkdir(parents=True, exist_ok=False)
            for name in ("data", "meta", "videos"):
                source = self.source_root / name
                self.entries.append((name, source.exists()))
                if not source.exists():
                    continue
                copy_function = shutil.copy2 if name == "meta" else _link_or_copy
                shutil.copytree(
                    source,
                    dataset_backup / name,
                    copy_function=copy_function,
                    symlinks=True,
                )
            (self.backup_root / "backup.json").write_text(
                json.dumps(
                    {
                        "source_root": str(self.source_root),
                        "job_id": job_id,
                        "operation": operation,
                        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    },
                    indent=2,
                )
                + "\n"
            )
        except Exception:
            shutil.rmtree(self.backup_root, ignore_errors=True)
            raise

    def restore(self) -> None:
        dataset_backup = self.backup_root / "dataset"
        for name, existed in self.entries:
            target = self.source_root / name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            if existed:
                shutil.copytree(dataset_backup / name, target, copy_function=shutil.copy2, symlinks=True)


class _AgentDataset:
    def __init__(self, repo_id: str, root: Path):
        self.repo_id = repo_id
        if is_v3_dataset(root):
            self.meta = V3DatasetMetadata(repo_id=repo_id, root=root)
        else:
            self.meta = LeRobotDatasetMetadata(repo_id=repo_id, root=root)
        self.root = self.meta.root
        self.features = self.meta.features
        self.fps = self.meta.fps
        self.codebase_version = self.meta.info.get("codebase_version", "unknown")
        self.total_frames = self.meta.total_frames
        self.total_episodes = self.meta.total_episodes


def _dataset_payload(root: Path, *, node_name: str, metadata: dict | None = None) -> dict:
    from lerobot.data_platform.precompute.dataset_io import load_episode_records, load_task_records

    root = Path(root).resolve()
    info = json.loads((root / "meta" / "info.json").read_text())
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    namespace = f"node-{_safe_name(node_name, 'remote')}"
    name = f"{_safe_name(root.name, 'dataset')}-{digest}"
    details = {
        "codebase_version": info.get("codebase_version"),
        "fps": info.get("fps"),
        "total_episodes": info.get("total_episodes"),
        "total_frames": info.get("total_frames"),
        "features": info.get("features") or {},
        "analysis_metadata_version": 1,
        "tasks": load_task_records(root),
        "episodes": [
            {
                key: row[key]
                for key in (
                    "episode_index",
                    "tasks",
                    "task",
                    "task_index",
                    "length",
                    "dataset_from_index",
                    "dataset_to_index",
                )
                if key in row
            }
            for row in load_episode_records(root)
        ],
    }
    stats_path = root / "meta/stats.json"
    details["stats"] = {
        key: value
        for key, value in (json.loads(stats_path.read_text()) if stats_path.is_file() else {}).items()
        if str((info.get("features", {}).get(key) or {}).get("dtype", "")).startswith(
            ("float", "int", "uint")
        )
    }
    details.update(dict(metadata or {}))
    details.update(dataset_semantics(info, resolve_data_profile(root)))
    return {
        "dataset_key": f"{namespace}/{name}",
        "root": str(root),
        "output_dir": str(get_default_output_dir(root)),
        "metadata": details,
    }


def discover_datasets(roots: list[Path], *, node_name: str, strict: bool = False) -> list[dict]:
    datasets = []
    seen = set()
    for configured_root in _resolve_roots(roots):
        if not configured_root.exists():
            logging.warning("Allowed root does not exist: %s", configured_root)
            continue
        stack = [configured_root]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if (current / "meta" / "info.json").is_file() and (current / "data").is_dir():
                try:
                    datasets.append(_dataset_payload(current, node_name=node_name))
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    if strict:
                        raise
                    logging.warning("Could not inspect dataset %s: %s", current, exc)
                continue
            mcap_files = [path for path in current.glob("*.mcap") if path.is_file() and not path.is_symlink()]
            if mcap_files:
                from lerobot.data_platform.mcap_import import raw_location

                datasets.append(raw_location(current, node_name, mcap_files))
                continue
            try:
                children = sorted(current.iterdir(), reverse=True)
            except OSError:
                if strict:
                    raise
                continue
            for child in children:
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and not child.name.startswith(".")
                    and child.name not in {"data", "videos", "vis", "wandb", "outputs", "__pycache__"}
                ):
                    stack.append(child)
    return sorted(datasets, key=lambda item: (item["dataset_key"], item["root"]))


@dataclass
class AgentState:
    node_id: str
    node_token: str
    name: str
    server_url: str
    environment: str = ""
    instance_id: str = ""


class AgentClient:
    def __init__(self, server_url: str, *, verify: bool | str = True):
        self.server_url = str(server_url).rstrip("/")
        self.verify = verify
        if verify is True:
            self.verify = os.environ.get("DATA_PLATFORM_AGENT_CA_BUNDLE") or True
        self.session = requests.Session()
        self.worker_instance_id = str(uuid.uuid4())
        self.executions = {}

    def activate(self, job):
        if job.get("execution"):
            self.executions[job["job_id"]] = dict(job["execution"])

    def checkpoint(self, state, job_id, phase, *, fingerprint=None):
        return self._request(
            "POST",
            f"/api/agents/jobs/{job_id}/checkpoint",
            token=state.node_token,
            json={"phase": phase, "fingerprint": fingerprint},
        )

    def control_heartbeat(self, state, job_id, *, lease_seconds):
        return self._request(
            "POST",
            f"/api/agents/jobs/{job_id}/heartbeat",
            token=state.node_token,
            json={"lease_seconds": lease_seconds},
        )

    def _request(self, method: str, path: str, *, token: str | None = None, **kwargs) -> dict:
        headers = dict(kwargs.pop("headers", {}) or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        parts = path.split("/")
        execution = (
            self.executions.get(parts[4])
            if len(parts) > 4 and parts[1:4] == ["api", "agents", "jobs"]
            else None
        )
        if execution:
            headers["X-Job-Attempt"] = execution["attempt_id"]
            headers["X-Job-Credential"] = execution["credential"]
        response = self.session.request(
            method,
            f"{self.server_url}{path}",
            headers=headers,
            timeout=kwargs.pop("timeout", (10, 300)),
            verify=self.verify,
            **kwargs,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": response.text[:1000]}
        if not response.ok:
            raise RuntimeError(payload.get("error") or f"HTTP {response.status_code}")
        return payload

    def enroll(self, payload: dict) -> AgentState:
        response = self._request("POST", "/api/agents/enroll", json=payload)
        node = response["node"]
        return AgentState(
            node_id=node["node_id"],
            node_token=response["node_token"],
            name=node["name"],
            server_url=self.server_url,
        )

    def heartbeat(self, state: AgentState, capabilities: dict) -> dict:
        return self._request(
            "POST",
            "/api/agents/heartbeat",
            token=state.node_token,
            json={"capabilities": capabilities},
        )

    def result_locations(self, state: AgentState) -> list[dict]:
        return self._request("GET", "/api/agents/dataset-results", token=state.node_token)["locations"]

    def result_bundle(self, state: AgentState, location_id: str) -> dict:
        return self._request("GET", f"/api/agents/locations/{location_id}/results", token=state.node_token)

    def ack_results(self, state: AgentState, location_id: str, revision: str, *, error: bool = False) -> dict:
        return self._request(
            "POST",
            f"/api/agents/locations/{location_id}/results/ack",
            token=state.node_token,
            json={"revision": revision, "error": error},
        )

    def sync_locations(self, state: AgentState, locations: list[dict]) -> list[dict]:
        response = self._request(
            "POST",
            "/api/agents/locations/sync",
            token=state.node_token,
            json={"locations": locations},
        )
        return list(response["locations"])

    def update_scan(self, state: AgentState, request_id: str, **payload) -> dict:
        return self._request(
            "POST",
            f"/api/agents/scans/{request_id}",
            token=state.node_token,
            json=payload,
        )["scan"]

    def claim(self, state: AgentState, *, lease_seconds: int) -> dict | None:
        response = self._request(
            "POST",
            "/api/agents/jobs/claim",
            token=state.node_token,
            json={
                "lease_seconds": lease_seconds,
                "worker_instance_id": self.worker_instance_id,
                "release": os.environ.get("DATA_PLATFORM_RELEASE"),
            },
        )
        job = response.get("job")
        if job:
            self.activate(job)
        return job

    def heartbeat_job(self, state: AgentState, job_id: str, *, lease_seconds: int) -> bool:
        response = self._request(
            "POST",
            f"/api/agents/jobs/{job_id}/heartbeat",
            token=state.node_token,
            json={"lease_seconds": lease_seconds},
        )
        return bool(response.get("renewed"))

    def event(self, state: AgentState, job_id: str, message: str, payload: dict | None = None) -> None:
        self._request(
            "POST",
            f"/api/agents/jobs/{job_id}/events",
            token=state.node_token,
            json={"message": str(message), "payload": _json_value(payload or {})},
        )

    def upload_artifact(
        self,
        state: AgentState,
        job_id: str,
        relative_path: Path,
        path: Path,
        *,
        derived: bool = False,
        caption: bool = False,
        curation: bool = False,
    ) -> None:
        endpoint = (
            "curation-artifacts"
            if curation
            else ("caption-artifacts" if caption else ("derived-artifacts" if derived else "artifacts"))
        )
        with Path(path).open("rb") as handle:
            self._request(
                "PUT",
                f"/api/agents/jobs/{job_id}/{endpoint}/{relative_path.as_posix()}",
                token=state.node_token,
                data=handle,
                headers={"Content-Type": "application/octet-stream"},
                timeout=(10, 3600),
            )

    def download_curation_input(self, state, job, name, destination, expected):
        import hashlib
        from urllib.parse import quote

        execution = job["execution"]
        headers = {
            "Authorization": f"Bearer {state.node_token}",
            "X-Job-Attempt": execution["attempt_id"],
            "X-Job-Credential": execution["credential"],
        }
        url = f"{self.server_url}/api/agents/jobs/{job['job_id']}/curation-inputs/{quote(name, safe='/')}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, count = hashlib.sha256(), 0
        renewed_at = 0.0

        def renew():
            nonlocal renewed_at
            if time.monotonic() - renewed_at < 20:
                return
            response = self.control_heartbeat(state, job["job_id"], lease_seconds=120)
            if response.get("stop_mode"):
                from lerobot.data_platform.execution import StopRequestedError

                raise StopRequestedError("Curation input download cancelled")
            if not response.get("renewed"):
                raise RuntimeError("Lost execution lease while downloading Curation input")
            renewed_at = time.monotonic()

        renew()
        with self.session.get(
            url, headers=headers, stream=True, timeout=(10, 30), verify=self.verify
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    renew()
                    count += len(chunk)
                    if count > expected["size"]:
                        raise ValueError("Curation input exceeds declared size")
                    digest.update(chunk)
                    handle.write(chunk)
        if count != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError("Curation input checksum mismatch")

    def complete(
        self,
        state: AgentState,
        job_id: str,
        *,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> dict:
        response = self._request(
            "POST",
            f"/api/agents/jobs/{job_id}/complete",
            token=state.node_token,
            json={"status": status, "result": _json_value(result or {}), "error": error},
        )
        return response["job"]


class DataPlatformAgent:
    def __init__(
        self,
        *,
        client: AgentClient,
        state_path: Path,
        name: str,
        allowed_roots: list[Path],
        writable_roots: list[Path],
        enrollment_token: str,
        lease_seconds: int = 300,
        allow_source_mutations: bool = False,
        local_requests: bool = False,
    ):
        self.client = client
        self.state_path = Path(state_path).expanduser()
        self.name = str(name)
        self.allowed_roots = _resolve_roots(allowed_roots)
        self.writable_roots = _resolve_roots(writable_roots)
        self.enrollment_token = str(enrollment_token)
        self.lease_seconds = max(30, int(lease_seconds))
        self.allow_source_mutations = bool(allow_source_mutations)
        self.local_requests = bool(local_requests)
        from lerobot.data_platform.environment import EnvironmentIdentity, validate_dev_path, verify_directory

        self.environment_identity = EnvironmentIdentity.from_env()
        if self.environment_identity:
            verify_directory(self.state_path.parent.resolve(), "agent")
            for root in self.allowed_roots + self.writable_roots:
                validate_dev_path(root)
            health = self.client._request("GET", "/healthz")
            if any(
                health.get(key) != value
                for key, value in self.environment_identity.record("agent").items()
                if key != "kind"
            ):
                raise RuntimeError("Agent and server environment identities do not match")
        self.state = self._load_or_enroll()

    def _load_or_enroll(self) -> AgentState:
        if self.state_path.is_file():
            payload = json.loads(self.state_path.read_text())
            state = AgentState(**payload)
            if self.environment_identity and (
                state.environment != self.environment_identity.name
                or state.instance_id != self.environment_identity.instance_id
            ):
                raise RuntimeError("Agent state belongs to a different environment")
            if state.name != self.name or state.server_url.rstrip("/") != self.client.server_url:
                raise ValueError("agent state belongs to a different node name or server URL")
            return state
        if not self.enrollment_token:
            raise RuntimeError("DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN is required for first enrollment")
        state = self.client.enroll(
            {
                "name": self.name,
                "hostname": socket.gethostname(),
                "allowed_roots": [str(path) for path in self.allowed_roots],
                "writable_roots": [str(path) for path in self.writable_roots],
                "capabilities": self.capabilities(),
                "enrollment_token": self.enrollment_token,
            }
        )
        if self.environment_identity:
            state.environment = self.environment_identity.name
            state.instance_id = self.environment_identity.instance_id
        self._write_state(state)
        return state

    def _write_state(self, state: AgentState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(asdict(state), indent=2) + "\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _completed_mutation_path(self, job_id: str) -> Path:
        return self.state_path.parent / "completed-mutations" / f"{job_id}.json"

    def _load_completed_mutation(self, job_id: str) -> dict | None:
        path = self._completed_mutation_path(job_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"invalid completed mutation marker: {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid completed mutation marker: {path}")
        return payload

    def _write_completed_mutation(self, job_id: str, result: dict) -> None:
        path = self._completed_mutation_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(_json_value(result), indent=2) + "\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def capabilities(self) -> dict:
        from lerobot.data_platform.curation_execution import worker_capabilities
        from lerobot.data_platform.mcap_contract import OPERATIONS as MCAP_OPERATIONS
        from lerobot.data_platform.mcap_import import runtime_status

        disk = {}
        for root in self.allowed_roots:
            try:
                usage = shutil.disk_usage(root)
                disk[str(root)] = {"total": usage.total, "free": usage.free}
            except OSError:
                continue
        operations = [
            *worker_capabilities()["curation_operations"],
            "caption.annotate",
            "viewer.prepare",
            *[f"preprocess.{op}" for op in sorted(_PREPROCESS_OPS)],
        ]
        if self.allow_source_mutations:
            operations.extend(f"mutation.{op}" for op in sorted(_SOURCE_MUTATION_OPS))
        mcap_runtime = runtime_status()
        if mcap_runtime.get("ready"):
            operations.extend(sorted(MCAP_OPERATIONS))
        from lerobot.data_platform.operators.registry import list_operators, worker_manifest

        operations.extend(
            spec.operator_id
            for spec in list_operators()
            if spec.remote
            and spec.family not in {"mcap", "curation", "caption", "viewer", "preprocess", "mutation"}
        )
        backends = worker_capabilities().get("curation_backends", {})
        for kind in ("labeling", "tagging", "embedding"):
            group = backends.get(kind, {})
            ready = (
                any(item.get("available") for item in group.get("backends", {}).values())
                if "backends" in group
                else group.get("available")
            )
            if not ready and f"curation.{kind}" in operations:
                operations.remove(f"curation.{kind}")
        manifest = worker_manifest(operations)
        operations = sorted(set(operations) & set(manifest["operator_versions"]))
        if self.local_requests:
            # Local commands and protected request adapters pin local-only descriptors too.
            manifest["operator_versions"].update(
                {
                    spec.operator_id: spec.version
                    for spec in list_operators()
                    if spec.effect != "source" or self.allow_source_mutations
                }
            )
        return {
            **manifest,
            **worker_capabilities(),
            "caption_protocol": 1,
            "caption_read_protocol": 1,
            "source_cleanup_protocol": 1,
            "caption_concurrent_limit": max(
                1, int(os.environ.get("DATA_PLATFORM_CAPTION_RUNNING_LIMIT", "1"))
            ),
            "job_concurrent_limit": agent_slots(),
            "caption_dataset_versions": ["v2.1", "v3.0"],
            "caption_signal_options": True,
            "caption_schemes": ["multiview_semantics", "video_events", "fusion_review"],
            "caption_configured": bool(os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_DASHSCOPE_API_KEY")),
            "job_protocol": 2,
            "local_requests": self.local_requests,
            "rolling_deploy_protocol": 1 if os.environ.get("DATA_PLATFORM_EXECUTOR_RELEASE") else 0,
            "environment": os.environ.get("DATA_PLATFORM_ENV", "legacy"),
            "instance_id": os.environ.get("DATA_PLATFORM_INSTANCE_ID", ""),
            "release": os.environ.get("DATA_PLATFORM_RELEASE", "legacy"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "disk": disk,
            "allowed_roots": [str(path) for path in self.allowed_roots],
            "writable_roots": [str(path) for path in self.writable_roots],
            "data_profile_protocol": DATA_PROFILE_PROTOCOL,
            "operations": operations,
            "merge_alignment_protocol": 3,
            "source_mutations_enabled": self.allow_source_mutations,
            "task_config_protocol": TASK_CONFIG_PROTOCOL,
            "result_sync_protocol": 1,
            "dataset_scan_protocol": 1,
            "mcap_import_protocol": 1,
            "mcap_import": {**mcap_runtime, "adapters": ["genrobot_das"]},
        }

    def sync_locations(self) -> list[dict]:
        discovered = discover_datasets(self.allowed_roots, node_name=self.name)
        return self.client.sync_locations(self.state, discovered)

    def _process_scan_request(self, request: dict | None) -> None:
        completion = getattr(self, "_pending_scan_completion", None)
        if completion:
            request_id, payload = completion
            self.client.update_scan(self.state, request_id, **payload)
            self._pending_scan_completion = None
            if request and request["request_id"] == request_id:
                return
        if not request:
            return
        request_id = request["request_id"]
        self.client.update_scan(self.state, request_id, status="running")
        try:
            root = _require_within(Path(request["path"]), self.allowed_roots, "scan path")
            if not root.is_dir():
                raise FileNotFoundError(f"Scan directory does not exist: {root}")
            locations = discover_datasets([root], node_name=self.name, strict=True)
            synced = self.client.sync_locations(self.state, locations)
            payload = {"status": "done", "dataset_count": len(synced)}
        except Exception as exc:
            from lerobot.data_platform.operation_log import sanitize_for_log

            payload = {"status": "error", "error": sanitize_for_log(str(exc))}
        self._pending_scan_completion = request_id, payload
        self.client.update_scan(self.state, request_id, **payload)
        self._pending_scan_completion = None

    def run_once(
        self, *, sync: bool = True, dispatch=None, allow_claim: bool = True, sync_results: bool = True
    ) -> bool:
        heartbeat = self.client.heartbeat(self.state, self.capabilities()) or {}
        if heartbeat.get("draining"):
            return False
        if isinstance(self.client, AgentClient) and heartbeat.get("caption_read_protocol") == 1:
            self._start_caption_reads()
        if (
            isinstance(self.client, AgentClient)
            and heartbeat.get("source_cleanup_protocol") == 1
            and not getattr(self, "_source_cleanup", None)
        ):
            from lerobot.data_platform.source_cleanup_agent import SourceCleanupWorker

            self._source_cleanup = SourceCleanupWorker(self)
            self._source_cleanup.start()
        self._process_scan_request(heartbeat.get("scan_request"))
        if isinstance(self.client, AgentClient):
            from lerobot.data_platform.execution import ExecutionSupervisor, StopRequestedError

            supervisor = ExecutionSupervisor(self)
            if dispatch is None and supervisor.reconcile():
                return True
        if sync:
            self.sync_locations()
        if sync_results and heartbeat.get("result_sync_protocol") == 1:
            self._sync_results(background=dispatch is not None)
        if not allow_claim:
            return False
        job = self.client.claim(self.state, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        if dispatch is not None and job.get("execution"):
            dispatch(job)
            return True
        job_id = job["job_id"]
        if job.get("execution"):
            lock = supervisor.acquire_supervision(job["execution"]["attempt_id"])
            if lock is None:
                return False
            try:
                supervisor.start(job)
            except Exception as exc:
                logging.exception("Execution requires reconciliation for job %s", job_id)
                marker = supervisor.root / job["execution"]["attempt_id"] / "marker.json"
                if not marker.exists():
                    self.client.complete(
                        self.state,
                        job_id,
                        status="cancelled" if isinstance(exc, StopRequestedError) else "error",
                        error=str(exc),
                    )
                    supervisor._release(supervisor.root / job["execution"]["attempt_id"] / "reservation.json")
            finally:
                lock.close()
            return True
        stop_heartbeat = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._renew_job_lease,
            args=(job_id, stop_heartbeat),
            daemon=True,
            name=f"data-platform-lease-{job_id[:8]}",
        )
        heartbeat_thread.start()
        try:
            self.client.event(self.state, job_id, f"Starting {job['operation']}")
            result = self.execute_job(job)
        except Exception as exc:
            logging.exception("Remote job %s failed", job_id)
            try:
                self.client.complete(self.state, job_id, status="error", error=str(exc))
            except Exception:
                logging.exception("Could not report failure for remote job %s", job_id)
        else:
            try:
                self.client.complete(self.state, job_id, status="done", result=result)
            except Exception:
                logging.exception("Job %s finished locally but completion could not be confirmed", job_id)
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=5)
        return True

    def _renew_job_lease(self, job_id: str, stop: threading.Event) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        while not stop.wait(interval):
            try:
                if not self.client.heartbeat_job(
                    self.state,
                    job_id,
                    lease_seconds=self.lease_seconds,
                ):
                    logging.error(
                        "Lost lease for remote job %s; allowing the current safe operation to finish", job_id
                    )
                    return
            except Exception:
                logging.exception("Could not renew lease for remote job %s", job_id)

    def execute_job(self, job: dict) -> dict:
        location = dict(job.get("location") or {})
        source_root = _require_within(Path(location.get("root") or ""), self.allowed_roots, "dataset root")
        operation = str(job.get("operation") or "")
        from lerobot.data_platform.curation_execution import execute_curation_job
        from lerobot.data_platform.mcap_import import execute_import
        from lerobot.data_platform.operators import ExecutionContext, get_operator
        from lerobot.data_platform.operators.registry import execute_operator
        from lerobot.data_platform.temporal_caption_jobs import execute_caption_job

        snapshot = (job.get("options") or {}).get("operator_snapshot")
        if (job.get("options") or {}).get("local_compute"):
            from lerobot.data_platform.processing.local_executor import execute_local_operator

            get_operator(operation, snapshot.get("version") if snapshot else None)
            return execute_local_operator(self, job)
        get_operator(operation, snapshot.get("version") if snapshot else None)
        if not operation.startswith("import.mcap."):
            require_dataset_operation(
                source_root, operation, data_version_override=(job.get("options") or {}).get("data_version")
            )
        if snapshot and snapshot.get("source_fingerprint"):
            from lerobot.data_platform.lifecycle import dataset_snapshot

            if dataset_snapshot(source_root)[0] != snapshot["source_fingerprint"]:
                raise ValueError("Pinned pipeline dataset version changed")
        if snapshot:
            job = {
                **job,
                "_operator_version": snapshot["version"],
                "options": {k: v for k, v in job["options"].items() if k != "operator_snapshot"},
            }
        spec = get_operator(operation, job.get("_operator_version"))
        if spec.family == "preprocess" and job["options"].get("version_inputs"):
            from lerobot.data_platform.operators.dataset_versions import (
                publish_preprocess_version,
                resume_preprocess_result,
            )

            completed = resume_preprocess_result(self, job)
            if completed:
                return publish_preprocess_version(self, job, source_root, completed)
        if spec.family not in {"mcap", "curation", "caption", "viewer", "preprocess", "mutation"}:
            from lerobot.data_platform.operators.execution import execute_registered

            return execute_registered(self, job, source_root, location)
        return execute_operator(
            operation,
            ExecutionContext(
                source_root=source_root,
                handlers={
                    "mcap": lambda: execute_import(self, job, source_root),
                    "curation": lambda: execute_curation_job(self, job, source_root, location),
                    "caption": lambda: execute_caption_job(self, job, source_root, location),
                    "viewer": lambda: self._prepare_viewer(job, source_root, location),
                    "preprocess": lambda: self._run_preprocess(job, source_root, location),
                    "mutation": lambda: self._run_source_mutation(job, source_root, location),
                },
            ),
        )

    def _prepare_viewer(self, job: dict, source_root: Path, location: dict) -> dict:
        options = dict(job.get("options") or {})
        TaskConfigSnapshot.from_dict(options.get("task_config"))
        output_dir = Path(location.get("output_dir") or get_default_output_dir(source_root)).expanduser()
        output_dir = _require_within(output_dir, self.writable_roots, "viewer output directory")
        last_event_at = 0.0
        last_stage = None

        def progress(payload: dict) -> None:
            nonlocal last_event_at, last_stage
            now = time.monotonic()
            stage = payload.get("phase") or payload.get("step")
            finished = (
                payload.get("status") in {"done", "error"}
                or (payload.get("total") and payload.get("current", 0) >= payload["total"])
                or ("files_total" in payload and payload.get("files_current") == payload["files_total"])
            )
            if now - last_event_at < 1.0 and stage == last_stage and not finished:
                return
            last_event_at, last_stage = now, stage
            payload = dict(payload)
            if "progress" in payload:
                payload["progress"] = 0.95 * payload["progress"]
            elif payload.get("total"):
                payload["progress"] = 95 * payload.get("current", 0) / payload["total"]
            self.client.event(
                self.state,
                job["job_id"],
                str(payload.get("message") or payload.get("step") or "Viewer preparation progress"),
                payload,
            )

        result = run_precompute(
            root=source_root,
            repo_id=location["dataset_key"],
            episodes=options.get("episodes"),
            image_keys=options.get("image_keys"),
            output_dir=output_dir,
            prepare_videos=bool(options.get("prepare_videos", True)),
            prepare_csv=bool(options.get("prepare_csv", True)),
            prepare_workers=max(1, int(options.get("prepare_workers") or 4)),
            max_frames=options.get("max_frames"),
            downsample=options.get("downsample"),
            overwrite=bool(options.get("overwrite", False)),
            overwrite_csv=bool(options.get("overwrite_csv", False)),
            visualize_only=True,
            data_version=options.get("data_version"),
            task_config=options.get("task_config"),
            fallback_stage_count=int(options.get("fallback_stage_count") or 5),
            force_recompute_stage=bool(options.get("force_recompute_stage", False)),
            progress_callback=progress,
            show_progress=False,
        )
        uploaded_files = self._upload_viewer_artifacts(job, output_dir / "static")
        return {
            "output_dir": str(output_dir),
            "uploaded_files": uploaded_files,
            "precompute": _json_value(result),
        }

    def _upload_viewer_artifacts(self, job: dict, static_dir: Path, *, derived: bool = False) -> int:
        """Upload a complete cache, with the manifest last so Server A never exposes a partial view."""
        static_dir = Path(static_dir)
        manifest = static_dir / "viewer_manifest.json"
        if not manifest.is_file():
            raise FileNotFoundError(f"viewer cache manifest was not generated: {manifest}")
        files = [path for path in static_dir.rglob("*") if path.is_file() and not path.is_symlink()]
        files.sort(key=lambda path: (path == manifest, path.as_posix()))
        for index, path in enumerate(files, start=1):
            relative = path.relative_to(static_dir)
            self.client.upload_artifact(
                self.state,
                job["job_id"],
                relative,
                path,
                derived=derived,
            )
            if not getattr(self.client, "deferred_uploads", False):
                start = 98 if derived else 95
                self.client.event(
                    self.state,
                    job["job_id"],
                    f"Uploaded viewer artifacts {index}/{len(files)}",
                    {
                        "current": index,
                        "total": len(files),
                        "phase": "upload",
                        "progress": start + (99 - start) * index / len(files),
                    },
                )
        return len(files)

    def _run_preprocess(self, job: dict, source_root: Path, location: dict) -> dict:
        op = str(job["operation"]).removeprefix("preprocess.")
        if op not in _PREPROCESS_OPS:
            raise ValueError(f"unsupported remote preprocess op: {op}")
        if op in {"merge", "subtract"}:
            from lerobot.data_platform.operators.dataset_versions import publish_preprocess_version

            computed = (
                self._run_merge(job, source_root) if op == "merge" else self._run_subtract(job, source_root)
            )
            return publish_preprocess_version(self, job, source_root, computed)
        options = dict(job.get("options") or {})
        version_inputs = options.pop("version_inputs", None)
        if version_inputs:
            from lerobot.data_platform.lifecycle import dataset_snapshot

            roots = {str(source_root.resolve()): source_root}
            roots.update(
                {
                    str(Path(item["root"]).resolve()): Path(item["root"])
                    for item in options.get("_source_locations", [])
                }
            )
            for value in version_inputs:
                root = roots.get(str(Path(value["version"]["root"]).resolve()))
                if root is None or dataset_snapshot(root)[0] != value["version"]["fingerprint"]:
                    raise ValueError("Versioned pipeline input changed")
            for key in ("target", "parents", "source_batches", "task_config"):
                options.pop(key, None)
        if op == "pose_interpolate":
            from lerobot.data_platform.lifecycle import dataset_snapshot
            from lerobot.data_platform.task_catalog import content_digest

            review = options.pop("_review", None)
            if (
                not isinstance(review, dict)
                or not review.get("approved_by")
                or not review.get("evidence_job_ids")
            ):
                raise PermissionError("Pose interpolation requires a reviewed pipeline decision")
            if review.get("repairs_digest") != content_digest(options.get("repairs")) or (
                review.get("source_fingerprint") != dataset_snapshot(source_root)[0]
            ):
                raise ValueError("Reviewed pose inputs changed")
        blocked = sorted(_FORBIDDEN_OPTION_KEYS.intersection(options))
        if blocked:
            raise ValueError(f"remote path/in-place options are not allowed: {blocked}")
        requested_out_root = str(options.pop("out_root", "") or "").strip()
        out_root = (
            Path(requested_out_root).expanduser()
            if requested_out_root
            else _default_remote_output(op, source_root, options)
        )
        out_root = _require_safe_output(out_root, source_root, self.writable_roots)

        overwrite_output = bool(options.pop("overwrite_output", False))
        if overwrite_output:
            if op != "standardize":
                raise ValueError("overwrite_output is only supported for standardize")
            options["overwrite"] = True
        if "overwrite" in options and op not in {"convert_v3", "standardize"}:
            raise ValueError(f"overwrite is not supported for remote {op}")

        delete_episode_value = options.pop("delete_episodes", None)
        if delete_episode_value not in (None, "", []):
            if op != "standardize":
                raise ValueError("delete_episodes is only supported for standardize")
            delete_episode_ids = _parse_episode_ids(delete_episode_value)
        else:
            delete_episode_ids = []
        standardize_step_count = 4 if delete_episode_ids else 3
        standardize_write_end = 55 if delete_episode_ids else 60
        standardize_cache_start = 65 if delete_episode_ids else 60

        if (
            op in {"convert_v3", "standardize"}
            and bool(options.get("overwrite", False))
            and out_root.exists()
        ):
            if out_root.is_symlink() or not out_root.is_dir():
                raise ValueError(f"refusing to overwrite a non-directory output: {out_root}")
            if not (out_root / "meta" / "info.json").is_file():
                raise ValueError(f"refusing to overwrite a directory that is not a dataset: {out_root}")

        def progress(payload: dict) -> None:
            message = str(payload.get("message") or payload.get("step") or "Preprocess progress")
            self.client.event(self.state, job["job_id"], message, payload)

        def scaled_progress(start: int, end: int, label: str):
            def callback(payload: dict) -> None:
                current = float(payload.get("current") or 0)
                total = float(payload.get("total") or 0)
                fraction = (
                    float(payload["progress"]) / 100
                    if "progress" in payload
                    else current / total
                    if total > 0
                    else 0.0
                )
                mapped = start + max(0.0, min(1.0, fraction)) * (end - start)
                message = str(payload.get("message") or payload.get("step") or label)
                self.client.event(
                    self.state,
                    job["job_id"],
                    f"{label}: {message}",
                    {
                        **payload,
                        "phase": label,
                        "progress": round(mapped, 2),
                        "current": round(mapped, 2),
                        "total": 100,
                    },
                )

            return callback

        if op == "standardize" and not bool(options.get("dry_run", False)):
            source_output_dir = Path(
                location.get("output_dir") or get_default_output_dir(source_root)
            ).expanduser()
            source_output_dir = _require_within(
                source_output_dir,
                self.writable_roots,
                "standardize source cache directory",
            )
            run_precompute(
                root=source_root,
                repo_id=location["dataset_key"],
                output_dir=source_output_dir,
                prepare_videos=True,
                prepare_csv=True,
                prepare_workers=max(1, int(options.get("workers") or 8)),
                data_version=options.get("data_version"),
                progress_callback=scaled_progress(
                    0,
                    25,
                    f"Step 1/{standardize_step_count} source cache",
                ),
                show_progress=False,
            )

        if options.get("urdf_path"):
            options["urdf_path"] = _require_within(
                Path(options["urdf_path"]),
                [*self.allowed_roots, Path(__file__).resolve().parents[2]],
                "robot model",
            )
        if job.get("_operator_version"):
            options["operator_version"] = job["_operator_version"]
        result = run_preprocess_op(
            op,
            src_root=source_root,
            out_root=out_root,
            progress_callback=(
                scaled_progress(
                    25,
                    standardize_write_end,
                    f"Step 2/{standardize_step_count} standardize",
                )
                if op == "standardize" and not bool(options.get("dry_run", False))
                else progress
            ),
            **options,
        )
        if op == "standardize" and delete_episode_ids:
            if bool(options.get("dry_run", False)):
                result.summary["delete_episodes"] = delete_episode_ids
            else:
                self.client.event(
                    self.state,
                    job["job_id"],
                    f"Step 3/{standardize_step_count} delete episodes: {delete_episode_ids}",
                    {"current": 60, "total": 100},
                )
                standardized_dataset = _AgentDataset(result.repo_id, result.out_root)
                delete_result = delete_episodes_inplace(
                    standardized_dataset,
                    delete_episode_ids,
                    static_folder=None,
                    log=lambda message: self.client.event(self.state, job["job_id"], message),
                )
                result.total_episodes = int(delete_result["new_total_episodes"])
                result.total_frames = int(standardized_dataset.total_frames or result.total_frames)
                result.summary["delete_episodes"] = delete_result["deleted_episode_ids"]
                result.summary["episodes_after_delete"] = delete_result["new_total_episodes"]
        if op == "standardize" and not bool(options.get("dry_run", False)):
            output_dir = _require_within(
                get_default_output_dir(result.out_root),
                self.writable_roots,
                "standardize output cache directory",
            )
            run_precompute(
                root=result.out_root,
                repo_id=result.repo_id,
                output_dir=output_dir,
                prepare_videos=True,
                prepare_csv=True,
                prepare_workers=max(1, int(options.get("workers") or 8)),
                fix_episode_indices_enabled=True,
                annotate=True,
                write_parquet=True,
                force_recompute_stage=True,
                write_subtask=True,
                overwrite_csv=True,
                data_version=options.get("data_version"),
                progress_callback=scaled_progress(
                    standardize_cache_start,
                    98,
                    f"Step {standardize_step_count}/{standardize_step_count} output cache",
                ),
                show_progress=False,
            )
            uploaded_files = self._upload_viewer_artifacts(
                job,
                output_dir / "static",
                derived=True,
            )
            self.client.event(
                self.state,
                job["job_id"],
                (
                    f"Step {standardize_step_count}/{standardize_step_count} output cache: "
                    f"Precompute complete; uploaded {uploaded_files} viewer artifacts"
                ),
                {"status": "done", "current": 100, "total": 100},
            )
        if result.summary.get("native_images") and not result.dry_run:
            output_dir = _require_within(
                get_default_output_dir(result.out_root), self.writable_roots, "output cache directory"
            )
            run_precompute(
                root=result.out_root,
                repo_id=result.repo_id,
                output_dir=output_dir,
                visualize_only=True,
                prepare_workers=max(1, int(options.get("workers") or 4)),
                progress_callback=scaled_progress(85, 98, "Output viewer cache"),
                show_progress=False,
            )
            uploaded_files = self._upload_viewer_artifacts(job, output_dir / "static", derived=True)
        payload = {"preprocess": _json_value(result)}
        if not result.dry_run:
            payload["dataset_location"] = _dataset_payload(
                Path(result.out_root),
                node_name=self.name,
                metadata={
                    "data_version": result.summary.get("data_version"),
                    "stage": "standard" if op == "standardize" else "raw",
                    "derived_from_location_id": job.get("location_id"),
                    "derived_by_operation": job.get("operation"),
                },
            )
            if op == "standardize" or result.summary.get("native_images"):
                payload["viewer_cache"] = {
                    "output_dir": str(output_dir),
                    "uploaded_files": uploaded_files,
                }
        from lerobot.data_platform.operators.dataset_versions import publish_preprocess_version

        return publish_preprocess_version(self, job, source_root, payload)

    def _run_subtract(self, job, source_root):
        options = dict(job["options"])
        locations = options.get("_source_locations") or []
        ids = options.get("source_location_ids") or []
        if (
            len(locations) < 2
            or [row["location_id"] for row in locations] != ids
            or ids[0] != job["location_id"]
        ):
            raise ValueError("Subtract requires matching registered inputs")
        if any(row["node_id"] != self.state.node_id for row in locations):
            raise ValueError("Subtract inputs must belong to this node")
        roots = [
            _require_within(Path(row["root"]), self.allowed_roots, "subtract source") for row in locations
        ]
        if roots[0] != source_root or len(set(roots)) != len(roots):
            raise ValueError("Subtract inputs must be distinct")
        if options.get("version_inputs"):
            from lerobot.data_platform.lifecycle import dataset_snapshot

            expected = {
                str(Path(row["version"]["root"]).resolve()): row["version"]["fingerprint"]
                for row in options["version_inputs"]
            }
            if set(expected) != {str(root) for root in roots} or any(
                dataset_snapshot(root)[0] != expected[str(root)] for root in roots
            ):
                raise ValueError("Versioned Subtract input changed")
        from lerobot.data_platform.operators import ExecutionContext, execute_operator, get_operator
        from lerobot.data_platform.operators.schema import validate_parameters

        params = {
            key: value
            for key, value in options.items()
            if key
            not in {
                "version_inputs",
                "parents",
                "source_batches",
                "task_config",
                "target",
                "source_location_ids",
                "_source_locations",
                "out_root",
            }
        }
        params = validate_parameters(get_operator("preprocess.subtract"), params)
        output = Path(options.get("out_root") or default_preprocess_path(source_root, "subtract"))
        for source in roots:
            output = _require_safe_output(output, source, self.writable_roots)
        result = execute_operator(
            "preprocess.subtract",
            ExecutionContext(
                source_root,
                output_root=output,
                source_roots=tuple(roots),
                progress=lambda payload: self.client.event(
                    self.state, job["job_id"], str(payload.get("message") or "Subtract"), payload
                ),
            ),
            params,
        )
        payload = {"preprocess": _json_value(result)}
        if not result.dry_run:
            payload["dataset_location"] = _dataset_payload(
                result.out_root,
                node_name=self.name,
                metadata={
                    "derived_from_location_id": job["location_id"],
                    "derived_from_location_ids": ids,
                    "derived_by_operation": job["operation"],
                },
            )
        return payload

    def _run_merge(self, job: dict, source_root: Path) -> dict:
        options = dict(job.get("options") or {})
        for key in ("version_inputs", "target", "parents", "source_batches", "task_config"):
            options.pop(key, None)
        unexpected = sorted(set(options) - _MERGE_OPTION_KEYS)
        if unexpected:
            raise ValueError(f"unsupported remote merge options: {unexpected}")
        source_ids = options.pop("source_location_ids", None)
        locations = options.pop("_source_locations", None)
        if (
            not isinstance(source_ids, list)
            or len(source_ids) < 2
            or any(not isinstance(value, str) or not value for value in source_ids)
            or len(set(source_ids)) != len(source_ids)
            or source_ids[0] != job.get("location_id")
            or not isinstance(locations, list)
            or len(locations) != len(source_ids)
            or any(not isinstance(item, dict) for item in locations)
        ):
            raise ValueError("merge requires matching source_location_ids and control-plane source locations")
        if [item.get("location_id") for item in locations] != source_ids or any(
            item.get("node_id") != self.state.node_id for item in locations
        ):
            raise ValueError("merge sources must belong to this Agent and match the queued locations")
        roots = [
            _require_within(Path(item["root"]), self.allowed_roots, "merge source") for item in locations
        ]
        if job["options"].get("version_inputs"):
            from lerobot.data_platform.lifecycle import dataset_snapshot

            expected = {
                str(Path(value["version"]["root"]).resolve()): value["version"]["fingerprint"]
                for value in job["options"]["version_inputs"]
            }
            if set(expected) != {str(root) for root in roots} or any(
                dataset_snapshot(root)[0] != expected[str(root)] for root in roots
            ):
                raise ValueError("Versioned Merge input changed")
        if roots[0] != source_root or len(set(roots)) != len(roots):
            raise ValueError("merge source roots must be distinct and start with the job source")
        validate_merge_sources(
            roots,
            dimension_policy=options.get("dimension_policy", "strict"),
            dimension_names=options.get("dimension_names"),
            dimension_indices=options.get("dimension_indices"),
            padding_value=options.get("padding_value", 0),
        )
        excluded = options.get("exclude_episodes")
        if excluded is not None:
            if not isinstance(excluded, list) or len(excluded) != len(roots):
                raise ValueError("exclude_episodes must align with source_location_ids")
            options["exclude_episodes"] = [
                _parse_episode_ids(value) if value not in (None, "", []) else [] for value in excluded
            ]
        if "dry_run" in options and not isinstance(options["dry_run"], bool):
            raise ValueError("dry_run must be a boolean")
        if "workers" in options and (type(options["workers"]) is not int or options["workers"] < 1):
            raise ValueError("workers must be a positive integer")
        requested_output = str(options.pop("out_root", "") or "").strip()
        out_root = (
            Path(requested_output).expanduser()
            if requested_output
            else default_preprocess_path(source_root, "merge")
        )
        for root in roots:
            out_root = _require_safe_output(out_root, root, self.writable_roots)
        output_dir = _require_within(
            get_default_output_dir(out_root), self.writable_roots, "merge output cache"
        )
        for root in roots:
            _require_safe_output(output_dir, root, self.writable_roots)
        if output_dir.exists() and not options.get("dry_run", False):
            raise FileExistsError(f"Output cache already exists; choose a fresh output: {output_dir}")
        source_static_dirs = [
            _require_within(
                Path(item.get("output_dir") or get_default_output_dir(root)),
                self.allowed_roots + self.writable_roots,
                "merge source cache",
            )
            / "static"
            for item, root in zip(locations, roots, strict=True)
        ]

        def progress(start: int, end: int):
            def callback(payload: dict) -> None:
                total = float(payload.get("total") or 0)
                fraction = (
                    float(payload["progress"]) / 100
                    if "progress" in payload
                    else float(payload.get("current") or 0) / total
                    if total
                    else 0
                )
                self.client.event(
                    self.state,
                    job["job_id"],
                    str(payload.get("message") or "Merge progress"),
                    {
                        **payload,
                        "status": "running",
                        "phase": "merge" if start == 0 else "output_viewer",
                        "progress": start + (end - start) * min(1, max(0, fraction)),
                        "current": start + (end - start) * min(1, max(0, fraction)),
                        "total": 100,
                    },
                )

            return callback

        result = run_preprocess_op(
            "merge",
            src_root=source_root,
            src_roots=roots,
            out_root=out_root,
            src_static_dirs=source_static_dirs,
            out_static_dir=output_dir / "static",
            progress_callback=progress(0, 75),
            **options,
        )
        result.summary["source_location_ids"] = source_ids
        if result.dry_run:
            return {"preprocess": _json_value(result)}
        output_profile = resolve_processing_profile(result.out_root)
        run_precompute(
            root=result.out_root,
            repo_id=result.repo_id,
            output_dir=output_dir,
            prepare_videos=True,
            prepare_csv=True,
            overwrite_csv=True,
            prepare_workers=options.get("workers", 8),
            data_version=output_profile.legacy_data_version,
            progress_callback=progress(75, 99),
            show_progress=False,
        )
        uploaded_files = self._upload_viewer_artifacts(job, output_dir / "static", derived=True)
        result.summary["csv_cache"] = "rebuilt_from_output"
        return {
            "preprocess": _json_value(result),
            "dataset_location": _dataset_payload(
                Path(result.out_root),
                node_name=self.name,
                metadata={
                    "data_version": output_profile.legacy_data_version,
                    "derived_from_location_id": job["location_id"],
                    "derived_from_location_ids": source_ids,
                    "derived_by_operation": job["operation"],
                    "stage": "standard"
                    if all((item.get("metadata") or {}).get("stage") == "standard" for item in locations)
                    else "raw",
                },
            ),
            "viewer_cache": {"output_dir": str(output_dir), "uploaded_files": uploaded_files},
        }

    def _run_source_mutation(self, job: dict, source_root: Path, location: dict) -> dict:
        if not self.allow_source_mutations:
            raise PermissionError("source mutations are disabled on this Agent")
        source_root = _require_within(source_root, self.writable_roots, "source mutation dataset")
        op = str(job["operation"]).removeprefix("mutation.")
        if op not in _SOURCE_MUTATION_OPS:
            raise ValueError(f"unsupported remote source mutation: {op}")
        completed = self._load_completed_mutation(job["job_id"])
        if completed is not None:
            self.client.event(
                self.state,
                job["job_id"],
                "Mutation already completed locally; reusing the recorded result",
            )
            return completed
        options = dict(job.get("options") or {})
        actor = dict(options.pop("requested_by", {}) or {})
        reason = str(options.pop("reason", "") or "").strip()
        dry_run = bool(options.get("dry_run", False))
        backup = None
        backup_root = None
        if not dry_run:
            backup_root = (
                source_root.parent
                / ".data-platform-backups"
                / source_root.name
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}-{job['job_id']}"
            )
            backup_root = _require_within(
                backup_root,
                self.writable_roots,
                "source mutation backup directory",
            )
        audit_dir = get_default_output_dir(source_root) / "static"
        event_id = uuid.uuid4().hex
        audit_details = {
            "job_id": job["job_id"],
            "node": self.name,
            "reason": reason,
            "options": options,
        }
        append_operation_event(
            audit_dir,
            f"remote_{op}",
            status="started",
            phase="request",
            source="agent",
            dataset_keys=[location.get("dataset_key")],
            dataset_roots=[source_root],
            episode_ids=(_parse_episode_ids(options.get("episodes")) if op == "delete_episodes" else []),
            details=audit_details,
            actor=actor,
            event_id=event_id,
        )

        def progress(payload: dict) -> None:
            message = str(payload.get("message") or payload.get("step") or "Mutation progress")
            self.client.event(self.state, job["job_id"], message, payload)

        try:
            if backup_root is not None:
                self.client.event(
                    self.state,
                    job["job_id"],
                    f"Creating persistent source backup: {backup_root}",
                )
                backup = _PersistentDatasetBackup(
                    source_root,
                    backup_root,
                    job_id=job["job_id"],
                    operation=op,
                )
            if op == "delete_episodes":
                episode_ids = _parse_episode_ids(options.pop("episodes", None))
                dataset = _AgentDataset(str(location.get("dataset_key") or source_root.name), source_root)
                static_folder = None
                output_dir = Path(
                    location.get("output_dir") or get_default_output_dir(source_root)
                ).expanduser()
                if _is_within(output_dir, self.writable_roots) and (output_dir / "static").is_dir():
                    static_folder = output_dir / "static"
                result = delete_episodes_inplace(
                    dataset,
                    episode_ids,
                    static_folder=static_folder,
                    log=lambda message: self.client.event(self.state, job["job_id"], message),
                )
                result_payload = {"delete": result}
                affected_episode_ids = episode_ids
            elif op == "trim_episode":
                from lerobot.data_platform.precompute.preprocess.flag_fixes import trim_episode_inplace

                episode, start, end = (options.get(key) for key in ("episode_id", "start_frame", "end_frame"))
                if (
                    any(type(value) is not int for value in (episode, start, end))
                    or episode < 0
                    or start < 0
                    or end < start
                ):
                    raise ValueError("A valid episode_id and inclusive integer frame range are required")
                if not reason:
                    raise ValueError("reason is required for trimming source frames")
                output_dir = Path(
                    location.get("output_dir") or get_default_output_dir(source_root)
                ).expanduser()
                static_dir = _require_within(output_dir / "static", self.writable_roots, "trim cache")
                self.client.event(
                    self.state, job["job_id"], "Trimming episode data and videos; rebuilding metadata"
                )
                result_payload = {
                    "trim": trim_episode_inplace(source_root, episode, start, end, static_dir=static_dir)
                }
                affected_episode_ids = [episode]
            elif op == "repair_v3_video_timestamps":
                result = repair_v3_video_timestamps(
                    source_root,
                    dry_run=dry_run,
                    progress_callback=progress,
                )
                result_payload = {"preprocess": _json_value(result)}
                affected_episode_ids = []
            else:
                episode_ids = (
                    _parse_episode_ids(options.pop("episode_ids"))
                    if options.get("episode_ids") not in (None, "", [])
                    else None
                )
                result = run_preprocess_op(
                    "value_edit",
                    src_root=source_root,
                    in_place=True,
                    episode_ids=episode_ids,
                    progress_callback=progress,
                    **options,
                )
                result_payload = {"preprocess": _json_value(result)}
                affected_episode_ids = episode_ids or []
        except Exception as exc:
            restored = False
            if backup is not None:
                try:
                    backup.restore()
                    restored = True
                    self.client.event(
                        self.state,
                        job["job_id"],
                        f"Mutation failed; restored source from {backup.backup_root}",
                    )
                except Exception:
                    logging.exception("Could not restore source mutation backup %s", backup.backup_root)
            append_operation_event(
                audit_dir,
                f"remote_{op}",
                status="failed",
                source="agent",
                dataset_keys=[location.get("dataset_key")],
                dataset_roots=[source_root],
                details={
                    **audit_details,
                    "error": str(exc),
                    "backup_root": str(backup_root) if backup_root is not None else None,
                    "restored": restored,
                },
                actor=actor,
                parent_event_id=event_id,
            )
            raise

        source_changed = not dry_run
        append_operation_event(
            audit_dir,
            f"remote_{op}",
            status="success",
            source="agent",
            dataset_keys=[location.get("dataset_key")],
            dataset_roots=[source_root],
            episode_ids=affected_episode_ids,
            details={
                **audit_details,
                "backup_root": str(backup_root) if backup_root is not None else None,
                "result": result_payload,
            },
            actor=actor,
            parent_event_id=event_id,
        )
        completed_result = {
            **result_payload,
            "backup_root": str(backup_root) if backup_root is not None else None,
            "source_changed": source_changed,
            "dataset_location": _dataset_payload(source_root, node_name=self.name),
        }
        self._write_completed_mutation(job["job_id"], completed_result)
        return completed_result

    def _sync_results(self, *, background: bool) -> None:
        """Keep one result transfer independent of job claims and heartbeats."""
        thread = getattr(self, "_result_sync_thread", None)
        if thread is not None and thread.is_alive():
            return
        if time.monotonic() - getattr(self, "_last_result_sync_at", 0) < 10:
            return

        def run():
            from lerobot.data_platform.dataset_results import sync_agent_results

            worker = self
            client = None
            try:
                if background:
                    client = AgentClient(self.client.server_url, verify=self.client.verify)
                    worker = copy.copy(self)
                    worker.client = client
                    worker.client.worker_instance_id = self.client.worker_instance_id
                sync_agent_results(worker)
            except Exception:
                logging.exception("Dataset result synchronization will retry on the next sync poll")
            finally:
                if client is not None:
                    client.session.close()
                self._last_result_sync_at = time.monotonic()

        if background:
            self._result_sync_thread = threading.Thread(
                target=run, daemon=True, name="data-platform-result-sync"
            )
            self._result_sync_thread.start()
        else:
            run()

    def run_forever(self, *, poll_seconds: float, sync_seconds: float) -> None:
        from lerobot.data_platform.rolling_deployment import ExecutorGeneration

        with ExecutorGeneration.from_env() as generation:
            self._run_forever(generation, poll_seconds=poll_seconds, sync_seconds=sync_seconds)

    def _run_forever(self, generation, *, poll_seconds: float, sync_seconds: float) -> None:
        dispatcher = _ExecutionDispatcher(self) if isinstance(self.client, AgentClient) else None
        last_sync_at = 0.0
        while True:
            now = time.monotonic()
            try:
                if dispatcher is not None:
                    dispatcher.reconcile()
                if generation.draining:
                    result_sync = getattr(self, "_result_sync_thread", None)
                    if dispatcher is None or (
                        not dispatcher.threads
                        and not dispatcher.supervisor.pending_here()
                        and not (result_sync and result_sync.is_alive())
                    ):
                        return
                    time.sleep(max(0.5, poll_seconds))
                    continue
                # Worker threads keep running while the main loop refreshes the dataset catalog.
                should_sync = now - last_sync_at >= max(10.0, sync_seconds)
                worked = self.run_once(
                    sync=should_sync,
                    dispatch=dispatcher.dispatch if dispatcher is not None else None,
                    allow_claim=dispatcher.available() if dispatcher is not None else True,
                )
                if should_sync:
                    last_sync_at = now
            except Exception:
                logging.exception("Agent poll failed")
                worked = False
            if not worked:
                time.sleep(max(0.5, poll_seconds))

    def close(self) -> None:
        """Stop background reads and release transports when replacing a local executor Agent."""
        cleanup = getattr(self, "_source_cleanup", None)
        if cleanup is not None:
            cleanup.close()
        stop = getattr(self, "_caption_read_stop", None)
        if stop is not None:
            stop.set()
        for thread in getattr(self, "_caption_read_threads", []):
            thread.join(timeout=45)
        self.client.session.close()

    def _start_caption_reads(self) -> None:
        """Serve reads independently of model jobs, uploads, and heartbeats."""
        from lerobot.data_platform.dataset_captions import read_dataset_caption

        if any(thread.is_alive() for thread in getattr(self, "_caption_read_threads", [])):
            return
        self._caption_video_lock = threading.Lock()
        self._caption_read_threads = []
        self._caption_read_stop = stop = threading.Event()

        def serve():
            client = AgentClient(self.client.server_url, verify=self.client.verify)
            try:
                while not stop.is_set():
                    try:
                        read = client._request(
                            "GET", "/api/agents/caption-reads", token=self.state.node_token, timeout=(10, 30)
                        ).get("read")
                        if not read:
                            stop.wait(0.5)
                            continue
                        try:
                            status, body, binary = read_dataset_caption(self, read)
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            status = 404 if isinstance(exc, FileNotFoundError) else 422
                            body = json.dumps(
                                {"error": f"Invalid dataset caption: {type(exc).__name__}"}
                            ).encode()
                            binary = False
                        client._request(
                            "POST",
                            f"/api/agents/caption-reads/{read['request_id']}",
                            token=self.state.node_token,
                            data=body,
                            params={"status": status},
                            headers={
                                "Content-Type": "application/octet-stream",
                                "X-Caption-Read-Format": "bytes" if binary else "json",
                            },
                            timeout=(10, 30),
                        )
                    except Exception:
                        logging.exception("Caption read transport will retry")
                        stop.wait(5)
            finally:
                client.session.close()

        for number in range(2):
            thread = threading.Thread(target=serve, daemon=True, name=f"data-platform-caption-read-{number}")
            self._caption_read_threads.append(thread)
            thread.start()


class _ExecutionDispatcher:
    """Keep supervised jobs independent while the main Agent continues polling."""

    def __init__(self, agent: DataPlatformAgent):
        from lerobot.data_platform.execution import ExecutionSupervisor

        self.agent = agent
        self.supervisor = ExecutionSupervisor(agent)
        self.threads: dict[str, tuple[threading.Thread, dict]] = {}
        self.job_limit = agent_slots()

    def _worker_agent(self, job: dict) -> DataPlatformAgent:
        worker = copy.copy(self.agent)
        worker.client = AgentClient(self.agent.client.server_url, verify=self.agent.client.verify)
        worker.client.worker_instance_id = self.agent.client.worker_instance_id
        worker.client.activate(job)
        return worker

    def _launch(self, job: dict, *, marker: dict | None = None) -> None:
        from lerobot.data_platform.execution import ExecutionSupervisor, StopRequestedError

        attempt_id = job["execution"]["attempt_id"]
        lock = self.supervisor.acquire_supervision(attempt_id)
        if lock is None:
            return
        if marker is not None:
            marker = json.loads((self.supervisor.root / attempt_id / "marker.json").read_text())
            if marker.get("acknowledged"):
                lock.close()
                return
        worker = self._worker_agent(job)

        def run() -> None:
            supervisor = ExecutionSupervisor(worker)
            try:
                if marker is None:
                    supervisor.start(job)
                else:
                    supervisor.monitor(marker, None)
            except Exception as exc:
                logging.exception("Execution requires reconciliation for job %s", job["job_id"])
                path = supervisor.root / attempt_id / "marker.json"
                if not path.exists():
                    try:
                        worker.client.complete(
                            worker.state,
                            job["job_id"],
                            status="cancelled" if isinstance(exc, StopRequestedError) else "error",
                            error=str(exc),
                        )
                        supervisor._release(supervisor.root / attempt_id / "reservation.json")
                    except Exception:
                        logging.exception("Could not report start failure for job %s", job["job_id"])
            finally:
                worker.client.session.close()
                lock.close()

        thread = threading.Thread(target=run, daemon=True, name=f"data-platform-job-{job['job_id'][:8]}")
        self.threads[attempt_id] = (thread, job)
        thread.start()

    def reconcile(self) -> None:
        from lerobot.data_platform.execution import atomic_json
        from lerobot.data_platform.rolling_deployment import execution_belongs_here

        for attempt_id, (thread, _) in list(self.threads.items()):
            if not thread.is_alive():
                del self.threads[attempt_id]
        for launch in sorted(self.supervisor.root.glob("*/launch.json")):
            marker_path = launch.parent / "marker.json"
            if not marker_path.exists():
                record = json.loads(launch.read_text())
                if "job" in record:
                    atomic_json(marker_path, record)
        for path in sorted(self.supervisor.root.glob("*/marker.json")):
            marker = json.loads(path.read_text())
            if not execution_belongs_here(marker["job"]):
                continue
            attempt_id = marker["job"]["execution"]["attempt_id"]
            if (
                marker.get("acknowledged")
                and marker.get("reservation")
                and Path(marker["reservation"]).exists()
            ):
                self.supervisor._release(marker["reservation"])
            if not marker.get("acknowledged") and attempt_id not in self.threads:
                self._launch(marker["job"], marker=marker)
        self.supervisor.reconcile_unstarted(self.threads)

    def available(self) -> bool:
        from lerobot.data_platform.job_concurrency import PARALLEL_OPERATIONS

        active = [job for _, job in self.threads.values()]
        return not active or (
            len(active) < self.job_limit and all(job["operation"] in PARALLEL_OPERATIONS for job in active)
        )

    def dispatch(self, job: dict) -> None:
        self._launch(job)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Data Platform node agent.")
    default_allowed_roots = [
        Path(value)
        for value in os.environ.get("DATA_PLATFORM_AGENT_ALLOWED_ROOTS", "").split(os.pathsep)
        if value.strip()
    ]
    default_writable_roots = [
        Path(value)
        for value in os.environ.get("DATA_PLATFORM_AGENT_WRITABLE_ROOTS", "").split(os.pathsep)
        if value.strip()
    ]
    parser.add_argument(
        "--server-url",
        default=os.environ.get("DATA_PLATFORM_SERVER_URL", ""),
        help="HTTPS URL of server A.",
    )
    parser.add_argument(
        "--name",
        default=os.environ.get("DATA_PLATFORM_AGENT_NAME", socket.gethostname()),
        help="Stable unique node name.",
    )
    parser.add_argument("--allowed-root", type=Path, action="append", default=default_allowed_roots)
    parser.add_argument("--writable-root", type=Path, action="append", default=default_writable_roots)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("DATA_PLATFORM_AGENT_STATE", "~/.config/data-platform/agent.json")),
    )
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--sync-seconds", type=float, default=300.0)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument(
        "--allow-source-mutations",
        action="store_true",
        default=_env_bool("DATA_PLATFORM_AGENT_ALLOW_SOURCE_MUTATIONS"),
        help="Allow centrally approved Admin jobs to modify source datasets in writable roots.",
    )
    parser.add_argument("--ca-bundle", type=Path, default=None)
    parser.add_argument(
        "--insecure", action="store_true", help="Disable TLS verification for development only."
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.server_url:
        parser.error("--server-url or DATA_PLATFORM_SERVER_URL is required")
    if not args.allowed_root:
        parser.error("--allowed-root or DATA_PLATFORM_AGENT_ALLOWED_ROOTS is required")
    if not args.writable_root:
        parser.error("--writable-root or DATA_PLATFORM_AGENT_WRITABLE_ROOTS is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    verify: bool | str = False if args.insecure else str(args.ca_bundle) if args.ca_bundle else True
    agent = DataPlatformAgent(
        client=AgentClient(args.server_url, verify=verify),
        state_path=args.state_file,
        name=args.name,
        allowed_roots=args.allowed_root,
        writable_roots=args.writable_root,
        enrollment_token=os.environ.get("DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN", ""),
        lease_seconds=args.lease_seconds,
        allow_source_mutations=args.allow_source_mutations,
    )
    if args.once:
        agent.run_once(sync=True)
        return
    agent.run_forever(poll_seconds=args.poll_seconds, sync_seconds=args.sync_seconds)


if __name__ == "__main__":
    main()

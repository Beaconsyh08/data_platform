"""Node agent for executing Data Platform jobs beside local datasets."""

from __future__ import annotations

import argparse
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
from lerobot.data_platform.operation_log import append_operation_event
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
    "convert_action",
    "convert_v3",
    "drop_field",
    "merge",
    "smooth_action",
    "split",
    "standardize",
    "value_edit",
}
_SOURCE_MUTATION_OPS = {
    "delete_episodes",
    "repair_v3_video_timestamps",
    "value_edit",
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


def discover_datasets(roots: list[Path], *, node_name: str) -> list[dict]:
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
                    logging.warning("Could not inspect dataset %s: %s", current, exc)
                continue
            try:
                children = sorted(current.iterdir(), reverse=True)
            except OSError:
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

    def heartbeat(self, state: AgentState, capabilities: dict) -> None:
        self._request(
            "POST",
            "/api/agents/heartbeat",
            token=state.node_token,
            json={"capabilities": capabilities},
        )

    def sync_locations(self, state: AgentState, locations: list[dict]) -> list[dict]:
        response = self._request(
            "POST",
            "/api/agents/locations/sync",
            token=state.node_token,
            json={"locations": locations},
        )
        return list(response["locations"])

    def claim(self, state: AgentState, *, lease_seconds: int) -> dict | None:
        response = self._request(
            "POST",
            "/api/agents/jobs/claim",
            token=state.node_token,
            json={"lease_seconds": lease_seconds, "worker_instance_id": self.worker_instance_id},
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
    ) -> None:
        endpoint = "derived-artifacts" if derived else "artifacts"
        with Path(path).open("rb") as handle:
            self._request(
                "PUT",
                f"/api/agents/jobs/{job_id}/{endpoint}/{relative_path.as_posix()}",
                token=state.node_token,
                data=handle,
                headers={"Content-Type": "application/octet-stream"},
                timeout=(10, 3600),
            )

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
    ):
        self.client = client
        self.state_path = Path(state_path).expanduser()
        self.name = str(name)
        self.allowed_roots = _resolve_roots(allowed_roots)
        self.writable_roots = _resolve_roots(writable_roots)
        self.enrollment_token = str(enrollment_token)
        self.lease_seconds = max(30, int(lease_seconds))
        self.allow_source_mutations = bool(allow_source_mutations)
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
        disk = {}
        for root in self.allowed_roots:
            try:
                usage = shutil.disk_usage(root)
                disk[str(root)] = {"total": usage.total, "free": usage.free}
            except OSError:
                continue
        operations = ["viewer.prepare", *[f"preprocess.{op}" for op in sorted(_PREPROCESS_OPS)]]
        if self.allow_source_mutations:
            operations.extend(f"mutation.{op}" for op in sorted(_SOURCE_MUTATION_OPS))
        return {
            "job_protocol": 2,
            "environment": os.environ.get("DATA_PLATFORM_ENV", "legacy"),
            "instance_id": os.environ.get("DATA_PLATFORM_INSTANCE_ID", ""),
            "release": os.environ.get("DATA_PLATFORM_RELEASE", "legacy"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "disk": disk,
            "data_profile_protocol": DATA_PROFILE_PROTOCOL,
            "operations": operations,
            "merge_alignment_protocol": 3,
            "source_mutations_enabled": self.allow_source_mutations,
            "task_config_protocol": TASK_CONFIG_PROTOCOL,
        }

    def sync_locations(self) -> list[dict]:
        discovered = discover_datasets(self.allowed_roots, node_name=self.name)
        return self.client.sync_locations(self.state, discovered)

    def run_once(self, *, sync: bool = True) -> bool:
        self.client.heartbeat(self.state, self.capabilities())
        if isinstance(self.client, AgentClient):
            from lerobot.data_platform.execution import ExecutionSupervisor

            supervisor = ExecutionSupervisor(self)
            if supervisor.reconcile():
                return True
        if sync:
            self.sync_locations()
        job = self.client.claim(self.state, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        job_id = job["job_id"]
        if job.get("execution"):
            try:
                supervisor.start(job)
            except Exception as exc:
                logging.exception("Execution requires reconciliation for job %s", job_id)
                marker = supervisor.root / job["execution"]["attempt_id"] / "marker.json"
                if not marker.exists():
                    self.client.complete(self.state, job_id, status="error", error=str(exc))
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
        require_dataset_operation(
            source_root, operation, data_version_override=(job.get("options") or {}).get("data_version")
        )
        if operation == "viewer.prepare":
            return self._prepare_viewer(job, source_root, location)
        if operation.startswith("preprocess."):
            return self._run_preprocess(job, source_root, location)
        if operation.startswith("mutation."):
            return self._run_source_mutation(job, source_root, location)
        raise ValueError(f"unsupported remote operation: {operation}")

    def _prepare_viewer(self, job: dict, source_root: Path, location: dict) -> dict:
        options = dict(job.get("options") or {})
        TaskConfigSnapshot.from_dict(options.get("task_config"))
        output_dir = Path(location.get("output_dir") or get_default_output_dir(source_root)).expanduser()
        output_dir = _require_within(output_dir, self.writable_roots, "viewer output directory")
        last_event_at = 0.0

        def progress(payload: dict) -> None:
            nonlocal last_event_at
            now = time.monotonic()
            if now - last_event_at < 1.0 and payload.get("status") != "error":
                return
            last_event_at = now
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
            progress = (
                {"current": round(98 + 2 * index / len(files), 2), "total": 100, "phase": "upload"}
                if derived
                else {"current": index, "total": len(files), "phase": "upload"}
            )
            self.client.event(
                self.state,
                job["job_id"],
                f"Uploading viewer artifact {index}/{len(files)}: {relative}",
                progress,
            )
            self.client.upload_artifact(
                self.state,
                job["job_id"],
                relative,
                path,
                derived=derived,
            )
        return len(files)

    def _run_preprocess(self, job: dict, source_root: Path, location: dict) -> dict:
        op = str(job["operation"]).removeprefix("preprocess.")
        if op not in _PREPROCESS_OPS:
            raise ValueError(f"unsupported remote preprocess op: {op}")
        if op == "merge":
            return self._run_merge(job, source_root)
        options = dict(job.get("options") or {})
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
                fraction = current / total if total > 0 else 0.0
                mapped = start + max(0.0, min(1.0, fraction)) * (end - start)
                message = str(payload.get("message") or payload.get("step") or label)
                self.client.event(
                    self.state,
                    job["job_id"],
                    f"{label}: {message}",
                    {**payload, "current": round(mapped, 2), "total": 100},
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
        return payload

    def _run_merge(self, job: dict, source_root: Path) -> dict:
        options = dict(job.get("options") or {})
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
                fraction = float(payload.get("current") or 0) / total if total else 0
                self.client.event(
                    self.state,
                    job["job_id"],
                    str(payload.get("message") or "Merge progress"),
                    {
                        **payload,
                        "status": "running",
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

    def run_forever(self, *, poll_seconds: float, sync_seconds: float) -> None:
        last_sync_at = 0.0
        while True:
            now = time.monotonic()
            should_sync = now - last_sync_at >= max(10.0, sync_seconds)
            try:
                worked = self.run_once(sync=should_sync)
                if should_sync:
                    last_sync_at = now
            except Exception:
                logging.exception("Agent poll failed")
                worked = False
            if not worked:
                time.sleep(max(0.5, poll_seconds))


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

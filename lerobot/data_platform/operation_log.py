"""Append-only operation audit logs for the Data Platform."""

from __future__ import annotations

import contextlib
import functools
import getpass
import gzip
import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from lerobot.data_platform.cache_files import compress_file

try:
    import fcntl
except ImportError:  # pragma: no cover - Data Platform is normally run on Linux.
    fcntl = None


LOG_FILENAME = "operation_log.jsonl"
LOG_MAX_BYTES = 32 * 1024 * 1024
LOG_BACKUP_COUNT = 4
SCHEMA_VERSION = 1
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "credential",
    "database_url",
)
_WRITE_LOCK = threading.Lock()


def should_audit_agent_request(
    path: str, status_code: int, payload: object, *, catalog_changed: bool = True
) -> bool:
    """Keep failures and real work while omitting successful control-plane polling."""
    import re

    if status_code >= 400:
        return True
    if path == "/api/agents/heartbeat":
        return False
    if path == "/api/agents/locations/sync":
        return catalog_changed
    if not isinstance(payload, dict):
        return True
    if path == "/api/agents/jobs/claim":
        return bool(payload.get("job"))
    if re.fullmatch(r"/api/agents/jobs/[^/]+/heartbeat", path):
        return bool(payload.get("stop_mode"))
    return True


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def sanitize_for_log(value: Any, *, _depth: int = 0) -> Any:
    """Make a bounded JSON value while redacting credentials."""
    if _depth >= 8:
        return "<max-depth>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 200:
                out["<truncated>"] = f"{len(value) - index} more fields"
                break
            out[str(key)] = (
                "<redacted>" if _is_sensitive_key(key) else sanitize_for_log(item, _depth=_depth + 1)
            )
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized = [sanitize_for_log(item, _depth=_depth + 1) for item in items[:500]]
        if len(items) > 500:
            sanitized.append(f"<{len(items) - 500} more items>")
        return sanitized
    if isinstance(value, str):
        import re

        value = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", r"\1<redacted>@", value)
        value = re.sub(r"(?i)(bearer\s+)[a-zA-Z0-9._~+/=-]+", r"\1<redacted>", value)
        return value if len(value) <= 4000 else value[:4000] + "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def summarize_location_sync_request(body: dict) -> dict:
    """Keep catalog-sync identity and metadata digests without copying the entire catalog."""
    locations = body.get("locations")
    if not isinstance(locations, list):
        return sanitize_for_log(body)
    summaries = []
    for location in locations[:500]:
        if not isinstance(location, dict):
            summaries.append(sanitize_for_log(location))
            continue
        metadata = sanitize_for_log(location.get("metadata") or {})
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        summaries.append(
            {
                **{key: location[key] for key in ("dataset_key", "root", "output_dir") if key in location},
                "metadata_summary": {
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "bytes": len(encoded),
                    "keys": list(metadata) if isinstance(metadata, dict) else [],
                },
            }
        )
    return sanitize_for_log({**body, "locations": summaries, "location_count": len(locations)})


def local_actor() -> dict[str, str]:
    try:
        username = getpass.getuser()
    except Exception:
        username = "unknown"
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"
    return {"username": username or "unknown", "hostname": hostname or "unknown"}


def _normalized_paths(log_dirs: Path | str | Iterable[Path | str | None] | None) -> list[Path]:
    if log_dirs is None:
        return []
    values = [log_dirs] if isinstance(log_dirs, (str, Path)) else list(log_dirs)
    paths = []
    seen = set()
    for value in values:
        if value is None:
            continue
        path = Path(value).expanduser()
        path = path if path.name == LOG_FILENAME else path / LOG_FILENAME
        key = str(path)
        if key not in seen:
            paths.append(path)
            seen.add(key)
    return paths


def build_operation_event(
    operation: str,
    *,
    status: str,
    phase: str = "result",
    source: str = "web",
    dataset_keys: Iterable[str] | None = None,
    dataset_roots: Iterable[Path | str] | None = None,
    episode_ids: Iterable[int] | None = None,
    details: dict | None = None,
    actor: dict | None = None,
    client: dict | None = None,
    event_id: str | None = None,
    parent_event_id: str | None = None,
) -> dict:
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    keys = [str(value) for value in (dataset_keys or []) if value not in (None, "")]
    roots = [str(value) for value in (dataset_roots or []) if value not in (None, "")]
    episodes = sorted({int(value) for value in (episode_ids or [])})
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id or uuid.uuid4().hex,
        "parent_event_id": parent_event_id,
        "timestamp": timestamp,
        "operation": str(operation),
        "phase": str(phase),
        "status": str(status),
        "source": str(source),
        "actor": sanitize_for_log(actor or local_actor()),
        "client": sanitize_for_log(client or {}),
        "dataset_keys": list(dict.fromkeys(keys)),
        "dataset_roots": list(dict.fromkeys(roots)),
        "episode_ids": episodes,
        "details": sanitize_for_log(details or {}),
    }
    # Keep the original reader contract for existing integrations.
    payload.update(
        {
            "time": timestamp,
            "op": payload["operation"],
            "dataset_key": payload["dataset_keys"][0] if payload["dataset_keys"] else None,
            "dataset_root": payload["dataset_roots"][0] if payload["dataset_roots"] else None,
        }
    )
    return payload


@contextlib.contextmanager
def _operation_log_lock(path: Path, *, write: bool):
    # A stable inode is required because rotation replaces the active log file.
    lock = path.with_name(f".{path.name}.lock")
    with _WRITE_LOCK:
        try:
            stream = lock.open("ab" if write else "rb")
        except FileNotFoundError:
            if write:
                raise
            yield
            return
        with stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def compress_rotated_operation_logs(path: Path) -> None:
    """Caller holds the ledger lock. Active append files remain plaintext."""
    for index in range(1, LOG_BACKUP_COUNT + 1):
        previous = path.with_name(f"{path.name}.{index}")
        if previous.is_file():
            compress_file(previous)


def _rotate_operation_log(path: Path, incoming_bytes: int) -> None:
    if not path.exists() or path.stat().st_size + incoming_bytes <= LOG_MAX_BYTES:
        return
    compressed = compress_file(path, remove_original=False)
    for index in range(LOG_BACKUP_COUNT, 1, -1):
        previous = path.with_name(f"{path.name}.{index - 1}.gz")
        if previous.exists():
            previous.replace(path.with_name(f"{path.name}.{index}.gz"))
    compressed.replace(path.with_name(f"{path.name}.1.gz"))
    path.unlink()


def _recent_log_lines(stream, limit: int, *, compressed: bool = False):
    """Yield newest lines lazily; the caller owns a rotation-safe open file descriptor."""
    if compressed:
        with gzip.GzipFile(fileobj=stream) as archive:
            for line in reversed(deque(archive, maxlen=limit)):
                yield line
        return
    position = stream.seek(0, os.SEEK_END)
    pending = b""
    count = 0
    while position:
        size = min(position, 64 * 1024)
        position -= size
        stream.seek(position)
        parts = (stream.read(size) + pending).split(b"\n")
        pending = parts[0]
        for line in reversed(parts[1:]):
            if line:
                yield line
                count += 1
                if count >= limit:
                    return
    if pending and count < limit:
        yield pending


def append_operation_event(
    log_dirs: Path | str | Iterable[Path | str | None] | None,
    operation: str,
    **event_fields,
) -> dict:
    """Append one identical event to each requested ledger."""
    payload = build_operation_event(operation, **event_fields)
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    for path in _normalized_paths(log_dirs):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _operation_log_lock(path, write=True):
                compress_rotated_operation_logs(path)
                _rotate_operation_log(path, len(encoded))
                with path.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
        except OSError as exc:
            logging.warning("Could not append operation audit log %s: %s", path, exc)
    return payload


def _event_timestamp(event: dict) -> str:
    return str(event.get("timestamp") or event.get("time") or "")


def summarize_operation_event(event: dict) -> dict:
    """Bound list payloads without altering the audit ledger or legacy detail responses."""
    result = {}
    for key in (
        "schema_version",
        "event_id",
        "parent_event_id",
        "timestamp",
        "time",
        "operation",
        "op",
        "phase",
        "status",
        "source",
        "dataset_key",
        "dataset_root",
    ):
        value = event.get(key)
        result[key] = (
            value[:500]
            if isinstance(value, str)
            else value
            if isinstance(value, (int, float, bool))
            else None
        )
    result["actor"] = {"username": str((event.get("actor") or {}).get("username") or "")[:200]}
    for key in ("dataset_keys", "dataset_roots", "episode_ids"):
        values = event.get(key) or []
        result[key] = [str(value)[:500] if not isinstance(value, int) else value for value in values[:20]]
    details = event.get("details") or {}
    result["details"] = {
        key: str(details[key])[:500]
        for key in ("job_id", "error", "duration_ms", "status_code")
        if key in details
    }
    result["summary"] = True
    return result


def read_operation_events(
    log_dirs: Path | str | Iterable[Path | str | None] | None,
    *,
    limit: int = 200,
    dataset_key: str | None = None,
    status: str | None = None,
    operation: str | None = None,
    event_id: str | None = None,
    summary: bool = False,
) -> list[dict]:
    """Read newest matching events, deduplicating mirrored ledgers."""
    limit = max(1, min(5000, int(limit)))
    candidates: list[dict] = []
    seen = set()
    status_aliases = {"success": {"success", "ok"}, "failed": {"failed", "error"}}
    accepted_statuses = status_aliases.get(status, {status}) if status else None
    for path in _normalized_paths(log_dirs):
        matched = 0
        # Open a consistent set of inodes under the rotation lock, then release it before
        # parsing/decompression. Writers can rotate/unlink these files without blocking reads.
        with contextlib.ExitStack() as stack:
            streams = []
            try:
                with _operation_log_lock(path, write=False):
                    for candidate in [
                        path,
                        *(
                            candidate
                            for i in range(1, LOG_BACKUP_COUNT + 1)
                            for candidate in (
                                path.with_name(f"{path.name}.{i}"),
                                path.with_name(f"{path.name}.{i}.gz"),
                            )
                        ),
                    ]:
                        with contextlib.suppress(FileNotFoundError):
                            streams.append(
                                (stack.enter_context(candidate.open("rb")), candidate.suffix == ".gz")
                            )
            except OSError:
                continue
            for stream, compressed in streams:
                try:
                    for line in _recent_log_lines(stream, max(5000, limit * 20), compressed=compressed):
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        keys = event.get("dataset_keys") or (
                            [event.get("dataset_key")] if event.get("dataset_key") else []
                        )
                        if dataset_key and dataset_key not in keys:
                            continue
                        if accepted_statuses and str(event.get("status") or "") not in accepted_statuses:
                            continue
                        event_operation = str(event.get("operation") or event.get("op") or "")
                        if operation and operation.lower() not in event_operation.lower():
                            continue
                        identity = (
                            event.get("event_id")
                            or hashlib.sha256(
                                json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
                            ).hexdigest()
                        )
                        if event_id and str(identity) != event_id:
                            continue
                        if str(identity) in seen:
                            continue
                        event.setdefault("event_id", str(identity))
                        event.setdefault("operation", event_operation)
                        event.setdefault("timestamp", _event_timestamp(event))
                        candidates.append(summarize_operation_event(event) if summary else event)
                        seen.add(str(identity))
                        matched += 1
                        if matched >= limit:
                            break
                except (OSError, EOFError):
                    continue
                if matched >= limit:
                    break
    candidates.sort(key=_event_timestamp, reverse=True)
    return candidates[:limit]


def _cli_value(argv: list[str], option: str) -> str | None:
    for index, value in enumerate(argv):
        if value == option and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(option + "="):
            return value.split("=", 1)[1]
    return None


def _sanitize_cli_argv(argv: list[str]) -> list[str]:
    sanitized = []
    redact_next = False
    for value in argv:
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        if value.startswith("--"):
            option, separator, _raw = value.partition("=")
            if _is_sensitive_key(option):
                sanitized.append(option + ("=<redacted>" if separator else ""))
                redact_next = not separator
                continue
        sanitized.append(value if len(value) <= 4000 else value[:4000] + "<truncated>")
    return sanitized


def audit_cli_main(func: Callable) -> Callable:
    """Record the complete lifecycle of a Data Platform CLI invocation."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        argv = list(sys.argv[1:])
        root_value = _cli_value(argv, "--root")
        output_value = _cli_value(argv, "--output-dir")
        run_visualize_value = str(_cli_value(argv, "--run-visualize") or "0").lower()
        root = Path(root_value).expanduser() if root_value else None
        if output_value:
            log_dir = Path(output_value).expanduser() / "static"
        elif root is not None and run_visualize_value not in {"0", "false", "no", "off"}:
            log_dir = root / "vis" / "_console" / "static"
        elif root is not None:
            log_dir = root.parent / "vis" / f"local_vis_{root.name or 'dataset'}" / "static"
        else:
            log_dir = None
        event_id = uuid.uuid4().hex
        started = time.perf_counter()
        details = {
            "argv": _sanitize_cli_argv(argv),
            "pid": os.getpid(),
            "actions": [value for value in argv if value.startswith("--") and value != "--root"],
        }
        append_operation_event(
            log_dir,
            "cli_invocation",
            status="started",
            phase="request",
            source="cli",
            dataset_roots=[root] if root is not None else [],
            details=details,
            event_id=event_id,
        )
        try:
            result = func(*args, **kwargs)
        except SystemExit as exc:
            status = "success" if exc.code in (None, 0) else "failed"
            append_operation_event(
                log_dir,
                "cli_invocation",
                status=status,
                phase="result",
                source="cli",
                dataset_roots=[root] if root is not None else [],
                details={
                    **details,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "exit_code": exc.code,
                },
                parent_event_id=event_id,
            )
            raise
        except BaseException as exc:
            append_operation_event(
                log_dir,
                "cli_invocation",
                status="failed",
                phase="result",
                source="cli",
                dataset_roots=[root] if root is not None else [],
                details={
                    **details,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                },
                parent_event_id=event_id,
            )
            raise
        append_operation_event(
            log_dir,
            "cli_invocation",
            status="success",
            phase="result",
            source="cli",
            dataset_roots=[root] if root is not None else [],
            details={**details, "duration_ms": round((time.perf_counter() - started) * 1000)},
            parent_event_id=event_id,
        )
        return result

    return wrapper

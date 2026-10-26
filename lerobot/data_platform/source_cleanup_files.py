"""Remove only owned cache artifacts after authoritative source-loss confirmation."""

from __future__ import annotations

import json
import shutil
import stat
import uuid
from pathlib import Path

from lerobot.data_platform.execution import atomic_json


def missing_witness(root: Path, allowed_roots: list[Path]) -> dict | None:
    """An unreadable source, dangling link or unavailable parent is not a deleted source."""
    root = Path(root).absolute()
    if not any(root.is_relative_to(Path(path).absolute()) for path in allowed_roots):
        return None
    try:
        root.lstat()
        return None
    except FileNotFoundError:
        pass
    except OSError:
        return None
    for parent in root.parents:
        if parent == Path("/"):
            return None
        try:
            value = parent.stat()
            if not stat.S_ISDIR(value.st_mode) or parent.is_symlink():
                return None
            # Recheck the exact entry after probing its readable parent.
            root.lstat()
            return None
        except FileNotFoundError:
            try:
                value = parent.stat()
            except FileNotFoundError:
                continue
            except OSError:
                return None
            return {"anchor": str(parent), "device": value.st_dev, "inode": value.st_ino}
        except OSError:
            return None
    return None


def remove_owned(path: Path, base: Path, *, protected_roots=()) -> None:
    """Keep deletion within a managed cache namespace; never follow directory symlinks."""
    path, base = Path(path).absolute(), Path(base).absolute()
    if path == base or not path.is_relative_to(base) or ".." in path.parts:
        raise PermissionError("Cache deletion must target a managed child")
    if not path.parent.resolve().is_relative_to(base.resolve()):
        raise PermissionError("Cache parent escapes its managed namespace")
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise PermissionError("Cache deletion escapes its managed namespace")
    if any(
        resolved == Path(root).resolve() or Path(root).resolve().is_relative_to(resolved)
        for root in protected_roots
    ):
        raise PermissionError("Cache is also used as a registered source")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def prune_viewer_cache(task: dict, writable_roots: list[Path], *, state_root: Path) -> None:
    from lerobot.data_platform.cli import get_default_output_dir

    output, source = Path(task["output_dir"]), Path(task["root"])
    if not output.exists() and not output.is_symlink():
        return
    if task.get("shared_cache"):
        raise PermissionError("Viewer cache is shared by another registered dataset")
    if output.is_symlink() or not any(
        output.resolve().is_relative_to(Path(root).resolve()) for root in writable_roots
    ):
        raise PermissionError("Viewer cache is outside configured writable roots")
    if output == source or (output / "meta/info.json").exists():
        raise PermissionError("Dataset directories cannot be removed as cache")
    protected = [Path(root).resolve() for root in task.get("protected_roots", [])]
    if any(root == output.resolve() or root.is_relative_to(output.resolve()) for root in protected):
        raise PermissionError("Viewer cache contains another registered source")
    if (output / "static").is_symlink():
        raise PermissionError("Viewer cache static directory must not be a symbolic link")
    manifest = output / "static/viewer_manifest.json"
    known = output.resolve() == get_default_output_dir(source).resolve()
    if manifest.is_file():
        value = json.loads(manifest.read_text())
        known = value.get("root") == str(source) and value.get("repo_id") == task["dataset_key"]
    token = task.get("token")
    receipt = state_root / "source-cleanup-receipts" / f"{uuid.UUID(token)}.json" if token else None
    entry = output.stat()
    identity = {"root": str(source), "output_dir": str(output), "device": entry.st_dev, "inode": entry.st_ino}
    if not manifest.exists() and receipt and receipt.is_file():
        known = known or json.loads(receipt.read_text()) == identity
    if not known:
        raise PermissionError("Viewer cache ownership cannot be verified")
    if receipt:
        atomic_json(receipt, identity)
    # A single-dataset console may store platform state beside its cache.
    platform_state = output / "management"
    critical = {
        "remote-cache",
        "remote_cache",
        "management",
        "lifecycle",
        "datasets_registry.json",
        "admin_auth.json",
        "admin_auth.sqlite3",
        ".data-platform-environment.json",
    }
    if (
        state_root.resolve().is_relative_to(output.resolve())
        or platform_state.exists()
        or (output / "static/lifecycle").exists()
    ):
        static = output / "static"
        if static.is_dir():
            for path in static.iterdir():
                if path.name not in critical and not path.name.startswith("operation_log.jsonl"):
                    remove_owned(path, static, protected_roots=protected)
        for name in (
            ".edited-csv.json",
            ".result-sync.json",
            ".result-sync-backups",
            ".viewer-publication.json",
            ".execution-owner.json",
        ):
            remove_owned(output / name, output, protected_roots=protected)
    else:
        remove_owned(output, output.parent, protected_roots=protected)


def prune_node_job_cache(
    state_root: Path, task: dict, job_ids: list[str], *, writable_roots: list[Path]
) -> None:
    protected = task.get("protected_roots", [])
    for job_id in job_ids:
        if len(job_id) != 36 or any(char not in "0123456789abcdef-" for char in job_id):
            raise ValueError("Invalid cleanup Job ID")
        for name in ("operator-work", "curation"):
            remove_owned(state_root / name / job_id, state_root, protected_roots=protected)
        remove_owned(state_root / "request-secrets" / f"{job_id}.json", state_root, protected_roots=protected)
        executions = state_root / "executions"
        if executions.is_dir():
            for marker_path in executions.glob("*/marker.json"):
                marker = json.loads(marker_path.read_text())
                if (marker.get("job") or {}).get("job_id") != job_id:
                    continue
                if not marker.get("acknowledged"):
                    raise RuntimeError("Execution has not acknowledged process exit and publication")
                staging = Path(marker["staging"])
                if not any(
                    staging.resolve().is_relative_to(Path(root).resolve())
                    for root in [state_root, *writable_roots]
                ):
                    raise PermissionError("Execution cache is outside current writable roots")
                expected = f".dp-{job_id}-{marker_path.parent.name}"
                if staging != marker_path.parent / "staging" and staging.name != expected:
                    raise PermissionError("Unrecognized execution staging directory")
                for child in ("cache", "source-cache", "vis", "previous-cache"):
                    if staging.is_dir():
                        remove_owned(staging / child, staging, protected_roots=protected)
                remove_owned(marker_path.parent, executions, protected_roots=protected)


def prune_server_job_cache(cache_root: Path, ledger_root: Path, job_ids: list[str]) -> None:
    for job_id in job_ids:
        if len(job_id) != 36 or any(char not in "0123456789abcdef-" for char in job_id):
            raise ValueError("Invalid cleanup Job ID")
        for path, base in (
            (cache_root / ".jobs" / job_id, cache_root),
            (ledger_root / "curation/.jobs" / job_id, ledger_root),
            (ledger_root / "curation/runs" / job_id, ledger_root),
            (ledger_root / "operator-results" / job_id, ledger_root),
        ):
            remove_owned(path, base)


def prune_server_location_cache(cache_root: Path, location_id: str) -> None:
    if len(location_id) != 36 or any(char not in "0123456789abcdef-" for char in location_id):
        raise ValueError("Invalid cleanup location ID")
    from lerobot.data_platform.dataset_results import result_lock

    with result_lock(cache_root / location_id):
        remove_owned(cache_root / location_id, cache_root)
        remove_owned(cache_root / ".history" / location_id, cache_root)
        for prefix in (f".{location_id}.backup-", f".{location_id}.stale-"):
            for path in cache_root.glob(prefix + "*"):
                remove_owned(path, cache_root)

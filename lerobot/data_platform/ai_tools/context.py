"""Authorized, bounded context for assistants; never read source rows or media."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy import select

from lerobot.data_platform.control_plane import DatasetLocation, RemoteJob, RemoteJobEvent
from lerobot.data_platform.dataset_references import dataset_references
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.management_storage import JobControl
from lerobot.data_platform.task_catalog import content_digest

KINDS = {"pipeline", "dataset", "quality", "job", "episode"}
TOOL_BYTES = 20 * 1024


def viewer_reference(location):
    """Offer only a registered, locally published Viewer; never inspect source rows or media."""
    metadata = location.get("metadata") or {}
    url = metadata.get("viewer_url")
    if not metadata.get("viewer_ready") or not isinstance(url, str):
        return None
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/episode_[0-9]+", url):
        return None
    if any(part in {".", ".."} for part in url.split("/")):
        return None
    try:
        cache = Path(metadata.get("cache_root") or "")
        manifest = cache / "static" / "viewer_manifest.json"
        if (
            not cache.is_absolute()
            or manifest.is_symlink()
            or not manifest.resolve().is_relative_to(cache.resolve())
        ):
            return None
        with manifest.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return None
        data = json.loads(raw)
        if data.get("version") != 1 or data.get("root") != location.get("root"):
            return None
        episode = int(url.rsplit("_", 1)[-1])
        if not any(item.get("episode_index") == episode for item in data.get("episodes", [])):
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return {"id": "viewer", "title": "Open prepared Viewer", "url": url}


def safe_text(value, limit=500):
    text = str(value)[:4000]
    text = re.sub(r"(?:https?://|[/\\]|[A-Za-z]:\\)\S+", "[redacted]", text)
    text = re.sub(
        r"(?i)(?:sk-[\w-]+|bearer\s+\S+|(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+)",
        "[redacted]",
        text,
    )
    text = re.sub(r"\b[A-Za-z0-9_=-]{32,}\b", "[redacted]", text)
    return text[:limit]


def diagnostic(value):
    """Classify free-form errors/events without disclosing raw diagnostics or SQL parameters."""
    text = str(value).lower()
    for words, label in (
        (("permission", "denied", "not authorized"), "Permission denied"),
        (("out of memory", "memoryerror", "oom"), "Insufficient memory"),
        (("already exists", "collision", "fileexists"), "Output already exists"),
        (("upload", "publish", "publication"), "Publication or upload event"),
        (("timeout", "connection", "unreachable"), "Connection failure or timeout"),
        (("checkpoint", "recover"), "Checkpoint or recovery event"),
        (("cancel", "stop requested"), "Cancellation event"),
        (("invalid", "missing", "unsupported"), "Input or capability validation event"),
    ):
        if any(word in text for word in words):
            return label
    return "Open the task for diagnostic details"


def safe_summary(value, depth=0, *, max_depth=4):
    """Only bounded statistics/labels; arbitrary nested reports are never forwarded."""
    if depth > max_depth:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            safe_text(k, 80): safe_summary(v, depth + 1, max_depth=max_depth)
            for k, v in list(value.items())[:40]
            if not re.search(r"(?i)path|root|token|secret|password|url|key|prompt|message|error", str(k))
        }
    if isinstance(value, list):
        return [safe_summary(v, depth + 1, max_depth=max_depth) for v in value[:20]]
    if isinstance(value, str):
        return safe_text(value, 200)
    return value if value is None or type(value) in {int, float, bool} else None


def bounded(value):
    encoded = json.dumps(value, ensure_ascii=False).encode()
    if len(encoded) > TOOL_BYTES:
        return {"unavailable": "Summary exceeds the context limit; open the source page.", "truncated": True}
    return value


def normalize_context(value, location_id):
    if value is None:
        return {"type": "pipeline", "target_id": location_id}
    if (
        not isinstance(value, dict)
        or set(value) - {"type", "target_id", "page"}
        or not {"type", "target_id"}.issubset(value)
        or value["type"] not in KINDS
    ):
        raise ValueError("Use a supported assistant context and target ID")
    if not isinstance(value["target_id"], str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]{1,128}", value["target_id"]
    ):
        raise ValueError("Invalid context target")
    if value["type"] in {"pipeline", "dataset"} and value["target_id"] != location_id:
        raise ValueError("Context must match the selected dataset")
    if value["type"] == "episode" and not re.fullmatch(r"0|[1-9][0-9]{0,8}", value["target_id"]):
        raise ValueError("Invalid episode index")
    if "page" in value and (
        not isinstance(value["page"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", value["page"])
    ):
        raise ValueError("Invalid page")
    return dict(value)


def page_context(service, owner, dataset_key, episode_index):
    """Resolve a Viewer page reference through the same dataset authorization as a conversation."""
    if not isinstance(dataset_key, str) or len(dataset_key) > 512:
        raise ValueError("Invalid dataset reference")
    context = normalize_context({"type": "episode", "target_id": episode_index, "page": "viewer"}, "")
    with service.store.sessions() as session:
        service.manager._actor(session, owner, write=False)
        ids = session.scalars(
            select(DatasetLocation.location_id).where(DatasetLocation.dataset_key == dataset_key).limit(2)
        ).all()
        if len(ids) != 1:
            raise KeyError("Dataset not found")
        location, _ = service.manager._location(session, ids[0], owner, write=False)
        return {
            "location_id": ids[0],
            "dataset": safe_text(location["dataset_key"].rsplit("/", 1)[-1], 200),
            "context": context,
        }


def episode_context(location, episode_index):
    """Use published Viewer metadata only; never read Parquet, video, or client-supplied instruction text."""
    metadata = location.get("metadata") or {}
    result = {
        "episode_index": episode_index,
        "instruction_available": False,
        "source": "published_viewer_metadata",
        "media_observed": False,
    }
    cache = Path(metadata.get("cache_root") or "")
    manifest = cache / "static" / "viewer_manifest.json"
    try:
        if (
            not metadata.get("viewer_ready")
            or not cache.is_absolute()
            or manifest.is_symlink()
            or not manifest.resolve().is_relative_to(cache.resolve())
        ):
            raise ValueError("Unavailable Viewer metadata")
        with manifest.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Viewer metadata exceeds the bounded read limit")
        data = json.loads(raw)
        if data.get("version") != 1 or data.get("root") != location.get("root"):
            raise ValueError("Viewer metadata does not match this dataset")
        episode = next(
            (item for item in data.get("episodes", []) if item.get("episode_index") == episode_index), None
        )
        if episode is None:
            raise KeyError("Episode not found in the published Viewer")
        tasks = episode.get("tasks") or []
        if isinstance(tasks, str):
            tasks = [tasks]
        if not isinstance(tasks, list):
            raise ValueError("Invalid instruction metadata")
        texts = [safe_text(item, 1000) for item in tasks[:10] if isinstance(item, str) and item.strip()]
        result.update(
            instructions=texts,
            instruction_available=bool(texts),
            instructions_truncated=len(tasks) > 10 or any(len(str(t)) > 1000 for t in tasks[:10]),
            length=episode.get("length") if type(episode.get("length")) is int else None,
            fps=data.get("fps") if type(data.get("fps")) in {int, float} else None,
        )
        if not texts:
            result["unavailable_reason"] = (
                "This published episode metadata contains no instruction text; this does not prove the source dataset has none."
            )
        # Bind only the selected episode and metadata identity, not unrelated result sidecars.
        identity = content_digest(
            {
                "episode": episode,
                "root": data.get("root"),
                "fps": data.get("fps"),
                "version": data.get("version"),
            }
        )
        return result, {"episode_index": episode_index, "viewer_episode_digest": identity}
    except KeyError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        result["unavailable_reason"] = (
            "Published episode metadata is unavailable or cannot be safely verified. Instruction text may still exist; open or refresh the Viewer."
        )
        return result, {"episode_index": episode_index, "viewer_episode_digest": None}


def _quality_files(manager, target):
    directory = manager.lifecycle.root / "curation" / "runs" / target
    values, identity = {}, {}
    for name in ("quality_flagged_episodes.json", "quality_coverage.json"):
        path = directory / "static" / name
        if not path.exists():
            identity[name] = None
            continue
        if (
            path.is_symlink()
            or not directory.resolve().is_relative_to(
                (manager.lifecycle.root / "curation" / "runs").resolve()
            )
            or not path.resolve().is_relative_to(directory.resolve())
        ):
            raise ValueError("Unsafe report artifact")
        with path.open("rb") as stream:
            raw = stream.read(262145)
        identity[name] = {
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
            "digest": content_digest(raw.hex()),
        }
        if len(raw) > 262144:
            values[name] = {"unavailable": "Legacy report exceeds bounded read limit", "truncated": True}
            continue
        try:
            data = json.loads(raw)
            if name == "quality_coverage.json":
                values["coverage"] = safe_summary(data.get("streams", [])[:20])
            else:
                values["summary"] = safe_summary(data.get("summary", {}))
                reasons = data.get("flag_reasons", {})
                values["examples"] = (
                    safe_summary(dict(list(reasons.items())[:20])) if isinstance(reasons, dict) else []
                )
                values["examples_truncated"] = len(reasons) > 20
        except (ValueError, TypeError, AttributeError):
            values[name] = {"unavailable": "Report summary is unavailable"}
    return values, identity


def resolve_context(manager, session, owner, location_id, context):
    """Authorize all referenced inputs before reading any report or task details."""
    location, _ = manager._location(session, location_id, owner, write=False)
    kind, target = context["type"], context["target_id"]
    links = [
        {
            "id": "dataset",
            "title": "Open dataset",
            "url": "/?" + urlencode({"select": "remote:" + location_id}),
        }
    ]
    identity, summary = {}, {}
    if kind in {"job", "quality"}:
        job = session.get(RemoteJob, target)
        if not job or job.location_id != location_id:
            raise KeyError("Context not found for this dataset")
        keys, locations = dataset_references(job.options or {})
        allowed_keys = manager.store.restricted_dataset_keys(owner)
        if allowed_keys is not None and not keys.issubset(allowed_keys):
            raise PermissionError("Task input is outside this account's data permissions")
        for source in locations:
            manager._location(session, source, owner, write=False)
        actor = manager._actor(session, owner, write=False)
        if kind == "job" and actor.role != "admin" and job.requested_by != owner:
            raise KeyError("Job not found")
        control = session.get(JobControl, target)
        identity["attempt_id"] = control.attempt_id if control else None
        summary = {"operation": job.operation, "status": job.status, "updated_at": job.updated_at.isoformat()}
        links.append(
            {"id": "job", "title": "Open job", "url": "/?" + urlencode({"job_id": target, "page": "runs"})}
        )
        if kind == "job":
            # Reuse admission/queue interpretation without copying the result or raw diagnostics.
            actor = manager._actor(session, owner, write=False)
            decorated = manager.store.job_manager.decorate(
                manager.store._job_dict(job), {"user_id": owner, "role": actor.role}
            )
            summary["queue_wait_reason"] = decorated.get("queue_wait_reason")
            summary["progress"] = safe_summary(
                {
                    key: value
                    for key, value in (job.result or {}).get("progress", {}).items()
                    if key in {"phase", "progress", "current", "total", "phase_current", "phase_total"}
                }
            )
            summary["error"] = diagnostic(job.error) if job.error else None
            rows = session.scalars(
                select(RemoteJobEvent)
                .where(RemoteJobEvent.job_id == target)
                .order_by(RemoteJobEvent.event_id.desc())
                .limit(21)
            ).all()
            summary["events"] = [
                {"level": row.level, "at": row.created_at.isoformat(), "message": diagnostic(row.message)}
                for row in rows[:20]
                if not identity["attempt_id"]
                or (row.payload or {}).get("attempt_id") == identity["attempt_id"]
            ]
            summary["events_truncated"] = len(rows) > 20
        else:
            if manager.store.restricted_dataset_keys(owner) is not None:
                raise PermissionError("Quality report context requires unrestricted dataset access")
            record = manager.lifecycle._get_record("curation_runs", target) if manager.lifecycle else None
            if job.operation != "curation.quality" or job.status != "done" or not record:
                raise JobConflictError("Choose a completed published Quality report")
            bound = record["target"]
            if bound.get("location_id") != location_id:
                raise KeyError("Report does not belong to the selected location")
            data, artifacts = _quality_files(manager, target)
            identity.update(version=bound.get("dataset_version_id"), artifacts=artifacts)
            summary.update(data)
            summary["coverage_notice"] = (
                "Flags are review candidates; sampled or missing coverage is not a full-data certificate."
            )
            links.append(
                {
                    "id": "quality",
                    "title": "Open Quality evidence",
                    "url": "/curation?"
                    + urlencode({"location_id": location_id, "run": target, "page": "quality"}),
                }
            )
    else:
        info = manager._info(location)
        summary = {
            k: info[k] for k in ("codebase_version", "fps", "total_episodes", "total_frames") if k in info
        }
        summary["features"] = {
            safe_text(k, 128): {
                field: safe_summary(value[field])
                for field in ("dtype", "shape", "units", "quaternion_order")
                if field in value
            }
            for k, value in list((info.get("features") or {}).items())[:40]
            if isinstance(value, dict)
        }
        summary["total_feature_count"] = len(info.get("features") or {})
        summary["snapshot_available"] = bool(
            manager.lifecycle and manager.lifecycle._get_record("curation_locations", location_id)
        )
    if kind == "episode":
        details, identity = episode_context(location, int(target))
        summary.update(details)
        reference = viewer_reference(location)
        if reference:
            links.append(
                {
                    "id": "episode",
                    "title": "Open current episode",
                    "url": reference["url"].rsplit("_", 1)[0] + "_" + target,
                }
            )
    summary["page"] = context.get("page", "viewer" if kind == "episode" else kind)
    viewer = viewer_reference(location)
    if kind in {"pipeline", "dataset"}:
        summary["viewer_available"] = bool(viewer)
    if viewer:
        links.append(viewer)
    actor = manager._actor(session, owner, write=False)
    suggestions = [{"type": "navigate", **link} for link in links]
    if actor.role != "viewer" and kind in {"dataset", "quality"}:
        suggestions.append(
            {
                "type": "prepare_draft",
                "title": "Plan next steps",
                "message": "根据当前摘要建议后续处理流程，保留人工复核，先预览，不执行。",
            }
        )
    return {
        "suggestions": suggestions,
        "identity": identity,
        "summary": bounded({**summary, "observed_at": int(time.time())}),
        "references": links,
    }

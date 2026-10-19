"""Read archived historical evidence without involving scheduling or summary list requests."""

from __future__ import annotations

import copy
from dataclasses import asdict

from lerobot.data_platform.artifact_store import ArtifactRef, ArtifactUnavailableError, configured_store

REPORT_PATHS = (("precompute",), ("mcap_import", "files"), ("mcap_import", "rejected"))
REFERENCE_KEY = "_oss_artifact"


def field_value(result, path):
    value = result
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def set_field(result, path, value):
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def archived_fields(result):
    return sorted(
        set(result.get("_archive_fields", []))
        | {
            ".".join(path)
            for path in REPORT_PATHS
            if isinstance(field_value(result, path), dict) and REFERENCE_KEY in field_value(result, path)
        }
    )


def reference(value):
    return {REFERENCE_KEY: asdict(value)}


def hydrate_result(result, store=None):
    if not archived_fields(result):
        return result
    store = store or configured_store()
    result = copy.deepcopy(result)
    remaining = 64 * 1024**2
    for path in REPORT_PATHS:
        value = field_value(result, path)
        if isinstance(value, dict) and REFERENCE_KEY in value:
            try:
                ref = ArtifactRef.parse(value[REFERENCE_KEY])
                loaded = store.read_json(ref, max_bytes=remaining)
                remaining -= ref.size
                set_field(result, path, loaded)
            except (ValueError, TypeError, KeyError):
                raise ArtifactUnavailableError("Archived report reference is invalid") from None
    result.pop("_archive_fields", None)
    return result


def require_reader_compatibility(control, protocol):
    """Old executors must not receive references they could silently treat as report contents."""
    if protocol >= 1:
        return
    from sqlalchemy import func, or_, select

    from lerobot.data_platform.control_plane import RemoteJob
    from lerobot.data_platform.management_storage import JobAttempt

    with control.sessions() as session:
        for model, identity in ((RemoteJob, RemoteJob.job_id), (JobAttempt, JobAttempt.attempt_id)):
            conditions = [
                func.json_extract(model.result, "$." + ".".join(path) + "._oss_artifact.sha256").is_not(None)
                for path in REPORT_PATHS
            ]
            if session.scalar(select(identity).where(or_(*conditions)).limit(1)):
                raise RuntimeError(
                    "Restore inline historical reports before installing a release without artifact reader protocol 1"
                )

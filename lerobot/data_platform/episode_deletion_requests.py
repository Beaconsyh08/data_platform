"""Administrator review of explicit episode deletion requests on registered Agents."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from lerobot.data_platform.control_plane import (
    ControlPlaneNode,
    ControlPlaneUser,
    DatasetLocation,
    EpisodeDeletionRequest,
    _iso,
    _utcnow,
)
from lerobot.data_platform.job_management import JobConflictError, scheduler_lock
from lerobot.data_platform.management_storage import enqueue_event
from lerobot.data_platform.operation_log import build_operation_event


def _snapshot(location):
    return {"root": location.root, "metadata": location.details or {}}


def has_approved_deletion(session, job):
    """Use persisted approval, never client-supplied approval flags, when claiming."""
    if job.operation != "mutation.delete_episodes":
        return False
    row = session.scalar(
        select(EpisodeDeletionRequest).where(
            EpisodeDeletionRequest.job_id == job.job_id,
            EpisodeDeletionRequest.status == "approved",
        )
    )
    if row is None:
        return False
    reviewer = session.get(ControlPlaneUser, row.reviewed_by)
    return bool(
        reviewer
        and reviewer.active
        and reviewer.role == "admin"
        and row.requested_by == job.requested_by
        and row.location_id == job.location_id
        and row.episodes == job.options.get("episodes")
        and row.reason == job.options.get("reason")
    )


def _serialize(row):
    return {
        key: getattr(row, key)
        for key in (
            "request_id",
            "location_id",
            "dataset_key",
            "requested_by",
            "episodes",
            "reason",
            "status",
            "reviewed_by",
            "review_reason",
            "job_id",
        )
    } | {"created_at": _iso(row.created_at), "reviewed_at": _iso(row.reviewed_at)}


def list_requests(store, actor):
    query = select(EpisodeDeletionRequest).order_by(EpisodeDeletionRequest.created_at.desc()).limit(200)
    if actor["role"] != "admin":
        query = query.where(EpisodeDeletionRequest.requested_by == actor["user_id"])
    with store.sessions() as session:
        user = session.get(ControlPlaneUser, actor["user_id"])
        return [
            _serialize(row)
            for row in session.scalars(query)
            if store.dataset_access_allowed(session, user, session.get(DatasetLocation, row.location_id))
        ]


def create_request(store, location_id, actor, body):
    if actor["role"] not in {"operator", "admin"}:
        raise PermissionError("An admin or operator account is required")
    episodes = body.get("episodes")
    reason = str(body.get("reason") or "").strip()
    if not isinstance(episodes, list) or not 1 <= len(episodes) <= 10000:
        raise ValueError("Select between 1 and 10000 explicit episode indices")
    if any(type(value) is not int or value < 0 for value in episodes):
        raise ValueError("Episode indices must be non-negative integers")
    if not reason or len(reason) > 500:
        raise ValueError("A deletion reason of 1-500 characters is required")
    with store.sessions.begin() as session:
        location = session.get(DatasetLocation, location_id)
        if location is None:
            raise KeyError(location_id)
        if location.state != "available":
            raise JobConflictError("Dataset location is not available")
        row = EpisodeDeletionRequest(
            request_id=str(uuid.uuid4()),
            location_id=location_id,
            requested_by=actor["user_id"],
            dataset_key=location.dataset_key,
            episodes=sorted(set(episodes)),
            reason=reason,
            snapshot=_snapshot(location),
            status="pending",
            created_at=_utcnow(),
        )
        session.add(row)
        session.flush()
        enqueue_event(
            session,
            build_operation_event(
                "episode_deletion.request",
                status="success",
                actor=actor,
                dataset_keys=[row.dataset_key],
                episode_ids=row.episodes,
                details={"request_id": row.request_id, "reason": row.reason},
            ),
        )
        return _serialize(row)


def review_request(store, request_id, actor, body, *, mutations_enabled):
    if actor["role"] != "admin":
        raise PermissionError("Administrator role required")
    decision = body.get("decision")
    reason = str(body.get("reason") or "").strip()
    if decision not in {"approve", "reject"} or not reason or len(reason) > 500:
        raise ValueError("An approve/reject decision and a review reason of 1-500 characters are required")
    with store.sessions.begin() as session:
        scheduler_lock(session)
        row = session.get(EpisodeDeletionRequest, request_id)
        if row is None:
            raise KeyError(request_id)
        if row.status != "pending":
            raise JobConflictError("This request has already been reviewed")
        job = None
        if decision == "approve":
            if not mutations_enabled:
                raise PermissionError("Source mutations are disabled on the central server")
            location = session.get(DatasetLocation, row.location_id)
            if location is None or location.state != "available" or _snapshot(location) != row.snapshot:
                raise JobConflictError("Dataset changed or is unavailable; submit a new deletion request")
            node = session.get(ControlPlaneNode, location.node_id)
            if node is None or not (node.capabilities or {}).get("source_mutations_enabled"):
                raise JobConflictError("Source mutations are disabled on this Agent")
            if body.get("confirmation") != f"DELETE {row.request_id}":
                raise JobConflictError("Confirm the exact deletion request before approval")
            job = store.create_job(
                location_id=row.location_id,
                requested_by=row.requested_by,
                operation="mutation.delete_episodes",
                options={
                    "episodes": row.episodes,
                    "reason": row.reason,
                    "requested_by": {
                        "user_id": row.requested_by,
                        "approved_by": actor["user_id"],
                        "deletion_request_id": row.request_id,
                    },
                },
                idempotency_key=f"episode-deletion:{row.request_id}",
                _session=session,
            )
            row.job_id = job["job_id"]
        row.status = "approved" if decision == "approve" else "rejected"
        row.reviewed_by = actor["user_id"]
        row.review_reason = reason
        row.reviewed_at = _utcnow()
        session.flush()
        enqueue_event(
            session,
            build_operation_event(
                f"episode_deletion.{decision}",
                status="success",
                actor=actor,
                dataset_keys=[row.dataset_key],
                episode_ids=row.episodes,
                details={"request_id": row.request_id, "reason": reason, "job_id": row.job_id},
            ),
        )
        return {"request": _serialize(row), "job": job}
